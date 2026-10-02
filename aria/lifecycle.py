from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from aria.errors import WorkflowError
from aria.feature_contract import validate_feature_contract
from aria.governance import governance_preflight
from aria.io import atomic_write_bytes, atomic_write_json, exclusive_lock, json_bytes
from aria.project import ProjectConfig, canonical_sha, safe_relative_path
from aria.simple_run import (
    _recover_feature_contract_amendment_locked,
    _run_contract_payload,
    _run_root,
    _sha256_bytes,
    _validate_run_identity,
    close_project_run,
    lock_project_feature_contract,
    read_project_run,
    start_project_run,
)


PHASES = ("specify", "clarify", "plan", "tasks", "implement", "converge")
REQUIREMENT_RE = re.compile(
    r"^\s*[-*]\s+\*\*(?P<id>(?:FR|R)-?\d+)\*\*\s*:\s*(?P<text>.+?)\s*$",
    re.IGNORECASE,
)
TASK_RE = re.compile(
    r"^\s*[-*]\s+\[[ xX]\]\s+(?P<id>T-?\d+)\b(?P<text>.+?)\s*$",
    re.IGNORECASE,
)
ACCEPTANCE_RE = re.compile(r"\bgiven\b.+\bwhen\b.+\bthen\b", re.IGNORECASE)
EXPLICIT_ACCEPTANCE_RE = re.compile(
    r"^\s*[-*]\s+\*\*(?:AC-?\d+)\*\*\s*:\s*(?P<text>.+?)\s*$",
    re.IGNORECASE,
)
REQUIREMENT_TAG_RE = re.compile(r"\[(?P<id>(?:FR|R)-?\d+)\]", re.IGNORECASE)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _lifecycle_path(project: ProjectConfig, run_id: str) -> Path:
    return _run_root(project, run_id) / "lifecycle.json"


def _read_lifecycle(project: ProjectConfig, run_id: str) -> dict[str, object]:
    path = _lifecycle_path(project, run_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Lifecycle state is unreadable: {path}") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("run_id") != run_id
        or payload.get("project") != project.project_id
    ):
        raise WorkflowError(f"Lifecycle identity mismatch: {run_id}")
    phase = payload.get("phase")
    if phase not in PHASES and phase not in {"completed", "blocked", "proposed"}:
        raise WorkflowError(f"Lifecycle phase is invalid: {phase!r}")
    if not isinstance(payload.get("artifacts"), dict):
        raise WorkflowError("Lifecycle artifacts must be a mapping")
    if not isinstance(payload.get("history"), list):
        raise WorkflowError("Lifecycle history must be a list")
    return payload


def _write_lifecycle(project: ProjectConfig, value: dict[str, object]) -> None:
    atomic_write_json(_lifecycle_path(project, str(value["run_id"])), value)


def _initialize_lifecycle(
    project: ProjectConfig, started: dict[str, object]
) -> dict[str, object]:
    run_id = str(started["run_id"])
    manifest = read_project_run(project, run_id)
    if manifest.get("feature_contract", {}).get("required") is not True:
        raise WorkflowError("Feature lifecycle requires a standard or deep design/build run")
    value: dict[str, object] = {
        "schema_version": 1,
        "project": project.project_id,
        "run_id": run_id,
        "task_id": manifest.get("task_id"),
        "phase": "specify",
        "created_at": _now(),
        "updated_at": _now(),
        "artifacts": {},
        "history": [
            {
                "phase": "specify",
                "at": _now(),
                "event": "feature_started",
            }
        ],
    }
    _write_lifecycle(project, value)
    return value


def start_feature(
    project: ProjectConfig,
    *,
    task: str,
    mode: str = "standard",
    spec: str | None = None,
    changed_paths: list[str] | None = None,
    risk_flags: list[str] | None = None,
    backlog_item_id: str | None = None,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    if mode not in {"standard", "deep"}:
        raise WorkflowError("aria feature mode must be standard or deep")
    started = start_project_run(
        project,
        task=task,
        intent="build",
        mode=mode,
        spec=spec,
        changed_paths=changed_paths,
        risk_flags=risk_flags,
        managed_lifecycle=True,
        backlog_item_id=backlog_item_id,
        actor_id=actor_id,
        device_id=device_id,
    )
    lifecycle = _initialize_lifecycle(project, started)
    return {
        **started,
        "lifecycle_path": str(_lifecycle_path(project, str(started["run_id"]))),
        "lifecycle_phase": lifecycle["phase"],
        "next_action": (
            "Codex: inspect the project and submit the feature specification with "
            f"`aria specify --project {project.project_id} --run {started['run_id']} "
            "--input <spec.md>`. The user does not edit JSON or calculate SHA values."
        ),
    }


def lifecycle_status(project: ProjectConfig, *, run_id: str) -> dict[str, object]:
    lock = project.runtime_root / "locks" / f"{run_id}.lock"
    with exclusive_lock(lock, timeout_seconds=120.0):
        manifest = read_project_run(project, run_id)
        manifest = _recover_feature_contract_amendment_locked(
            project, run_id, manifest
        )
        stale_reason = _validate_run_identity(project, manifest, allow_stale=True)
        lifecycle = _read_lifecycle(project, run_id)
    return {
        "ok": True,
        "project": project.project_id,
        "run_id": run_id,
        "phase": lifecycle["phase"],
        "artifacts": lifecycle["artifacts"],
        "run_status": manifest.get("status"),
        "contract_phase": manifest.get("contract_phase"),
        "stale_reason": stale_reason,
        "lifecycle_path": str(_lifecycle_path(project, run_id)),
    }


def _read_markdown(path: Path, label: str) -> bytes:
    try:
        content = path.resolve(strict=True).read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise WorkflowError(f"{label} must be a readable UTF-8 file: {path}") from error
    if not text.strip():
        raise WorkflowError(f"{label} must not be empty")
    return content


def submit_lifecycle_phase(
    project: ProjectConfig,
    *,
    run_id: str,
    phase: str,
    input_path: Path,
    contract_path: Path | None = None,
) -> dict[str, object]:
    if phase not in {"specify", "clarify", "plan", "tasks"}:
        raise WorkflowError(f"Unsupported lifecycle submission phase: {phase}")
    lock = project.runtime_root / "locks" / f"{run_id}.lock"
    with exclusive_lock(lock, timeout_seconds=120.0):
        manifest = read_project_run(project, run_id)
        _validate_run_identity(project, manifest)
        if manifest.get("status") != "started":
            raise WorkflowError(f"Run is not open for lifecycle submission: {run_id}")
        if manifest.get("contract_phase") != "awaiting_feature_contract":
            raise WorkflowError("Lifecycle planning artifacts cannot change after contract lock")
        lifecycle = _read_lifecycle(project, run_id)
        if lifecycle.get("phase") in {"completed", "blocked", "proposed"}:
            raise WorkflowError(f"Lifecycle is terminal: {lifecycle.get('phase')}")
        current_index = PHASES.index(str(lifecycle["phase"]))
        submitted_index = PHASES.index(phase)
        if submitted_index not in {current_index, current_index - 1}:
            raise WorkflowError(
                f"Lifecycle expects phase {lifecycle['phase']}; cannot submit {phase}"
            )
        content = _read_markdown(input_path, phase)
        run_root = _run_root(project, run_id)
        relative = f"lifecycle/{phase}.md"
        raw_contract: bytes | None = None
        contract_relative: str | None = None
        if phase == "tasks":
            if contract_path is None:
                raise WorkflowError("tasks requires --contract with the generated Feature Contract")
            try:
                raw_contract = contract_path.resolve(strict=True).read_bytes()
                contract = json.loads(raw_contract.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise WorkflowError("Feature Contract input must be readable UTF-8 JSON") from error
            validate_feature_contract(
                contract,
                task_id=str(manifest.get("task_id")),
                run_id=run_id,
            )
            policy = manifest.get("feature_contract")
            contract_relative = (
                str(policy.get("artifact")) if isinstance(policy, dict) else ""
            )
            if not contract_relative:
                raise WorkflowError("Run has no Feature Contract artifact declaration")
        destination = run_root / relative
        atomic_write_bytes(destination, content)
        if raw_contract is not None and contract_relative is not None:
            atomic_write_bytes(
                run_root / safe_relative_path(contract_relative), raw_contract
            )
        artifacts = dict(lifecycle["artifacts"])
        artifacts[phase] = {
            "path": relative,
            "sha256": _sha256_bytes(content),
            "size": len(content),
        }
        next_phase = PHASES[submitted_index + 1]
        lifecycle["phase"] = next_phase
        lifecycle["updated_at"] = _now()
        lifecycle["artifacts"] = artifacts
        history = list(lifecycle["history"])
        history.append(
            {
                "phase": phase,
                "at": _now(),
                "event": "artifact_submitted",
                "sha256": artifacts[phase]["sha256"],
            }
        )
        lifecycle["history"] = history
        _write_lifecycle(project, lifecycle)
        return {
            "ok": True,
            "project": project.project_id,
            "run_id": run_id,
            "submitted": phase,
            "phase": next_phase,
            "artifact": artifacts[phase],
            "next_action": _next_action(project.project_id, run_id, next_phase),
        }


def _next_action(project_id: str, run_id: str, phase: str) -> str:
    if phase in {"clarify", "plan"}:
        return (
            f"Create {phase}.md and submit it with `aria {phase} --project "
            f"{project_id} --run {run_id} --input <{phase}.md>`."
        )
    if phase == "tasks":
        return (
            "Create tasks.md plus the machine Feature Contract, then submit both with "
            f"`aria tasks --project {project_id} --run {run_id} --input <tasks.md> "
            "--contract <feature-contract.json>`."
        )
    if phase == "implement":
        return f"Lock and enter implementation with `aria implement --project {project_id} --run {run_id}`."
    if phase == "converge":
        return (
            "After implementation run `aria verify --project "
            f"{project_id} --run {run_id}` when VERIFY.yaml is configured, complete "
            "semantic evidence and independent review, then run `aria converge --project "
            f"{project_id} --run {run_id} --result <result.json>`."
        )
    return "Lifecycle is complete."


def begin_implementation(project: ProjectConfig, *, run_id: str) -> dict[str, object]:
    locked = lock_project_feature_contract(project, run_id=run_id)
    preflight = governance_preflight(project, operation="write", run_id=run_id)
    if preflight.get("ok") is not True:
        raise WorkflowError(
            f"Governance write preflight failed: {preflight.get('state')}"
        )
    lock = project.runtime_root / "locks" / f"{run_id}.lock"
    with exclusive_lock(lock, timeout_seconds=120.0):
        lifecycle = _read_lifecycle(project, run_id)
        if lifecycle.get("phase") == "implement":
            lifecycle["phase"] = "converge"
            lifecycle["updated_at"] = _now()
            history = list(lifecycle["history"])
            history.append(
                {
                    "phase": "implement",
                    "at": _now(),
                    "event": "feature_contract_locked",
                    "sha256": locked["feature_contract"]["sha256"],
                }
            )
            lifecycle["history"] = history
            _write_lifecycle(project, lifecycle)
        elif lifecycle.get("phase") != "converge":
            raise WorkflowError(
                f"Lifecycle changed while entering implementation: {lifecycle.get('phase')}"
            )
    return {
        "ok": True,
        "project": project.project_id,
        "run_id": run_id,
        "phase": "converge",
        "feature_contract": locked["feature_contract"],
        "governance_preflight": preflight,
        "next_action": _next_action(project.project_id, run_id, "converge"),
    }


def converge_feature(
    project: ProjectConfig,
    *,
    run_id: str,
    result_path: Path,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    lifecycle = _read_lifecycle(project, run_id)
    if lifecycle.get("phase") not in {"converge", "completed", "blocked", "proposed"}:
        raise WorkflowError(f"Lifecycle is not ready to converge: {lifecycle.get('phase')}")
    result = close_project_run(
        project,
        run_id=run_id,
        result_path=result_path,
        actor_id=actor_id,
        device_id=device_id,
    )
    lifecycle = _read_lifecycle(project, run_id)
    return {**result, "lifecycle_phase": lifecycle["phase"]}


def _normalized_requirement_id(raw: str, index: int) -> str:
    digits = "".join(character for character in raw if character.isdigit())
    return f"R-{int(digits):03d}" if digits else f"R-{index:03d}"


def _extract_requirements(spec_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in spec_text.splitlines():
        match = REQUIREMENT_RE.match(line)
        if match:
            rows.append(
                {
                    "id": _normalized_requirement_id(match.group("id"), len(rows) + 1),
                    "statement": match.group("text").strip(),
                }
            )
    if not rows:
        raise WorkflowError(
            "Spec Kit spec.md has no functional requirements such as `- **FR-001**: ...`"
        )
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise WorkflowError("Spec Kit spec.md produces duplicate requirement ids")
    return rows


def _requirement_refs(text: str, known: set[str], label: str) -> list[str]:
    refs = [
        _normalized_requirement_id(match.group("id"), index)
        for index, match in enumerate(REQUIREMENT_TAG_RE.finditer(text), start=1)
    ]
    refs = list(dict.fromkeys(refs))
    if not refs:
        raise WorkflowError(f"{label} requires explicit [FR-001] requirement links")
    unknown = sorted(set(refs) - known)
    if unknown:
        raise WorkflowError(f"{label} references unknown requirements: {unknown}")
    return refs


def _extract_tasks(tasks_text: str, requirement_ids: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    known = set(requirement_ids)
    for line in tasks_text.splitlines():
        match = TASK_RE.match(line)
        if not match:
            continue
        raw = match.group("text").strip()
        refs = _requirement_refs(raw, known, f"Spec Kit task {match.group('id')}")
        raw = REQUIREMENT_TAG_RE.sub("", raw)
        raw = re.sub(r"^(?:\s*\[[^]]+\])+\s*", "", raw).strip() or "Execute task"
        rows.append(
            {
                "id": f"T-{len(rows) + 1:03d}",
                "title": raw,
                "requirement_ids": refs,
                "plan_step_ids": ["P-001"],
                "depends_on": [],
            }
        )
    if not rows:
        raise WorkflowError("Spec Kit tasks.md has no checklist tasks such as `- [ ] T001 ...`")
    return rows


def _extract_acceptance(
    spec_text: str, requirement_ids: list[str]
) -> list[tuple[str, tuple[str, ...]]]:
    scenarios: list[tuple[str, tuple[str, ...]]] = []
    known = set(requirement_ids)
    for line in spec_text.splitlines():
        explicit = EXPLICIT_ACCEPTANCE_RE.match(line)
        if explicit:
            raw = explicit.group("text").strip()
            refs = _requirement_refs(raw, known, "Spec Kit acceptance oracle")
            scenarios.append(
                (REQUIREMENT_TAG_RE.sub("", raw).strip(), tuple(refs))
            )
            continue
        if ACCEPTANCE_RE.search(line):
            cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
            refs = _requirement_refs(cleaned, known, "Spec Kit acceptance scenario")
            scenarios.append(
                (REQUIREMENT_TAG_RE.sub("", cleaned).strip(), tuple(refs))
            )
    scenarios = list(dict.fromkeys(scenarios))
    if not scenarios:
        raise WorkflowError(
            "Spec Kit spec.md has no explicit acceptance scenario containing Given/When/Then "
            "or an `- **AC-001**: ...` oracle"
        )
    return scenarios


def _plan_summary(plan_text: str) -> str:
    lines = plan_text.splitlines()
    in_summary = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "## summary":
            in_summary = True
            continue
        if in_summary and stripped.startswith("#"):
            break
        if in_summary and stripped and not stripped.startswith("["):
            return stripped
    return next(
        (
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith(("#", "["))
        ),
        "Implementation plan imported from Spec Kit",
    )


def import_spec_kit(
    project: ProjectConfig, *, run_id: str, spec_dir: Path
) -> dict[str, object]:
    lock = project.runtime_root / "locks" / f"{run_id}.lock"
    with exclusive_lock(lock, timeout_seconds=120.0):
        return _import_spec_kit_locked(
            project, run_id=run_id, spec_dir=spec_dir
        )


def _import_spec_kit_locked(
    project: ProjectConfig, *, run_id: str, spec_dir: Path
) -> dict[str, object]:
    root = spec_dir.resolve(strict=True)
    files = {name: root / name for name in ("spec.md", "plan.md", "tasks.md")}
    content = {name: _read_markdown(path, f"Spec Kit {name}") for name, path in files.items()}
    manifest = read_project_run(project, run_id)
    manifest = _recover_feature_contract_amendment_locked(project, run_id, manifest)
    _validate_run_identity(project, manifest)
    if manifest.get("status") != "started":
        raise WorkflowError("Spec Kit import requires an open run")
    if manifest.get("managed_lifecycle") is not True:
        raise WorkflowError("Spec Kit import requires a managed `aria feature` run")
    if manifest.get("contract_phase") != "awaiting_feature_contract":
        raise WorkflowError("Spec Kit import is forbidden after Feature Contract lock")
    lifecycle = _read_lifecycle(project, run_id)
    if lifecycle.get("phase") not in {"specify", "clarify", "plan", "tasks", "implement"}:
        raise WorkflowError(
            f"Spec Kit import is not allowed in lifecycle phase {lifecycle.get('phase')!r}"
        )
    requirements = _extract_requirements(content["spec.md"].decode("utf-8"))
    requirement_ids = [row["id"] for row in requirements]
    decoded = {name: value.decode("utf-8") for name, value in content.items()}
    unresolved = [
        name for name, text in decoded.items() if "NEEDS CLARIFICATION" in text.upper()
    ]
    if unresolved:
        raise WorkflowError(
            "Spec Kit import requires all NEEDS CLARIFICATION markers to be resolved; "
            f"files={unresolved}"
        )
    spec_text = decoded["spec.md"]
    acceptance_oracles = _extract_acceptance(spec_text, requirement_ids)
    plan_text = decoded["plan.md"]
    plan_summary = _plan_summary(plan_text)
    plan_refs = set(_requirement_refs(plan_text, set(requirement_ids), "Spec Kit plan"))
    if plan_refs != set(requirement_ids):
        raise WorkflowError(
            "Spec Kit plan requirement coverage mismatch; "
            f"missing={sorted(set(requirement_ids) - plan_refs)}"
        )
    contract = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": manifest.get("task_id"),
        "status": "ready",
        "outcome": str(manifest.get("task")),
        "ambiguities_resolved": True,
        "requirements": requirements,
        "acceptance": [
            {
                "id": f"AC-{index:03d}",
                "requirement_ids": list(refs),
                "oracle": oracle,
            }
            for index, (oracle, refs) in enumerate(acceptance_oracles, start=1)
        ],
        "clarifications": [
            {
                "question": "Were material ambiguities resolved in the imported Spec Kit specification?",
                "resolution": "Yes; import rejected unresolved NEEDS CLARIFICATION markers.",
            }
        ],
        "plan": {
            "summary": plan_summary,
            "steps": [
                {
                    "id": "P-001",
                    "title": "Execute the imported Spec Kit implementation plan",
                    "requirement_ids": requirement_ids,
                }
            ],
        },
        "tasks": _extract_tasks(content["tasks.md"].decode("utf-8"), requirement_ids),
        "interchange": {
            "source": "github-spec-kit",
            "source_directory": str(root),
            "mapping": "Exact ARIA interchange links from explicit [FR-001] markers in acceptance, plan and tasks.",
        },
    }
    validate_feature_contract(
        contract, task_id=str(manifest.get("task_id")), run_id=run_id
    )
    run_root = _run_root(project, run_id)
    lifecycle_root = run_root / "lifecycle"
    artifacts: dict[str, dict[str, object]] = {}
    for phase, name in (("specify", "spec.md"), ("plan", "plan.md"), ("tasks", "tasks.md")):
        relative = f"lifecycle/{phase}.md"
        atomic_write_bytes(lifecycle_root / f"{phase}.md", content[name])
        artifacts[phase] = {
            "path": relative,
            "sha256": _sha256_bytes(content[name]),
            "size": len(content[name]),
            "source": f"spec-kit/{name}",
        }
    clarify = b"# Clarifications\n\n- Imported specification contains no unresolved NEEDS CLARIFICATION markers.\n"
    atomic_write_bytes(lifecycle_root / "clarify.md", clarify)
    artifacts["clarify"] = {
        "path": "lifecycle/clarify.md",
        "sha256": _sha256_bytes(clarify),
        "size": len(clarify),
        "source": "aria-import",
    }
    policy = manifest.get("feature_contract")
    contract_relative = str(policy.get("artifact")) if isinstance(policy, dict) else ""
    if not contract_relative:
        raise WorkflowError("Run has no Feature Contract artifact declaration")
    atomic_write_json(run_root / safe_relative_path(contract_relative), contract)
    lifecycle["phase"] = "implement"
    lifecycle["updated_at"] = _now()
    lifecycle["artifacts"] = artifacts
    history = list(lifecycle["history"])
    history.append(
        {
            "phase": "tasks",
            "at": _now(),
            "event": "spec_kit_imported",
            "source_sha256": canonical_sha(
                {name: _sha256_bytes(data) for name, data in content.items()}
            ),
        }
    )
    lifecycle["history"] = history
    _write_lifecycle(project, lifecycle)
    return {
        "ok": True,
        "project": project.project_id,
        "run_id": run_id,
        "phase": "implement",
        "requirements": len(requirements),
        "tasks": len(contract["tasks"]),
        "feature_contract_path": str(run_root / contract_relative),
        "next_action": _next_action(project.project_id, run_id, "implement"),
    }


def export_spec_kit(
    project: ProjectConfig, *, run_id: str, output_dir: Path
) -> dict[str, object]:
    lock = project.runtime_root / "locks" / f"{run_id}.lock"
    with exclusive_lock(lock, timeout_seconds=120.0):
        return _export_spec_kit_locked(
            project, run_id=run_id, output_dir=output_dir
        )


def _export_spec_kit_locked(
    project: ProjectConfig, *, run_id: str, output_dir: Path
) -> dict[str, object]:
    manifest = read_project_run(project, run_id)
    manifest = _recover_feature_contract_amendment_locked(project, run_id, manifest)
    _validate_run_identity(project, manifest, allow_stale=True)
    policy = manifest.get("feature_contract")
    relative = str(policy.get("artifact")) if isinstance(policy, dict) else ""
    if not relative:
        raise WorkflowError("Run has no Feature Contract")
    path = _run_root(project, run_id) / safe_relative_path(relative)
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError("Feature Contract is unreadable") from error
    validate_feature_contract(contract, task_id=str(manifest.get("task_id")), run_id=run_id)
    target = output_dir.absolute()
    target.mkdir(parents=True, exist_ok=True)
    if any((target / name).exists() for name in ("spec.md", "plan.md", "tasks.md")):
        raise WorkflowError("Spec Kit export refuses to overwrite spec.md, plan.md or tasks.md")
    requirement_tags = {
        str(row["id"]): f"FR-{index:03d}"
        for index, row in enumerate(contract["requirements"], start=1)
    }
    spec_lines = [f"# Feature Specification: {contract['outcome']}", "", "## Functional Requirements", ""]
    for index, row in enumerate(contract["requirements"], start=1):
        spec_lines.append(f"- **FR-{index:03d}**: {row['statement']}")
    spec_lines.extend(["", "## Acceptance Scenarios", ""])
    for row in contract["acceptance"]:
        tags = " ".join(
            f"[{requirement_tags[str(requirement_id)]}]"
            for requirement_id in row["requirement_ids"]
        )
        spec_lines.append(f"- **{row['id']}**: {tags} {row['oracle']}")
    plan_lines = [
        f"# Implementation Plan: {contract['outcome']}",
        "",
        "## Summary",
        "",
        str(contract["plan"]["summary"]),
        "",
        "Coverage: " + " ".join(f"[{tag}]" for tag in requirement_tags.values()),
        "",
        "## Steps",
        "",
    ]
    for row in contract["plan"]["steps"]:
        plan_lines.append(f"- **{row['id']}**: {row['title']}")
    task_lines = [f"# Tasks: {contract['outcome']}", ""]
    for index, row in enumerate(contract["tasks"], start=1):
        tags = " ".join(
            f"[{requirement_tags[str(requirement_id)]}]"
            for requirement_id in row["requirement_ids"]
        )
        task_lines.append(f"- [ ] T{index:03d} {tags} {row['title']}")
    rendered = {
        "spec.md": "\n".join(spec_lines).rstrip() + "\n",
        "plan.md": "\n".join(plan_lines).rstrip() + "\n",
        "tasks.md": "\n".join(task_lines).rstrip() + "\n",
    }
    for name, text in rendered.items():
        atomic_write_bytes(target / name, text.encode("utf-8"))
    return {
        "ok": True,
        "project": project.project_id,
        "run_id": run_id,
        "format": "github-spec-kit",
        "output_dir": str(target),
        "files": [
            {"path": name, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
            for name, text in rendered.items()
        ],
    }


def amend_feature_contract(
    project: ProjectConfig,
    *,
    run_id: str,
    contract_path: Path,
    reason: str,
) -> dict[str, object]:
    reason = reason.strip()
    if not reason:
        raise WorkflowError("Contract amendment reason must not be empty")
    lock_path = project.runtime_root / "locks" / f"{run_id}.lock"
    with exclusive_lock(lock_path, timeout_seconds=120.0):
        manifest = read_project_run(project, run_id)
        manifest = _recover_feature_contract_amendment_locked(
            project, run_id, manifest
        )
        _validate_run_identity(project, manifest)
        if manifest.get("status") != "started" or manifest.get("contract_phase") != "feature_contract_locked":
            raise WorkflowError("Only an open locked Feature Contract can be amended")
        policy = manifest.get("feature_contract")
        relative = str(policy.get("artifact")) if isinstance(policy, dict) else ""
        current_path = _run_root(project, run_id) / safe_relative_path(relative)
        try:
            before_bytes = current_path.read_bytes()
            before = json.loads(before_bytes.decode("utf-8"))
            after_bytes = contract_path.resolve(strict=True).read_bytes()
            after = json.loads(after_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowError("Contract amendment must be readable UTF-8 JSON") from error
        validate_feature_contract(after, task_id=str(manifest.get("task_id")), run_id=run_id)
        before_sha = _sha256_bytes(before_bytes)
        after_sha = _sha256_bytes(after_bytes)
        lock_receipt = manifest.get("feature_contract_lock")
        if not isinstance(lock_receipt, dict) or lock_receipt.get("sha256") != before_sha:
            raise WorkflowError("Current Feature Contract no longer matches its lock")
        if before_sha == after_sha:
            raise WorkflowError("Contract amendment does not change the contract")
        history = list(manifest.get("amendment_history", []))
        revision = len(history) + 1
        prior_run_contract_sha = str(manifest.get("contract_sha256"))
        run_root = _run_root(project, run_id)
        revision_root = run_root / "contract-revisions"
        before_relative = f"contract-revisions/{revision:04d}-before.json"
        after_relative = f"contract-revisions/{revision:04d}-after.json"
        receipt_relative = f"contract-revisions/{revision:04d}-receipt.json"
        atomic_write_bytes(revision_root / f"{revision:04d}-before.json", before_bytes)
        atomic_write_bytes(revision_root / f"{revision:04d}-after.json", after_bytes)
        receipt_payload = {
            "schema_version": 1,
            "revision": revision,
            "reason": reason,
            "amended_at": _now(),
            "before_path": before_relative,
            "before_sha256": before_sha,
            "after_path": after_relative,
            "after_sha256": after_sha,
            "previous_lock": lock_receipt,
            "locked_run_contract_sha256": prior_run_contract_sha,
        }
        receipt_content = json_bytes(receipt_payload)
        atomic_write_bytes(revision_root / f"{revision:04d}-receipt.json", receipt_content)
        entry = {
            **receipt_payload,
            "receipt_path": receipt_relative,
            "receipt_sha256": _sha256_bytes(receipt_content),
        }
        history.append(entry)
        new_receipt = {
            "schema_version": 1,
            "path": safe_relative_path(relative),
            "sha256": after_sha,
            "locked_at": _now(),
            "git_baseline_sha256": lock_receipt.get("git_baseline_sha256"),
            "prior_run_contract_sha256": prior_run_contract_sha,
            "revision": revision,
            "previous_feature_contract_sha256": before_sha,
            "reason": reason,
        }
        before_manifest = json.loads(json.dumps(manifest))
        after_manifest = json.loads(json.dumps(manifest))
        after_manifest["amendment_history"] = history
        after_manifest["feature_contract_lock"] = new_receipt
        after_manifest["contract_sha256"] = canonical_sha(
            _run_contract_payload(after_manifest)
        )
        stored_receipt = {
            "schema_version": 1,
            "run_id": run_id,
            "lock": new_receipt,
            "locked_run_contract_sha256": after_manifest["contract_sha256"],
        }
        try:
            before_lock_file = json.loads(
                (run_root / "feature-contract-lock.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise WorkflowError("Current Feature Contract lock file is unreadable") from error
        journal = {
            "schema_version": 1,
            "project": project.project_id,
            "run_id": run_id,
            "phase": "prepared",
            "revision": revision,
            "live_contract_path": safe_relative_path(relative),
            "before_contract_path": before_relative,
            "after_contract_path": after_relative,
            "before_manifest": before_manifest,
            "after_manifest": after_manifest,
            "before_lock_file": before_lock_file,
            "after_lock_file": stored_receipt,
        }
        journal_path = run_root / "amendment-journal.json"
        atomic_write_json(journal_path, journal)
        atomic_write_bytes(current_path, after_bytes)
        atomic_write_json(run_root / "feature-contract-lock.json", stored_receipt)
        atomic_write_json(run_root / "manifest.json", after_manifest)
        atomic_write_json(journal_path, {**journal, "phase": "committed"})
        verified = read_project_run(project, run_id)
        _validate_run_identity(project, verified)
        return {
            "ok": True,
            "project": project.project_id,
            "run_id": run_id,
            "revision": revision,
            "reason": reason,
            "before_sha256": before_sha,
            "after_sha256": after_sha,
            "receipt_path": str(revision_root / f"{revision:04d}-receipt.json"),
        }
