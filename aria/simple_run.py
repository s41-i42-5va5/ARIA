from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

from aria.errors import ConfigurationError, WorkflowError
from aria.execution import prepare_execution_contract, validate_execution_bundle
from aria.feature_contract import (
    feature_contract_policy,
    validate_convergence,
    validate_feature_contract,
)
from aria.assurance import (
    apply_system_map_impact,
    build_assurance_plan,
    inspect_system_map_file,
    load_system_map,
    merge_assurance_plans,
    validate_test_evidence,
)
from aria.integrity import engine_state
from aria.governance import governance_start_guard
from aria.io import atomic_write_bytes, atomic_write_json, exclusive_lock
from aria.lessons import LessonStore
from aria.project import (
    ProjectConfig,
    canonical_sha,
    file_fingerprint,
    git_commit_range,
    git_diff_names,
    git_inventory,
    git_snapshot,
    history_events,
    read_project_text,
    run_project_doctor,
    safe_relative_path,
    verify_history,
)
from aria.project_state import (
    find_task,
    parse_project_state,
    projection_yaml,
    roadmap_overview,
    select_next_task,
    state_current,
    state_profile,
    task_index,
)
from aria.routing import _detect_intent, design_trace_assessment, decide_run_route
from aria.run_contracts import (
    closure_contract,
    functional_coverage_contract,
    required_role_contract,
)


IGNORED_DIRECTORIES = {
    ".git",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".venv",
    "venv",
    ".cache",
    "vendor",
    "target",
    "project-docs",
    "_db_backups",
}
SENSITIVE_DATA_FILES = {
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}
GENERATED_ARTIFACT_NAMES = {
    ".coverage",
    "coverage.xml",
    "lcov.info",
    "junit.xml",
}
BINARY_RUNTIME_SUFFIXES = {
    ".7z",
    ".db",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".log",
    ".pdf",
    ".png",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".webp",
    ".zip",
}
RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")


def _yaml_mapping(text: str, label: str) -> dict[str, object]:
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise WorkflowError(f"{label} is not valid YAML: {error}") from error
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} must contain a top-level mapping")
    return value


def _task_id_for_run(
    task: str,
    task_selection: dict[str, object] | None,
    spec_text: str | None,
) -> str:
    selected = (
        task_selection.get("task_id") if isinstance(task_selection, dict) else None
    )
    if isinstance(selected, str) and selected.strip():
        return selected.strip()
    if spec_text:
        frontmatter = _spec_frontmatter(spec_text)
        for key in ("task_id", "task", "id"):
            value = frontmatter.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return f"task-{canonical_sha(task)[:12]}"


def _state_snapshot(project: ProjectConfig) -> tuple[dict[str, object], str]:
    text = read_project_text(project, project.files.state)
    state = parse_project_state(
        text,
        project_id=project.project_id,
        expected_profile=project.state_profile,
    )
    return state, text


def _resolve_run_task(
    state: dict[str, object], task: str | None
) -> tuple[str, dict[str, object] | None]:
    requested = task.strip() if isinstance(task, str) else ""
    if requested:
        registered = find_task(state, requested)
        if registered is not None:
            return str(registered["task"]), registered
        return requested, {
            "task_id": None,
            "task": requested,
            "stage_id": None,
            "spec": None,
            "source": "explicit-ad-hoc",
            "skipped": [],
        }
    selected = select_next_task(state)
    if selected is None:
        raise WorkflowError(
            "--task is required for a frontier project or when the roadmap has no eligible task"
        )
    if selected.get("blocked"):
        raise WorkflowError(
            f"Roadmap current task is blocked: {selected.get('task_id')}; "
            "pass --task with the concrete unblocking action"
        )
    return str(selected["task"]), selected


def _spec_relative(project: ProjectConfig, value: str) -> str:
    normalized = safe_relative_path(value)
    specs_root = PurePosixPath(safe_relative_path(project.files.specs))
    candidate = PurePosixPath(normalized)
    if candidate.suffix.lower() != ".md" or not candidate.is_relative_to(specs_root):
        raise WorkflowError(
            f"Spec must be a Markdown file under {specs_root.as_posix()}: {value}"
        )
    return normalized


def _resolve_spec(
    project: ProjectConfig,
    *,
    task: str,
    frontier: dict[str, object],
    explicit_spec: str | None,
    selected_spec: str | None = None,
) -> str | None:
    candidates: list[str] = []
    if explicit_spec:
        candidates.append(_spec_relative(project, explicit_spec))
    if selected_spec:
        candidates.append(_spec_relative(project, selected_spec))
    continue_frontier = frontier.get("task_summary") == task and frontier.get(
        "stage"
    ) in {"in_progress", "blocked"}
    if continue_frontier:
        frontier_spec = frontier.get("spec")
        if isinstance(frontier_spec, str) and frontier_spec.strip():
            candidates.append(_spec_relative(project, frontier_spec))
        task_id = frontier.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            base = safe_relative_path(project.files.specs)
            candidates.extend(
                [
                    f"{base}/active/{task_id}.md",
                    f"{base}/{task_id}.md",
                ]
            )
    for candidate in dict.fromkeys(candidates):
        path = project.document_path(candidate)
        if path.is_file():
            return candidate
    if explicit_spec:
        raise WorkflowError(f"Explicit spec does not exist: {explicit_spec}")
    return None


def _spec_frontmatter(spec_text: str) -> dict[str, object]:
    if not spec_text.startswith("---"):
        return {}
    end = spec_text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        value = yaml.safe_load(spec_text[3:end])
    except yaml.YAMLError as error:
        raise WorkflowError(f"Relevant spec frontmatter is invalid: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WorkflowError("Relevant spec frontmatter must be a mapping")
    return value


def _legacy_import_paths(project: ProjectConfig) -> dict[str, str]:
    path = project.docs_root / "legacy-import" / "manifest.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Legacy import manifest is unreadable: {path}") from error
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise WorkflowError("Legacy import manifest files must be a list")
    result: dict[str, str] = {}
    for row in files:
        if not isinstance(row, dict):
            continue
        source = row.get("source_path")
        destination = row.get("destination_path")
        if isinstance(source, str) and isinstance(destination, str):
            result[source.replace("\\", "/")] = destination.replace("\\", "/")
    return result


def _spec_context_hints(
    project: ProjectConfig,
    spec_text: str | None,
) -> dict[str, object]:
    if spec_text is None:
        return {"documents": [], "code": [], "unresolved": [], "ignored": []}
    frontmatter = _spec_frontmatter(spec_text)
    legacy_paths = _legacy_import_paths(project)
    documents: list[dict[str, object]] = []
    code: list[dict[str, object]] = []
    unresolved: list[dict[str, str]] = []
    ignored: list[dict[str, str]] = []
    raw_docs = frontmatter.get("read_docs", [])
    raw_code = frontmatter.get("read_code", [])
    if not isinstance(raw_docs, list) or not all(
        isinstance(item, str) for item in raw_docs
    ):
        raise WorkflowError("Relevant spec read_docs must be a string list")
    if not isinstance(raw_code, list) or not all(
        isinstance(item, str) for item in raw_code
    ):
        raise WorkflowError("Relevant spec read_code must be a string list")

    for raw in dict.fromkeys(raw_docs):
        normalized = safe_relative_path(raw)
        if normalized == "docs/STACK.md":
            normalized = project.files.stack
        direct = project.document_path(normalized)
        resolved_from = "project"
        if not direct.is_file():
            imported = legacy_paths.get(normalized)
            if imported is None:
                if normalized.startswith("docs/policies/"):
                    ignored.append(
                        {
                            "kind": "document",
                            "path": raw,
                            "reason": "legacy process policy is replaced by central ARIA",
                        }
                    )
                    continue
                unresolved.append({"kind": "document", "path": raw})
                continue
            normalized = safe_relative_path(imported)
            direct = project.document_path(normalized)
            resolved_from = "legacy-import"
        if not direct.is_file():
            unresolved.append({"kind": "document", "path": raw})
            continue
        row = file_fingerprint(direct, relative_to=project.docs_root)
        row["requested_path"] = raw
        row["resolved_from"] = resolved_from
        documents.append(row)

    code_root = project.code_root.resolve(strict=True)
    for raw in dict.fromkeys(raw_code):
        try:
            normalized = safe_relative_path(raw)
        except ConfigurationError:
            unresolved.append({"kind": "code", "path": raw})
            continue
        path = project.code_root.joinpath(*PurePosixPath(normalized).parts)
        resolved = path.resolve(strict=False)
        if (
            path.is_symlink()
            or not resolved.is_relative_to(code_root)
            or not path.is_file()
        ):
            unresolved.append({"kind": "code", "path": raw})
            continue
        code.append(file_fingerprint(path, relative_to=project.code_root))
    return {
        "documents": documents,
        "code": code,
        "unresolved": unresolved,
        "ignored": ignored,
    }


def _manifest_sources(project: ProjectConfig) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    root = project.code_root.resolve(strict=True)
    for relative in project.stack_manifests:
        path = project.code_root.joinpath(*PurePosixPath(relative).parts)
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_relative_to(root):
            raise WorkflowError(f"Stack manifest escapes code root: {relative}")
        rows.append(file_fingerprint(path, relative_to=project.code_root))
    return rows


def _safe_code_target(project: ProjectConfig, target: str | None) -> Path:
    relative = (
        safe_relative_path(target or ".") if target not in {None, "", "."} else "."
    )
    candidate = (
        project.code_root
        if relative == "."
        else project.code_root.joinpath(*PurePosixPath(relative).parts)
    )
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(project.code_root.resolve(strict=True)):
        raise WorkflowError(f"Review target escapes code root: {target!r}")
    if not candidate.exists():
        raise WorkflowError(f"Review target does not exist: {candidate}")
    return candidate


def _exclusion_reason(relative: str) -> str | None:
    lowered = relative.lower()
    parts = set(PurePosixPath(lowered).parts)
    # Do not reserve ambiguous domain names such as "coverage" here. Review
    # candidates already come from Git's tracked and non-ignored inventory, so
    # generated coverage output belongs in .gitignore while product packages
    # named coverage must remain reviewable.
    if parts & IGNORED_DIRECTORIES:
        return "ignored-directory"
    name = PurePosixPath(lowered).name
    if name == ".env" or name.startswith(".env.") or name in SENSITIVE_DATA_FILES:
        return "sensitive-data-file"
    if lowered.endswith((".pem", ".key", ".p12", ".pfx")):
        return "sensitive-extension"
    if name in GENERATED_ARTIFACT_NAMES:
        return "generated-runtime-artifact"
    if PurePosixPath(lowered).suffix in BINARY_RUNTIME_SUFFIXES:
        return "binary-or-runtime-artifact"
    return None


def build_review_scope(
    project: ProjectConfig,
    *,
    target_type: str,
    target: str | None,
    spec: str | None,
) -> dict[str, object]:
    if target_type == "spec":
        relative = _spec_relative(project, target) if target else spec
        if not relative:
            raise WorkflowError("Spec review requires --target or a relevant spec")
        path = project.document_path(relative)
        if not path.is_file():
            raise WorkflowError(f"Review spec does not exist: {path}")
        files = [file_fingerprint(path, relative_to=project.docs_root)]
        root = project.docs_root
        resolved_target = path
    elif target_type in {"component", "repository"}:
        if target_type == "component" and not target:
            raise WorkflowError("Component review requires --target")
        resolved_target = _safe_code_target(
            project, None if target_type == "repository" else target
        )
        root = project.code_root
        if resolved_target.is_file():
            candidates = [resolved_target]
        else:
            target_relative = resolved_target.relative_to(root).as_posix()
            candidates = [
                root.joinpath(*PurePosixPath(relative).parts)
                for relative in git_inventory(root, project.git_ignore_prefixes)
                if target_type == "repository"
                or PurePosixPath(relative).is_relative_to(
                    PurePosixPath(target_relative)
                )
            ]
        files = []
        excluded: list[dict[str, str]] = []
        root_resolved = root.resolve(strict=True)
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            reason = _exclusion_reason(relative)
            if reason is None and path.is_symlink():
                reason = "symlink"
            resolved = path.resolve(strict=False)
            if reason is None and not resolved.is_relative_to(root_resolved):
                reason = "outside-root"
            if reason is None and not path.is_file():
                reason = "not-a-file"
            if reason is None and path.stat().st_size > 5 * 1024 * 1024:
                reason = "file-too-large"
            if reason is not None:
                excluded.append({"path": relative, "reason": reason})
                continue
            files.append(file_fingerprint(path, relative_to=root))
        files.sort(key=lambda row: str(row["path"]))
        excluded.sort(key=lambda row: row["path"])
    else:
        raise WorkflowError(f"Unsupported review target type: {target_type!r}")
    if not files:
        raise WorkflowError(
            "Review scope contains no reviewable files; choose a concrete non-ignored target"
        )
    scope_payload = {
        "target_type": target_type,
        "target": str(resolved_target),
        "root": str(root.resolve(strict=True)),
        "files": files,
        "excluded": excluded if target_type != "spec" else [],
    }
    return {
        **scope_payload,
        "file_count": len(files),
        "sha256": canonical_sha(scope_payload),
    }


def _run_contract_payload(manifest: dict[str, object]) -> dict[str, object]:
    """Return the immutable part of a run manifest bound at start."""
    keys = (
        "schema_version",
        "run_id",
        "created_at",
        "project",
        "project_mode",
        "task",
        "task_id",
        "task_selection",
        "state_profile",
        "route",
        "roots",
        "engine",
        "context",
        "context_path",
        "scope_path",
        "review_request",
        "relevant_spec",
        "design_assessment",
        "role_contract",
        "functional_coverage_contract",
        "feature_contract",
        "assurance_plan",
        "system_map",
        "closure_contract",
    )
    # Preserve the exact 1.0 payload shape for unfinished runs created before
    # Feature Contract phases existed. Optional 1.1 keys are bound only when
    # the originating manifest actually contains them.
    optional_keys = {
        "feature_contract",
        "contract_phase",
        "feature_contract_lock",
        "amendment_history",
        "managed_lifecycle",
        "execution_contract",
        "governance",
    }
    return {
        key: manifest.get(key)
        for key in keys
        + (
            "contract_phase",
            "feature_contract_lock",
            "amendment_history",
            "managed_lifecycle",
            "execution_contract",
            "governance",
        )
        if key not in optional_keys or key in manifest
    }


def _run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _context_markdown(
    *,
    project: ProjectConfig,
    task: str,
    route: dict[str, object],
    state_text: str,
    stack_text: str,
    spec_relative: str | None,
    spec_text: str | None,
    git: dict[str, object] | None,
    scope: dict[str, object] | None,
    spec_hints: dict[str, object],
    design_assessment: dict[str, object],
    role_contract: dict[str, object],
    functional_contract: dict[str, object],
    feature_contract: dict[str, object],
    lessons_snapshot: dict[str, object],
    assurance_plan: dict[str, object],
    system_map: dict[str, object],
    task_id: str,
    run_id: str,
    managed_lifecycle: bool,
) -> str:
    sections = [
        f"# ARIA run — {project.display_name}",
        "",
        "## Task",
        "",
        task,
        "",
        "## Route",
        "",
        f"- Intent: `{route['intent']}`",
        f"- Mode: `{route['mode']}`",
        f"- Mechanism: `{route['mechanism']}`",
        f"- Stable task id: `{task_id}`",
        f"- Code root: `{project.code_root}`",
        f"- Project docs: `{project.docs_root}`",
    ]
    if git is not None:
        sections.extend(
            [
                f"- Git HEAD: `{git['head']}`",
                f"- Git branch: `{git['branch']}`",
                f"- Git dirty: `{git['dirty']}`",
            ]
        )
    if scope is not None:
        sections.extend(
            [
                f"- Review target: `{scope['target']}`",
                f"- Review scope SHA-256: `{scope['sha256']}`",
                f"- Review files: `{scope['file_count']}`",
                f"- Excluded inventory entries: `{len(scope.get('excluded', []))}`",
            ]
        )
    sections.extend(
        [
            "",
            "## Assurance plan",
            "",
            yaml.safe_dump(
                assurance_plan,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ).rstrip(),
            "",
            "Use the canonical project tools. For every executed check preserve raw output, "
            "quote an excerpt that is actually present in it, and describe the observed result. "
            "A linked scenario may prove multiple classes, but class_evidence must bind each "
            "class to its own raw-output proof excerpts; load-like classes also bind JSON metrics. "
            "E2E/load/recovery evidence must exercise a linked scenario, not a renamed unit test.",
        ]
    )
    map_content = system_map.get("content")
    sections.extend(
        [
            "",
            "## LLM system map",
            "",
            f"- Path: `{system_map.get('relative_path')}`",
            f"- Valid: `{system_map.get('valid')}`",
            f"- Fresh for current Git/worktree: `{system_map.get('fresh')}`",
            f"- Summary: `{system_map.get('summary')}`",
        ]
    )
    if not system_map.get("fresh"):
        sections.append(
            "- The map is stale: verify affected components against code and refresh its "
            "semantic content before relying on blast-radius conclusions."
        )
    if isinstance(map_content, dict):
        sections.extend(
            [
                "",
                "```yaml",
                yaml.safe_dump(
                    map_content,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                ).rstrip(),
                "```",
            ]
        )
    if route.get("mechanism") == "spec":
        sections.extend(
            [
                "",
                "## Research and ADR assessment",
                "",
                yaml.safe_dump(
                    design_assessment,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                ).rstrip(),
                "",
                "Record explicit research_assessment and adr_assessment in the result. "
                "Search and persist only material sources. A reference is evidence; an ADR "
                "is a durable decision and must not be created for routine implementation details.",
            ]
        )
    required_roles = role_contract.get("required_roles", [])
    if required_roles:
        sections.extend(
            [
                "",
                "## Required independent roles",
                "",
                *[f"- `{role}`" for role in required_roles],
                "",
                "The orchestrator must dispatch these roles automatically. Each role writes "
                "a read-only JSON artifact under outputs/roles/; completed/proposed closure "
                "is rejected until every artifact is read back and verified.",
            ]
        )
    if functional_contract.get("required") is True:
        sections.extend(
            [
                "",
                "## Functional coverage gate",
                "",
                "Before choosing or executing review tests, create `outputs/functional-coverage.md`. "
                "Use the relevant specification when it exists; otherwise infer expected behavior "
                "from code and label what is specified, observed, inferred or still unknown.",
                "",
                "Cover every material function, field/input/output/state, real UI/API/backend/runtime "
                "binding, interaction, invalid input, boundary, nesting/composition case and the "
                "resulting test obligation. Prefer risk-bearing combinations over an exhaustive "
                "Cartesian product.",
                "",
                "The Markdown body remains free-form, but it must contain these non-empty headings "
                "in order:",
                *[f"- `{heading}`" for heading in functional_contract["headings"]],
                "",
                "The functional_coverage_reviewer must challenge omissions and verify that the "
                "selected tests actually cover the material obligations before closure.",
            ]
        )
    if feature_contract.get("required") is True:
        convergence = feature_contract.get("convergence", {})
        sections.extend(
            [
                "",
                "## Feature Contract",
                "",
                "Before implementation or a deep design proposal, create "
                "`outputs/feature-contract.json` with status `ready`. It must state the "
                "measurable outcome, material requirements, acceptance oracles, resolved "
                "clarifications, an implementation plan and an acyclic task graph. Every "
                "requirement must be represented in acceptance, plan and tasks.",
                (
                    f"Then freeze it before changing product code with `aria implement "
                    f"--project {project.project_id} --run {run_id}`. "
                    if managed_lifecycle
                    else f"Then freeze it before changing product code with `py -3.12 -B -m aria "
                    f"_lock-feature-contract --project {project.project_id} --run {run_id}`. "
                )
                +
                "ARIA rejects the lock if the Git baseline has already changed, and closure "
                "rejects any later contract mutation.",
                "",
                "Do not invent a universal scaffold: derive the contract from the user task, "
                "the relevant specification and observed code. Clarifications may be empty "
                "only after explicitly setting `ambiguities_resolved: true`.",
                "",
                "```json",
                json.dumps(
                    feature_contract.get("template"),
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
            ]
        )
        if isinstance(convergence, dict) and convergence.get("required") is True:
            sections.extend(
                [
                    "",
                    "Before completed build closure, create `outputs/convergence.json`. "
                    "Bind every requirement to completed contract tasks and actual changed "
                    "paths. Prove every linked acceptance criterion separately with its exact "
                    "contract oracle and executed verification evidence. Any missing or "
                    "`unproven` acceptance or requirement blocks completed closure.",
                    "",
                    "```json",
                    json.dumps(
                        convergence.get("template"),
                        ensure_ascii=False,
                        indent=2,
                    ),
                    "```",
                ]
            )
    selected_lessons = lessons_snapshot.get("selected", [])
    if selected_lessons:
        sections.extend(["", "## Relevant behavior lessons", ""])
        for lesson in selected_lessons:
            sections.append(
                f"- Trigger: {lesson['trigger']} | Countermeasure: "
                f"{lesson['countermeasure']} | Evidence: {lesson['evidence']}"
            )
    sections.extend(["", "## Current STATE", "", "```yaml", state_text.rstrip(), "```"])
    sections.extend(["", "## Working STACK", "", stack_text.rstrip()])
    if spec_relative and spec_text is not None:
        sections.extend(
            [
                "",
                f"## Relevant spec — `{spec_relative}`",
                "",
                spec_text.rstrip(),
            ]
        )
        hint_documents = spec_hints.get("documents", [])
        hint_code = spec_hints.get("code", [])
        unresolved = spec_hints.get("unresolved", [])
        ignored = spec_hints.get("ignored", [])
        if hint_documents or hint_code or unresolved or ignored:
            sections.extend(["", "## Spec context hints", ""])
            for row in hint_documents:
                sections.append(
                    f"- Read document `{row['path']}` "
                    f"(SHA-256 `{row['sha256']}`, requested `{row['requested_path']}`)"
                )
            for row in hint_code:
                sections.append(
                    f"- Read code `{row['path']}` (SHA-256 `{row['sha256']}`)"
                )
            for row in unresolved:
                sections.append(
                    f"- Unresolved imported hint `{row['kind']}: {row['path']}`; "
                    "verify relevance instead of inventing a document."
                )
            for row in ignored:
                sections.append(
                    f"- Ignored legacy hint `{row['path']}`: {row['reason']}."
                )
    sections.extend(
        [
            "",
            "## Closure",
            "",
            "Do the work with the smallest sufficient process. Confirm the actual result, read it back, review the final scope or diff, then close the run. Do not create a spec unless the selected mechanism is `spec` or the task genuinely needs one.",
            "",
            "The runtime result must follow `manifest.json.closure_contract.result_fields`. Test output files and deep design candidates belong under this run's `outputs/` directory.",
            "Only a completed deep build with changes requires a clean baseline, committed task delta, clean final worktree, ARIA-Task on every commit and ARIA-Run/ARIA-Spec on the final commit.",
            f"Required independent role artifacts must be produced after the final code diff, review scope or spec candidate and bind `target_kind`/`target_sha256` from `py -3.12 -B -m aria _role-target --project {project.project_id} --run {run_id}`.",
            "",
            (
                f"Public close command: `aria converge --project {project.project_id} "
                f"--run {run_id} --result <result.json>`."
                if managed_lifecycle
                else f"Internal close command: `py -3.12 -B -m aria _close-run --project "
                f"{project.project_id} --run {run_id} --result <result.json>`."
            ),
            "",
        ]
    )
    return "\n".join(sections)


def start_project_run(
    project: ProjectConfig,
    *,
    task: str | None,
    intent: str = "auto",
    mode: str = "auto",
    changed_paths: list[str] | None = None,
    risk_flags: list[str] | None = None,
    spec: str | None = None,
    target_type: str | None = None,
    target: str | None = None,
    managed_lifecycle: bool = False,
    backlog_item_id: str | None = None,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    changed = [safe_relative_path(path) for path in (changed_paths or [])]
    risks = list(risk_flags or [])
    doctor = run_project_doctor(project)
    if doctor.get("ok") is not True:
        failed = [
            check["id"]
            for check in doctor.get("checks", [])
            if isinstance(check, dict) and check.get("ok") is not True
        ]
        raise WorkflowError(f"Project doctor failed: {', '.join(map(str, failed))}")
    backlog_revision: int | None = None
    backlog_branch: str | None = None
    if project.framework_version in {"1.5.0", "1.5.1", "1.5.2", "1.5.3", "1.5.4", "1.5.5"}:
        from aria.access import authorize_access, load_access_policy
        from aria.backlog import load_backlog

        access = load_access_policy(project)
        if access.get("status") == "active":
            snapshot = git_snapshot(project.code_root, project.git_ignore_prefixes)
            backlog_branch = (
                str(snapshot["branch"])
                if isinstance(snapshot.get("branch"), str)
                else None
            )
            authorize_access(
                project,
                permission="run.create",
                actor_id=actor_id,
                device_id=device_id,
                version=project.framework_version,
                branch=snapshot.get("branch"),
            )
            backlog_revision = int(load_backlog(project)["revision"])
    state, _state_source_text = _state_snapshot(project)
    task, task_selection = _resolve_run_task(state, task)
    if (
        isinstance(task_selection, dict)
        and task_selection.get("safety_impact") in {"high", "critical"}
        and "safety" not in risks
    ):
        risks.append("safety")
    frontier = state_current(state)
    selected_spec = (
        str(task_selection["spec"])
        if isinstance(task_selection, dict)
        and isinstance(task_selection.get("spec"), str)
        else None
    )
    provisional_spec = _resolve_spec(
        project,
        task=task,
        frontier=frontier,
        explicit_spec=spec,
        selected_spec=selected_spec,
    )
    selected_target_type = target_type
    if selected_target_type is None and (
        intent == "review" or _detect_intent(task) == "review"
    ):
        selected_target_type = "component" if target else "repository"
    route = decide_run_route(
        task=task,
        intent=intent,
        mode=mode,
        changed_paths=changed,
        risk_flags=risks,
        spec_exists=provisional_spec is not None,
        target_type=selected_target_type,
    )
    relevant_spec = (
        _resolve_spec(
            project,
            task=task,
            frontier=frontier,
            explicit_spec=spec,
            selected_spec=selected_spec,
        )
        if route["mode"] in {"standard", "deep"}
        else None
    )
    if route["mechanism"] == "next-task-new" and relevant_spec is None:
        raise WorkflowError(
            "Deep build requires a relevant spec; run deep design/spec first or pass --spec"
        )
    stack_text = read_project_text(project, project.files.stack)
    spec_text = read_project_text(project, relevant_spec) if relevant_spec else None
    task_id = _task_id_for_run(task, task_selection, spec_text)
    run_id = _run_id()
    assessment = design_trace_assessment(task, route)
    roles = required_role_contract(route, selected_target_type)
    functional_contract = functional_coverage_contract(
        selected_target_type if route["intent"] == "review" else None
    )
    feature_contract = feature_contract_policy(
        route, task_id=task_id, run_id=run_id
    )
    lessons_store = LessonStore(project.framework_root, project.runtime_root)
    lessons_snapshot = lessons_store.snapshot(project_id=project.project_id, task=task)
    spec_hints = _spec_context_hints(project, spec_text)
    state_text = projection_yaml(state, selected=task_selection)
    if len(state_text.encode("utf-8")) > project.state_projection_budget_bytes:
        raise WorkflowError(
            "STATE context projection exceeds project budget: "
            f"{len(state_text.encode('utf-8'))}>{project.state_projection_budget_bytes} bytes"
        )
    git_baseline = git_snapshot(
        project.code_root, project.git_ignore_prefixes
    )
    governance_binding = governance_start_guard(
        project,
        intent=str(route["intent"]),
        backlog_item_id=backlog_item_id,
        actor_id=actor_id,
        device_id=device_id,
        git_baseline=git_baseline,
    )
    context_git = git_baseline
    system_map = load_system_map(project, git_baseline)
    if system_map.get("valid") is not True:
        raise WorkflowError(
            f"Project system map is invalid: {system_map.get('errors')}"
        )
    assurance_plan = build_assurance_plan(
        task=task,
        route=route,
        changed_paths=changed,
        risk_flags=risks,
        stack_text=stack_text,
        target_type=selected_target_type,
    )
    assurance_plan = apply_system_map_impact(
        assurance_plan, system_map=system_map, changed_paths=changed
    )
    scope = None
    if route["intent"] == "review":
        scope = build_review_scope(
            project,
            target_type=selected_target_type or "repository",
            target=target,
            spec=relevant_spec,
        )
    history = verify_history(project)
    input_documents = [
        file_fingerprint(project.project_path, relative_to=project.docs_root),
        file_fingerprint(
            project.document_path(project.files.state), relative_to=project.docs_root
        ),
        file_fingerprint(
            project.document_path(project.files.stack), relative_to=project.docs_root
        ),
        file_fingerprint(
            project.document_path(project.files.history), relative_to=project.docs_root
        ),
        file_fingerprint(
            project.document_path(project.files.system_map),
            relative_to=project.docs_root,
        ),
    ]
    if relevant_spec:
        input_documents.append(
            file_fingerprint(
                project.document_path(relevant_spec), relative_to=project.docs_root
            )
        )
    if project.files.verification is not None:
        input_documents.append(
            file_fingerprint(
                project.document_path(project.files.verification),
                relative_to=project.docs_root,
            )
        )
    known_document_paths = {str(row["path"]) for row in input_documents}
    for row in spec_hints["documents"]:
        if str(row["path"]) not in known_document_paths:
            input_documents.append(
                {key: row[key] for key in ("path", "size", "sha256")}
            )
            known_document_paths.add(str(row["path"]))
    manifest_sources = _manifest_sources(project)
    run_root = project.runtime_root / "runs" / run_id
    execution_contract = prepare_execution_contract(project, run_root)
    context_text = _context_markdown(
        project=project,
        task=task,
        route=route,
        state_text=state_text,
        stack_text=stack_text,
        spec_relative=relevant_spec,
        spec_text=spec_text,
        git=context_git,
        scope=scope,
        spec_hints=spec_hints,
        design_assessment=assessment,
        role_contract=roles,
        functional_contract=functional_contract,
        feature_contract=feature_contract,
        lessons_snapshot=lessons_snapshot,
        assurance_plan=assurance_plan,
        system_map=system_map,
        task_id=task_id,
        run_id=run_id,
        managed_lifecycle=managed_lifecycle,
    )
    context_bytes = context_text.encode("utf-8")
    if len(context_bytes) > project.context_budget_bytes:
        raise WorkflowError(
            f"Context exceeds project budget: {len(context_bytes)}>{project.context_budget_bytes} bytes"
        )
    context_path = run_root / "context.md"
    atomic_write_bytes(context_path, context_bytes)
    scope_path: Path | None = None
    if scope is not None:
        scope_path = run_root / "scope.json"
        atomic_write_json(scope_path, scope)
    context_identity = {
        "documents": input_documents,
        "stack_sources": manifest_sources,
        "git": git_baseline,
        "history_head_sha256": history.get("head_sha256"),
        "scope_sha256": scope.get("sha256") if scope else None,
        "scope_artifact_sha256": (
            _sha256_bytes(scope_path.read_bytes()) if scope_path else None
        ),
        "relevant_code": spec_hints["code"],
        "unresolved_spec_hints": spec_hints["unresolved"],
        "ignored_spec_hints": spec_hints["ignored"],
        "behavior_lessons": lessons_snapshot,
        "system_map_sha256": (
            system_map.get("summary", {}).get("sha256")
            if isinstance(system_map.get("summary"), dict)
            else None
        ),
        "system_map_fresh": system_map.get("fresh"),
        "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
    }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "started",
        "project": project.project_id,
        "project_mode": project.mode,
        "task": task,
        "task_id": task_id,
        "task_selection": task_selection,
        "state_profile": state_profile(state),
        "route": route,
        "roots": {
            "framework": str(project.framework_root.resolve(strict=True)),
            "project_docs": str(project.docs_root.resolve(strict=True)),
            "code": str(project.code_root.resolve(strict=True)),
            "runtime": str(project.runtime_root.resolve(strict=True)),
        },
        "engine": engine_state(project.framework_root),
        "context": context_identity,
        "context_path": str(context_path),
        "scope_path": str(scope_path) if scope_path else None,
        "review_request": (
            {"target_type": selected_target_type, "target": target}
            if scope is not None
            else None
        ),
        "relevant_spec": relevant_spec,
        "design_assessment": assessment,
        "role_contract": roles,
        "functional_coverage_contract": functional_contract,
        "feature_contract": feature_contract,
        "contract_phase": (
            "awaiting_feature_contract"
            if feature_contract.get("required") is True
            else "execution"
        ),
        "feature_contract_lock": None,
        "managed_lifecycle": managed_lifecycle,
        "assurance_plan": assurance_plan,
        "system_map": {
            key: system_map.get(key)
            for key in (
                "relative_path",
                "valid",
                "fresh",
                "mapped_git_head",
                "current_git_head",
                "current_working_tree_sha256",
                "summary",
            )
        },
        "closure_contract": closure_contract(
            str(route["intent"]),
            str(route["mechanism"]),
            selected_target_type if route["intent"] == "review" else None,
            feature_contract,
        ),
        "product_writes": [],
    }
    if governance_binding is not None:
        manifest["governance"] = governance_binding
    if execution_contract is not None:
        manifest["execution_contract"] = execution_contract
    manifest["contract_sha256"] = canonical_sha(_run_contract_payload(manifest))
    manifest_path = run_root / "manifest.json"
    contract_anchor = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "start_contract_sha256": manifest["contract_sha256"],
        "feature_contract_present": "feature_contract" in manifest,
        "feature_contract_required": feature_contract.get("required") is True,
        "execution_contract_present": execution_contract is not None,
        "execution_contract_sha256": (
            execution_contract.get("sha256")
            if isinstance(execution_contract, dict)
            else None
        ),
        "created_at": manifest["created_at"],
    }
    anchor_path = run_root / "contract-anchor.json"
    atomic_write_json(anchor_path, contract_anchor)
    atomic_write_json(manifest_path, manifest)
    read_back = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        read_back.get("run_id") != run_id
        or context_path.read_bytes() != context_bytes
        or json.loads(anchor_path.read_text(encoding="utf-8")) != contract_anchor
    ):
        raise WorkflowError(f"Run read-back failed: {run_id}")
    if backlog_revision is not None:
        from aria.backlog import sync_backlog

        try:
            sync_backlog(
                project,
                expected_revision=backlog_revision,
                actor_id=actor_id,
                device_id=device_id,
                version=project.framework_version,
                branch=backlog_branch,
            )
        except BaseException:
            import shutil

            shutil.rmtree(run_root)
            raise
    if route["mechanism"] == "scoped-review":
        next_action = "Analyse the immutable review scope and report verified findings; do not modify code."
    elif route["mechanism"] == "spec":
        next_action = (
            "Dispatch the required architecture attacker, create a reviewed proposal, "
            "then stop for exact user approval; do not start build in this run."
        )
    elif route["mechanism"] == "next-task-new":
        next_action = (
            "Implement, dispatch C1 review, adversarial test design and C2 final review, "
            "then close with verified role artifacts."
        )
    elif route["mode"] == "standard":
        next_action = "Use the context and relevant code; keep one independent review and focused verification."
    else:
        next_action = "Execute directly; do not create a spec."
    if feature_contract.get("required") is True:
        next_action = (
            "Create and lock outputs/feature-contract.json before changing product code. "
            + next_action
        )
    if (
        isinstance(governance_binding, dict)
        and governance_binding.get("operation") == "write"
    ):
        next_action = (
            f"Before the first product mutation run `aria preflight --project "
            f"{project.project_id} --operation write --run {run_id}`. "
            + next_action
        )
    if execution_contract is not None:
        next_action = (
            next_action
            + " Execute configured verification only through aria verify; use its receipt ids "
            "in verification and convergence evidence before closure."
        )
    return {
        "ok": True,
        "run_id": run_id,
        "project": project.project_id,
        "task": task,
        "task_selection": task_selection,
        "route": route,
        "manifest_path": str(manifest_path),
        "context_path": str(context_path),
        "scope_path": str(scope_path) if scope_path else None,
        "assurance_level": assurance_plan["level"],
        "system_map_fresh": system_map.get("fresh"),
        "backlog_item_id": (
            governance_binding.get("backlog_item_id")
            if isinstance(governance_binding, dict)
            else None
        ),
        "next_action": next_action,
    }


def _run_root(project: ProjectConfig, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise WorkflowError(f"Invalid run id: {run_id!r}")
    root = project.runtime_root / "runs" / run_id
    if not root.is_dir():
        raise WorkflowError(f"Run not found: {run_id}")
    return root


def read_project_run(project: ProjectConfig, run_id: str) -> dict[str, object]:
    path = _run_root(project, run_id) / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Run manifest is unreadable: {path}") from error
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        raise WorkflowError(f"Run manifest identity mismatch: {run_id}")
    return payload


def verify_completed_project_run(
    project: ProjectConfig, run_id: str
) -> dict[str, object]:
    run_root = _run_root(project, run_id)
    manifest = read_project_run(project, run_id)
    if manifest.get("status") != "completed":
        raise WorkflowError(f"Run is not completed: {run_id}")
    anchor_path = run_root / "contract-anchor.json"
    if not anchor_path.is_file():
        raise WorkflowError(f"Completed run contract anchor is missing: {run_id}")
    _validate_run_identity(project, manifest)
    result_path = run_root / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Completed run result is unreadable: {run_id}") from error
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise WorkflowError(f"Completed run result identity is invalid: {run_id}")
    stored_result_path = manifest.get("result_path")
    if (
        not isinstance(stored_result_path, str)
        or Path(stored_result_path).resolve(strict=False)
        != result_path.resolve(strict=True)
    ):
        raise WorkflowError(f"Completed run result path mismatch: {run_id}")
    validated = _validate_result(project, run_root, manifest, dict(result))
    result_sha256 = canonical_sha(validated)
    if validated != result or manifest.get("result_sha256") != result_sha256:
        raise WorkflowError(f"Completed run result read-back mismatch: {run_id}")
    return {
        "run_id": run_id,
        "result_sha256": result_sha256,
        "manifest_path": str(run_root / "manifest.json"),
        "result_path": str(result_path),
    }


def _managed_lifecycle(
    project: ProjectConfig, manifest: dict[str, object]
) -> tuple[Path, dict[str, object]] | None:
    if manifest.get("managed_lifecycle") is not True:
        return None
    run_id = str(manifest.get("run_id"))
    path = _run_root(project, run_id) / "lifecycle.json"
    try:
        lifecycle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError("Managed lifecycle state is unreadable") from error
    if (
        not isinstance(lifecycle, dict)
        or lifecycle.get("schema_version") != 1
        or lifecycle.get("project") != project.project_id
        or lifecycle.get("run_id") != run_id
        or lifecycle.get("task_id") != manifest.get("task_id")
    ):
        raise WorkflowError("Managed lifecycle state identity mismatch")
    return path, lifecycle


def _require_managed_lifecycle_phase(
    project: ProjectConfig,
    manifest: dict[str, object],
    expected: str,
) -> None:
    bundle = _managed_lifecycle(project, manifest)
    if bundle is not None and bundle[1].get("phase") != expected:
        raise WorkflowError(
            f"Managed lifecycle must be in {expected!r}; actual={bundle[1].get('phase')!r}"
        )
    if bundle is not None and expected == "implement":
        artifacts = bundle[1].get("artifacts")
        required = {"specify", "clarify", "plan", "tasks"}
        present = set(artifacts) if isinstance(artifacts, dict) else set()
        if not required.issubset(present):
            raise WorkflowError(
                "Managed lifecycle cannot lock before all planning artifacts exist; "
                f"missing={sorted(required - present)}"
            )


def _commit_managed_lifecycle_terminal(
    project: ProjectConfig,
    manifest: dict[str, object],
    status: str,
) -> None:
    bundle = _managed_lifecycle(project, manifest)
    if bundle is None:
        return
    path, lifecycle = bundle
    phase = "completed" if status in {"completed", "proposed"} else status
    if lifecycle.get("phase") == phase:
        return
    if lifecycle.get("phase") != "converge":
        raise WorkflowError(
            f"Managed lifecycle cannot close from phase {lifecycle.get('phase')!r}"
        )
    lifecycle["phase"] = phase
    lifecycle["terminal_status"] = status
    lifecycle["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    history = lifecycle.get("history")
    if not isinstance(history, list):
        raise WorkflowError("Managed lifecycle history is malformed")
    history.append(
        {
            "phase": "converge",
            "at": lifecycle["updated_at"],
            "event": "run_closed",
            "status": status,
        }
    )
    atomic_write_json(path, lifecycle)
    try:
        read_back = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError("Managed lifecycle terminal read-back failed") from error
    if read_back != lifecycle:
        raise WorkflowError("Managed lifecycle terminal read-back mismatch")


def _recover_feature_contract_amendment_locked(
    project: ProjectConfig,
    run_id: str,
    manifest: dict[str, object],
) -> dict[str, object]:
    run_root = _run_root(project, run_id)
    journal_path = run_root / "amendment-journal.json"
    if not journal_path.is_file():
        return manifest
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError("Feature Contract amendment journal is unreadable") from error
    if not isinstance(journal, dict) or journal.get("run_id") != run_id:
        raise WorkflowError("Feature Contract amendment journal identity mismatch")
    if journal.get("phase") != "prepared":
        return manifest
    before_manifest = journal.get("before_manifest")
    after_manifest = journal.get("after_manifest")
    before_lock_file = journal.get("before_lock_file")
    after_lock_file = journal.get("after_lock_file")
    if not all(
        isinstance(value, dict)
        for value in (before_manifest, after_manifest, before_lock_file, after_lock_file)
    ):
        raise WorkflowError("Feature Contract amendment journal is malformed")
    live_relative = safe_relative_path(str(journal.get("live_contract_path")))
    before_relative = safe_relative_path(str(journal.get("before_contract_path")))
    after_relative = safe_relative_path(str(journal.get("after_contract_path")))
    try:
        before_bytes = (run_root / before_relative).read_bytes()
        after_bytes = (run_root / after_relative).read_bytes()
    except OSError as error:
        raise WorkflowError("Feature Contract amendment preimage is unreadable") from error
    if manifest == after_manifest:
        atomic_write_bytes(run_root / live_relative, after_bytes)
        atomic_write_json(run_root / "feature-contract-lock.json", after_lock_file)
        phase = "committed"
        recovered = after_manifest
    elif manifest == before_manifest:
        atomic_write_bytes(run_root / live_relative, before_bytes)
        atomic_write_json(run_root / "feature-contract-lock.json", before_lock_file)
        phase = "rolled-back"
        recovered = before_manifest
    else:
        raise WorkflowError("Feature Contract amendment journal cannot reconcile manifest state")
    atomic_write_json(journal_path, {**journal, "phase": phase})
    return dict(recovered)


def _validate_run_identity(
    project: ProjectConfig,
    manifest: dict[str, object],
    *,
    allow_stale: bool = False,
) -> str | None:
    def stale(reason: str) -> str:
        if allow_stale:
            return reason
        raise WorkflowError(reason)

    if manifest.get("project") != project.project_id:
        raise WorkflowError("Run project identity no longer matches the registry")
    expected_contract = manifest.get("contract_sha256")
    actual_contract = canonical_sha(_run_contract_payload(manifest))
    if expected_contract != actual_contract:
        raise WorkflowError("Run contract was modified after start")

    roots = manifest.get("roots")
    if not isinstance(roots, dict):
        raise WorkflowError("Run roots are missing")
    context = manifest.get("context")
    if not isinstance(context, dict):
        raise WorkflowError("Run context identity is missing")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str):
        raise WorkflowError("Run id is missing from its contract")
    run_root = _run_root(project, run_id)
    anchor_path = run_root / "contract-anchor.json"
    anchor: dict[str, object] | None = None
    if anchor_path.is_file():
        try:
            raw_anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkflowError("Run contract start anchor is unreadable") from error
        if (
            not isinstance(raw_anchor, dict)
            or raw_anchor.get("schema_version") != 1
            or raw_anchor.get("run_id") != run_id
            or raw_anchor.get("task_id") != manifest.get("task_id")
        ):
            raise WorkflowError("Run contract start anchor identity mismatch")
        anchor = raw_anchor
        if (
            "feature_contract_required" in anchor
            and "feature_contract" not in manifest
        ):
            raise WorkflowError("Run Feature Contract policy was removed after start")
        if (
            "feature_contract_present" in anchor
            and anchor["feature_contract_present"] != ("feature_contract" in manifest)
        ):
            raise WorkflowError("Run Feature Contract policy was removed after start")
        execution_descriptor = manifest.get("execution_contract")
        execution_present = isinstance(execution_descriptor, dict)
        if (
            "execution_contract_present" in anchor
            and anchor["execution_contract_present"] != execution_present
        ):
            raise WorkflowError("Run execution contract policy was removed after start")
        if execution_present and "execution_contract_sha256" in anchor:
            if anchor["execution_contract_sha256"] != execution_descriptor.get("sha256"):
                raise WorkflowError("Run execution contract no longer matches its start anchor")
    if "feature_contract" in manifest:
        if anchor is None:
            raise WorkflowError("Run contract start anchor is missing")
        phase = manifest.get("contract_phase")
        start_contract_sha = anchor.get("start_contract_sha256")
        if phase in {"awaiting_feature_contract", "execution"}:
            if expected_contract != start_contract_sha:
                raise WorkflowError("Run contract no longer matches its start anchor")
        elif phase == "feature_contract_locked":
            lock_receipt = manifest.get("feature_contract_lock")
            receipt_path = run_root / "feature-contract-lock.json"
            try:
                stored_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise WorkflowError("Feature Contract lock receipt is unreadable") from error
            if not isinstance(lock_receipt, dict) or not isinstance(stored_receipt, dict):
                raise WorkflowError("Feature Contract lock receipt is malformed")
            amendment_history = manifest.get("amendment_history", [])
            if not isinstance(amendment_history, list):
                raise WorkflowError("Feature Contract amendment history is malformed")
            prior_state_sha = start_contract_sha
            for index, raw_entry in enumerate(amendment_history, start=1):
                if not isinstance(raw_entry, dict) or raw_entry.get("revision") != index:
                    raise WorkflowError("Feature Contract amendment sequence is malformed")
                previous_lock = raw_entry.get("previous_lock")
                if not isinstance(previous_lock, dict):
                    raise WorkflowError("Feature Contract amendment previous lock is missing")
                if previous_lock.get("prior_run_contract_sha256") != prior_state_sha:
                    raise WorkflowError("Feature Contract amendment chain is broken")
                locked_run_sha = raw_entry.get("locked_run_contract_sha256")
                if not isinstance(locked_run_sha, str):
                    raise WorkflowError("Feature Contract amendment run SHA is missing")
                before_path = run_root / safe_relative_path(str(raw_entry.get("before_path")))
                after_path = run_root / safe_relative_path(str(raw_entry.get("after_path")))
                try:
                    before_sha = _sha256_bytes(before_path.read_bytes())
                    after_sha = _sha256_bytes(after_path.read_bytes())
                except OSError as error:
                    raise WorkflowError("Feature Contract amendment artifact is unreadable") from error
                if before_sha != raw_entry.get("before_sha256") or after_sha != raw_entry.get("after_sha256"):
                    raise WorkflowError("Feature Contract amendment artifact SHA mismatch")
                if previous_lock.get("sha256") != before_sha:
                    raise WorkflowError("Feature Contract amendment does not match the previous lock")
                receipt_path = run_root / safe_relative_path(
                    str(raw_entry.get("receipt_path"))
                )
                try:
                    receipt_content = receipt_path.read_bytes()
                    receipt_payload = json.loads(receipt_content.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise WorkflowError("Feature Contract amendment receipt is unreadable") from error
                expected_receipt = {
                    key: value
                    for key, value in raw_entry.items()
                    if key not in {"receipt_path", "receipt_sha256"}
                }
                if (
                    _sha256_bytes(receipt_content) != raw_entry.get("receipt_sha256")
                    or receipt_payload != expected_receipt
                ):
                    raise WorkflowError("Feature Contract amendment receipt mismatch")
                prior_state_sha = locked_run_sha
            if lock_receipt.get("prior_run_contract_sha256") != prior_state_sha:
                raise WorkflowError("Feature Contract lock does not chain to its prior run contract")
            if stored_receipt.get("lock") != lock_receipt:
                raise WorkflowError("Feature Contract lock receipt does not match the manifest")
            if stored_receipt.get("locked_run_contract_sha256") != expected_contract:
                raise WorkflowError("Feature Contract lock receipt run contract SHA mismatch")
        else:
            raise WorkflowError(f"Unknown run contract phase: {phase!r}")
    context_path = run_root / "context.md"
    try:
        context_sha = _sha256_bytes(context_path.read_bytes())
    except OSError as error:
        raise WorkflowError("Run context artifact is unreadable") from error
    if context_sha != context.get("context_sha256"):
        raise WorkflowError("Run context artifact changed after start")

    scope_path_value = manifest.get("scope_path")
    if scope_path_value is not None:
        scope_path = run_root / "scope.json"
        try:
            scope_artifact_sha = _sha256_bytes(scope_path.read_bytes())
        except OSError as error:
            raise WorkflowError("Stored review scope is unreadable") from error
        if scope_artifact_sha != context.get("scope_artifact_sha256"):
            raise WorkflowError("Stored review scope changed after start")

    if manifest.get("project_mode") != project.mode:
        return stale(
            "Run mode changed after start; start a new run after shadow/active cutover"
        )
    expected = {
        "framework": str(project.framework_root.resolve(strict=True)),
        "project_docs": str(project.docs_root.resolve(strict=True)),
        "code": str(project.code_root.resolve(strict=True)),
        "runtime": str(project.runtime_root.resolve(strict=True)),
    }
    if roots != expected:
        return stale("Run roots changed after start; start a new run")

    mutable_documents = {
        safe_relative_path(project.files.state),
        safe_relative_path(project.files.history),
    }
    documents = context.get("documents")
    if not isinstance(documents, list):
        raise WorkflowError("Run document fingerprints are missing")
    for stored in documents:
        if not isinstance(stored, dict) or not isinstance(stored.get("path"), str):
            raise WorkflowError("Run document fingerprint is malformed")
        relative = safe_relative_path(str(stored["path"]))
        if relative in mutable_documents:
            # STATE/HISTORY can already contain this run after a crash before
            # the final manifest write. Their hash-chain is checked separately.
            continue
        try:
            current = file_fingerprint(
                project.document_path(relative), relative_to=project.docs_root
            )
        except OSError as error:
            reason = f"Run input document is no longer readable: {relative}"
            if allow_stale:
                return reason
            raise WorkflowError(reason) from error
        if current != stored:
            return stale(f"Run input document changed after start: {relative}")

    try:
        current_stack_sources = _manifest_sources(project)
    except (OSError, WorkflowError) as error:
        reason = f"Stack manifest sources are no longer readable: {error}"
        if allow_stale:
            return reason
        raise WorkflowError(reason) from error
    if context.get("stack_sources") != current_stack_sources:
        return stale("Stack manifest sources changed after run start")
    relevant_code = context.get("relevant_code", [])
    if not isinstance(relevant_code, list):
        raise WorkflowError("Run relevant code fingerprints are malformed")
    route = manifest.get("route")
    build_run = isinstance(route, dict) and route.get("intent") == "build"
    for stored in [] if build_run else relevant_code:
        if not isinstance(stored, dict) or not isinstance(stored.get("path"), str):
            raise WorkflowError("Run relevant code fingerprint is malformed")
        relative = safe_relative_path(str(stored["path"]))
        path = project.code_root.joinpath(*PurePosixPath(relative).parts)
        try:
            current = file_fingerprint(path, relative_to=project.code_root)
        except OSError as error:
            reason = f"Relevant code is no longer readable: {relative}"
            if allow_stale:
                return reason
            raise WorkflowError(reason) from error
        if current != stored:
            return stale(f"Relevant code changed after run start: {relative}")
    if manifest.get("engine") != engine_state(project.framework_root):
        return stale("ARIA engine changed after run start; start a new run")
    return None


def _runtime_artifact(run_root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str):
        raise WorkflowError(f"{label} path must be a runtime-relative string")
    normalized = safe_relative_path(relative)
    pure = PurePosixPath(normalized)
    if not pure.parts or pure.parts[0] != "outputs":
        raise WorkflowError(f"{label} must be stored under run outputs/")
    path = run_root.joinpath(*pure.parts)
    resolved = path.resolve(strict=False)
    if path.is_symlink() or not resolved.is_relative_to(run_root.resolve(strict=True)):
        raise WorkflowError(f"{label} escapes the run directory")
    if not path.is_file():
        raise WorkflowError(f"{label} does not exist: {normalized}")
    return path


def _declared_json_artifact(
    run_root: Path,
    declaration: object,
    *,
    expected_path: object,
    label: str,
) -> tuple[str, str, dict[str, object]]:
    if not isinstance(declaration, dict):
        raise WorkflowError(f"{label} declaration must be a mapping")
    relative = declaration.get("path")
    if relative != expected_path:
        raise WorkflowError(
            f"{label} path mismatch: {relative!r}!={expected_path!r}"
        )
    artifact = _runtime_artifact(run_root, relative, label)
    content = artifact.read_bytes()
    actual_sha = _sha256_bytes(content)
    if declaration.get("sha256") != actual_sha:
        raise WorkflowError(f"{label} SHA mismatch")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"{label} must be UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise WorkflowError(f"{label} must contain a JSON object")
    return safe_relative_path(str(relative)), actual_sha, payload


def _validate_feature_contract_artifact(
    run_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]] | None:
    policy = manifest.get("feature_contract")
    if not isinstance(policy, dict) or policy.get("required") is not True:
        return None
    relative, sha256, payload = _declared_json_artifact(
        run_root,
        result.get("feature_contract"),
        expected_path=policy.get("artifact"),
        label="Feature Contract",
    )
    validated = validate_feature_contract(
        payload,
        task_id=str(manifest.get("task_id")),
        run_id=str(manifest.get("run_id")),
    )
    lock = manifest.get("feature_contract_lock")
    if manifest.get("contract_phase") != "feature_contract_locked" or not isinstance(
        lock, dict
    ):
        raise WorkflowError(
            "Feature Contract must be locked before implementation with "
            "_lock-feature-contract"
        )
    if lock.get("path") != relative or lock.get("sha256") != sha256:
        raise WorkflowError("Feature Contract differs from the pre-implementation lock")
    context = manifest.get("context")
    baseline = context.get("git") if isinstance(context, dict) else None
    if lock.get("git_baseline_sha256") != canonical_sha(baseline):
        raise WorkflowError("Feature Contract lock Git baseline mismatch")
    evidence = {
        "path": relative,
        "sha256": sha256,
        "outcome": validated.get("outcome"),
        "requirement_ids": [
            str(row.get("id"))
            for row in validated.get("requirements", [])
            if isinstance(row, dict)
        ],
        "acceptance_ids": [
            str(row.get("id"))
            for row in validated.get("acceptance", [])
            if isinstance(row, dict)
        ],
        "task_ids": [
            str(row.get("id"))
            for row in validated.get("tasks", [])
            if isinstance(row, dict)
        ],
    }
    return validated, evidence


def lock_project_feature_contract(
    project: ProjectConfig, *, run_id: str
) -> dict[str, object]:
    """Validate and freeze the Feature Contract before product implementation."""
    run_root = _run_root(project, run_id)
    lock_path = project.runtime_root / "locks" / f"{run_id}.lock"
    with exclusive_lock(lock_path, timeout_seconds=120.0):
        manifest = read_project_run(project, run_id)
        manifest = _recover_feature_contract_amendment_locked(
            project, run_id, manifest
        )
        if manifest.get("status") != "started":
            raise WorkflowError(f"Run is not open for Feature Contract lock: {run_id}")
        _validate_run_identity(project, manifest)
        _require_managed_lifecycle_phase(project, manifest, "implement")
        policy = manifest.get("feature_contract")
        if not isinstance(policy, dict) or policy.get("required") is not True:
            raise WorkflowError("This run does not require a Feature Contract")
        relative = policy.get("artifact")
        artifact = _runtime_artifact(run_root, relative, "Feature Contract")
        content = artifact.read_bytes()
        artifact_sha = _sha256_bytes(content)
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowError("Feature Contract must be UTF-8 JSON") from error
        validate_feature_contract(
            payload,
            task_id=str(manifest.get("task_id")),
            run_id=str(manifest.get("run_id")),
        )
        existing = manifest.get("feature_contract_lock")
        if manifest.get("contract_phase") == "feature_contract_locked":
            if not isinstance(existing, dict) or existing.get("sha256") != artifact_sha:
                raise WorkflowError("Locked Feature Contract was modified")
            return {
                "ok": True,
                "run_id": run_id,
                "status": "feature_contract_locked",
                "feature_contract": existing,
                "idempotent": True,
            }
        allowed_prelock_files = {
            "context.md",
            "manifest.json",
            "contract-anchor.json",
            "feature-contract-lock.json",
            safe_relative_path(str(relative)),
        }
        execution_contract = manifest.get("execution_contract")
        if isinstance(execution_contract, dict):
            execution_relative = safe_relative_path(
                str(execution_contract.get("path"))
            )
            execution_artifact = run_root / execution_relative
            try:
                execution_sha = _sha256_bytes(execution_artifact.read_bytes())
            except OSError as error:
                raise WorkflowError(
                    "Execution contract is unreadable before Feature Contract lock"
                ) from error
            if execution_sha != execution_contract.get("sha256"):
                raise WorkflowError(
                    "Execution contract SHA mismatch before Feature Contract lock"
                )
            allowed_prelock_files.add(execution_relative)
        lifecycle_path = run_root / "lifecycle.json"
        if lifecycle_path.is_file():
            try:
                lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise WorkflowError("Lifecycle state is unreadable before contract lock") from error
            artifacts = lifecycle.get("artifacts") if isinstance(lifecycle, dict) else None
            if (
                not isinstance(lifecycle, dict)
                or lifecycle.get("schema_version") != 1
                or lifecycle.get("run_id") != run_id
                or lifecycle.get("task_id") != manifest.get("task_id")
                or not isinstance(artifacts, dict)
            ):
                raise WorkflowError("Lifecycle state identity is invalid before contract lock")
            allowed_prelock_files.add("lifecycle.json")
            for name, raw_artifact in artifacts.items():
                if not isinstance(name, str) or not isinstance(raw_artifact, dict):
                    raise WorkflowError("Lifecycle artifact declaration is malformed")
                artifact_relative = safe_relative_path(str(raw_artifact.get("path")))
                if (
                    not artifact_relative.startswith("lifecycle/")
                    or PurePosixPath(artifact_relative).suffix.lower() not in {".md", ".json"}
                ):
                    raise WorkflowError("Lifecycle artifact path is outside the planning boundary")
                lifecycle_artifact = run_root / artifact_relative
                try:
                    actual_sha = _sha256_bytes(lifecycle_artifact.read_bytes())
                except OSError as error:
                    raise WorkflowError("Lifecycle artifact is unreadable before contract lock") from error
                if actual_sha != raw_artifact.get("sha256"):
                    raise WorkflowError("Lifecycle artifact SHA mismatch before contract lock")
                allowed_prelock_files.add(artifact_relative)
        unexpected_outputs = sorted(
            path.relative_to(run_root).as_posix()
            for path in run_root.rglob("*")
            if (path.is_file() or path.is_symlink())
            and path.relative_to(run_root).as_posix() not in allowed_prelock_files
        )
        if unexpected_outputs:
            raise WorkflowError(
                "Feature Contract must be locked before creating implementation, "
                f"test or proposal outputs; unexpected={unexpected_outputs}"
            )
        if manifest.get("contract_phase") != "awaiting_feature_contract":
            raise WorkflowError("Run is not awaiting a Feature Contract")
        context = manifest.get("context")
        baseline = context.get("git") if isinstance(context, dict) else None
        current = git_snapshot(project.code_root, project.git_ignore_prefixes)
        if current != baseline:
            raise WorkflowError(
                "Feature Contract must be locked before implementation; "
                "the Git baseline already changed"
            )
        prior_contract_sha = manifest.get("contract_sha256")
        receipt = {
            "schema_version": 1,
            "path": safe_relative_path(str(relative)),
            "sha256": artifact_sha,
            "locked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "git_baseline_sha256": canonical_sha(baseline),
            "prior_run_contract_sha256": prior_contract_sha,
        }
        manifest["contract_phase"] = "feature_contract_locked"
        manifest["feature_contract_lock"] = receipt
        manifest["contract_sha256"] = canonical_sha(_run_contract_payload(manifest))
        stored_receipt = {
            "schema_version": 1,
            "run_id": run_id,
            "lock": receipt,
            "locked_run_contract_sha256": manifest["contract_sha256"],
        }
        atomic_write_json(run_root / "feature-contract-lock.json", stored_receipt)
        manifest_path = run_root / "manifest.json"
        atomic_write_json(manifest_path, manifest)
        read_back = read_project_run(project, run_id)
        _validate_run_identity(project, read_back)
        if read_back.get("feature_contract_lock") != receipt:
            raise WorkflowError("Feature Contract lock read-back mismatch")
        return {
            "ok": True,
            "run_id": run_id,
            "status": "feature_contract_locked",
            "feature_contract": receipt,
            "idempotent": False,
        }


def _portable_feature_contract(
    contract: dict[str, object], evidence: dict[str, object]
) -> dict[str, object]:
    return {
        "schema_version": contract.get("schema_version"),
        "sha256": evidence.get("sha256"),
        "outcome": contract.get("outcome"),
        "requirements": contract.get("requirements", []),
        "acceptance": contract.get("acceptance", []),
        "clarifications": contract.get("clarifications", []),
        "ambiguities_resolved": contract.get("ambiguities_resolved"),
        "plan": contract.get("plan", {}),
        "tasks": contract.get("tasks", []),
    }


def _validate_convergence_artifact(
    run_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
    *,
    feature_contract: dict[str, object],
    feature_contract_sha256: str,
    verification: list[dict[str, object]],
    changed_files: list[str],
    no_change_reason: str | None,
) -> dict[str, object] | None:
    policy = manifest.get("feature_contract")
    convergence_policy = (
        policy.get("convergence") if isinstance(policy, dict) else None
    )
    if (
        not isinstance(convergence_policy, dict)
        or convergence_policy.get("required") is not True
    ):
        return None
    relative, sha256, payload = _declared_json_artifact(
        run_root,
        result.get("convergence"),
        expected_path=convergence_policy.get("artifact"),
        label="Convergence",
    )
    validated = validate_convergence(
        payload,
        run_id=str(manifest.get("run_id")),
        feature_contract=feature_contract,
        feature_contract_sha256=feature_contract_sha256,
        verification=verification,
        changed_files=changed_files,
        no_change_reason=no_change_reason,
    )
    return {
        "path": relative,
        "sha256": sha256,
        "verdict": validated.get("verdict"),
        "proven_requirements": [
            str(row.get("id"))
            for row in validated.get("requirements", [])
            if isinstance(row, dict) and row.get("status") == "proven"
        ],
        "completed_tasks": [
            str(row.get("id"))
            for row in validated.get("tasks", [])
            if isinstance(row, dict) and row.get("status") == "completed"
        ],
    }


def _document_target(
    project: ProjectConfig,
    value: object,
    *,
    root: str,
    label: str,
) -> tuple[str, Path]:
    if not isinstance(value, str):
        raise WorkflowError(f"{label} target must be a project-relative path")
    normalized = safe_relative_path(value)
    candidate = PurePosixPath(normalized)
    allowed = PurePosixPath(safe_relative_path(root))
    if candidate.suffix.lower() != ".md" or not candidate.is_relative_to(allowed):
        raise WorkflowError(
            f"{label} target must be Markdown under {allowed.as_posix()}"
        )
    return normalized, project.document_path(normalized)


def _reference_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise WorkflowError("Research references must be a list")
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise WorkflowError(f"Research reference {index} must be a mapping")
        normalized: dict[str, object] = {}
        for key in ("id", "url", "title", "accessed_at"):
            item = row.get(key)
            if not isinstance(item, str) or not item.strip():
                raise WorkflowError(f"Research reference {index} requires {key}")
            normalized[key] = item.strip()
        if not str(normalized["url"]).startswith(("https://", "http://")):
            raise WorkflowError(f"Research reference {index} URL must be HTTP(S)")
        reference_id = str(normalized["id"])
        if reference_id in seen:
            raise WorkflowError(f"Duplicate research reference id: {reference_id}")
        seen.add(reference_id)
        claims = row.get("claims", [])
        if not isinstance(claims, list) or not all(
            isinstance(item, str) and item.strip() for item in claims
        ):
            raise WorkflowError(
                f"Research reference {index} claims must be a string list"
            )
        normalized["claims"] = [str(item).strip() for item in claims]
        rows.append(normalized)
    return rows


def _candidate_document(
    project: ProjectConfig,
    run_root: Path,
    *,
    candidate_path: object,
    target: object,
    expected_sha: object,
    root: str,
    label: str,
    required_frontmatter: dict[str, object],
) -> dict[str, object]:
    candidate = _runtime_artifact(run_root, candidate_path, label)
    normalized_target, target_path = _document_target(
        project, target, root=root, label=label
    )
    content = candidate.read_bytes()
    actual_sha = _sha256_bytes(content)
    if expected_sha != actual_sha:
        raise WorkflowError(f"{label} candidate SHA mismatch")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkflowError(f"{label} candidate must be UTF-8 Markdown") from error
    frontmatter = _spec_frontmatter(text)
    for key, expected in required_frontmatter.items():
        actual = frontmatter.get(key)
        if isinstance(expected, list):
            if not isinstance(actual, list) or not set(expected).issubset(
                {str(item) for item in actual}
            ):
                raise WorkflowError(
                    f"{label} frontmatter {key} must include {expected}"
                )
        elif actual != expected:
            raise WorkflowError(
                f"{label} frontmatter {key} mismatch: {actual!r}!={expected!r}"
            )
    if target_path.is_file() and target_path.read_bytes() != content:
        raise WorkflowError(
            f"{label} target is immutable and already has different content: "
            f"{normalized_target}"
        )
    artifact_id = frontmatter.get("id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise WorkflowError(f"{label} frontmatter requires id")
    return {
        "id": artifact_id.strip(),
        "path": normalized_target,
        "sha256": actual_sha,
        "candidate_path": safe_relative_path(str(candidate_path)),
    }


def _validate_design_trace(
    project: ProjectConfig,
    run_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
) -> dict[str, object]:
    task_id = str(manifest.get("task_id"))
    spec = _candidate_document(
        project,
        run_root,
        candidate_path=result.get("spec_candidate_path"),
        target=result.get("spec_target"),
        expected_sha=result.get("spec_sha256"),
        root=project.files.specs,
        label="Spec",
        required_frontmatter={"task_id": task_id, "status": "approved"},
    )
    spec_text = _runtime_artifact(
        run_root, result.get("spec_candidate_path"), "Spec"
    ).read_text(encoding="utf-8")
    spec_frontmatter = _spec_frontmatter(spec_text)
    revision = spec_frontmatter.get("revision")
    if not isinstance(revision, int) or revision < 1:
        raise WorkflowError("Spec frontmatter revision must be a positive integer")
    spec["revision"] = revision

    research_assessment = result.get("research_assessment")
    if not isinstance(research_assessment, dict):
        raise WorkflowError("Deep design requires research_assessment")
    research_required = research_assessment.get("required")
    research_reason = research_assessment.get("reason")
    if (
        not isinstance(research_required, bool)
        or not isinstance(research_reason, str)
        or not research_reason.strip()
    ):
        raise WorkflowError(
            "research_assessment requires boolean required and non-empty reason"
        )
    references = _reference_rows(research_assessment.get("references", []))
    research: list[dict[str, object]] = []
    if research_required:
        row = _candidate_document(
            project,
            run_root,
            candidate_path=research_assessment.get("candidate_path"),
            target=research_assessment.get("target"),
            expected_sha=research_assessment.get("sha256"),
            root=project.files.knowledge,
            label="Research",
            required_frontmatter={"task_id": task_id},
        )
        row["references"] = [str(reference["id"]) for reference in references]
        research_text = _runtime_artifact(
            run_root,
            research_assessment.get("candidate_path"),
            "Research",
        ).read_text(encoding="utf-8")
        research_frontmatter = _spec_frontmatter(research_text)
        if set(map(str, research_frontmatter.get("references", []))) != {
            str(reference["id"]) for reference in references
        }:
            raise WorkflowError(
                "Research frontmatter references must match material reference ids"
            )
        research.append(row)
    elif any(
        research_assessment.get(key) is not None
        for key in ("candidate_path", "target", "sha256")
    ):
        raise WorkflowError(
            "Research candidate is only allowed when research_assessment.required is true"
        )

    adr_assessment = result.get("adr_assessment")
    if not isinstance(adr_assessment, dict):
        raise WorkflowError("Deep design requires adr_assessment")
    adr_required = adr_assessment.get("required")
    adr_reason = adr_assessment.get("reason")
    if (
        not isinstance(adr_required, bool)
        or not isinstance(adr_reason, str)
        or not adr_reason.strip()
    ):
        raise WorkflowError(
            "adr_assessment requires boolean required and non-empty reason"
        )
    raw_adrs = adr_assessment.get("candidates", [])
    if not isinstance(raw_adrs, list):
        raise WorkflowError("adr_assessment.candidates must be a list")
    if adr_required != bool(raw_adrs):
        raise WorkflowError(
            "adr_assessment.required must match whether ADR candidates exist"
        )
    adrs: list[dict[str, object]] = []
    for index, raw in enumerate(raw_adrs):
        if not isinstance(raw, dict):
            raise WorkflowError(f"ADR candidate {index} must be a mapping")
        adr_id = raw.get("id")
        if not isinstance(adr_id, str) or not re.fullmatch(r"ADR-[0-9]{3,}", adr_id):
            raise WorkflowError(f"ADR candidate {index} requires id ADR-NNN")
        row = _candidate_document(
            project,
            run_root,
            candidate_path=raw.get("candidate_path"),
            target=raw.get("target"),
            expected_sha=raw.get("sha256"),
            root=project.files.adr,
            label=f"ADR {adr_id}",
            required_frontmatter={
                "id": adr_id,
                "status": "accepted",
                "task_ids": [task_id],
            },
        )
        adr_text = _runtime_artifact(
            run_root, raw.get("candidate_path"), f"ADR {adr_id}"
        ).read_text(encoding="utf-8")
        adr_frontmatter = _spec_frontmatter(adr_text)
        if str(spec["id"]) not in {
            str(item) for item in adr_frontmatter.get("specs", [])
        }:
            raise WorkflowError(f"ADR {adr_id} must reference spec id {spec['id']}")
        unknown_reference_ids = {
            str(item) for item in adr_frontmatter.get("references", [])
        } - {str(reference["id"]) for reference in references}
        if unknown_reference_ids:
            raise WorkflowError(
                f"ADR {adr_id} references unknown material sources: "
                f"{sorted(unknown_reference_ids)}"
            )
        adrs.append(row)
    expected_spec_links = {
        "adrs": {str(row["id"]) for row in adrs},
        "research": {str(row["id"]) for row in research},
        "references": {str(row["id"]) for row in references},
    }
    for key, expected in expected_spec_links.items():
        actual = spec_frontmatter.get(key, [])
        if not isinstance(actual, list) or {str(item) for item in actual} != expected:
            raise WorkflowError(
                f"Spec frontmatter {key} must exactly match published trace ids: "
                f"{sorted(expected)}"
            )
    return {
        "task_id": task_id,
        "spec": spec,
        "adrs": adrs,
        "research": research,
        "references": references,
        "assessments": {
            "research": {
                "required": research_required,
                "reason": research_reason.strip(),
            },
            "adr": {"required": adr_required, "reason": adr_reason.strip()},
        },
    }


def _changed_since_start(
    project: ProjectConfig, manifest: dict[str, object]
) -> set[str]:
    context = manifest.get("context")
    baseline = context.get("git") if isinstance(context, dict) else None
    if not isinstance(baseline, dict) or not isinstance(baseline.get("head"), str):
        raise WorkflowError("Build run has no Git baseline")
    current = git_snapshot(project.code_root, project.git_ignore_prefixes)
    before_changes = baseline.get("changes")
    after_changes = current.get("changes")
    before_paths = (
        before_changes.get("paths") if isinstance(before_changes, dict) else None
    )
    after_paths = (
        after_changes.get("paths") if isinstance(after_changes, dict) else None
    )
    if not isinstance(before_paths, dict) or not isinstance(after_paths, dict):
        raise WorkflowError("Git change fingerprints are missing")
    changed = {
        path
        for path in set(before_paths) | set(after_paths)
        if before_paths.get(path) != after_paths.get(path)
    }
    changed.update(
        git_diff_names(
            project.code_root,
            str(baseline["head"]),
            str(current["head"]),
            project.git_ignore_prefixes,
        )
    )
    return changed


def _build_trace(
    project: ProjectConfig,
    manifest: dict[str, object],
    changed_files: set[str],
) -> dict[str, object]:
    context = manifest.get("context")
    baseline = context.get("git") if isinstance(context, dict) else None
    if not isinstance(baseline, dict) or not isinstance(baseline.get("head"), str):
        raise WorkflowError("Build run has no Git baseline")
    current = git_snapshot(project.code_root, project.git_ignore_prefixes)
    base_commit = str(baseline["head"])
    head_commit = str(current["head"])
    route = manifest.get("route")
    deep_build = (
        isinstance(route, dict)
        and route.get("intent") == "build"
        and route.get("mode") == "deep"
    )
    if changed_files and deep_build:
        if baseline.get("dirty") is not False:
            raise WorkflowError(
                "Completed deep build requires a clean Git baseline; commit or isolate "
                "pre-existing work before starting the run"
            )
        if current.get("dirty") is not False:
            raise WorkflowError(
                "Completed deep build requires all task changes to be committed and a clean worktree"
            )
        if base_commit == head_commit:
            raise WorkflowError(
                "Completed deep build requires at least one new Git commit"
            )
    commits = (
        git_commit_range(project.code_root, base_commit, head_commit)
        if base_commit != head_commit
        else []
    )
    committed_paths = set(git_diff_names(project.code_root, base_commit, head_commit))
    if deep_build and committed_paths != changed_files:
        raise WorkflowError(
            "Deep build committed files must exactly match the run Git delta; "
            f"committed_only={sorted(committed_paths - changed_files)}, "
            f"uncommitted_or_missing={sorted(changed_files - committed_paths)}"
        )
    task_id = str(manifest.get("task_id"))
    run_id = str(manifest.get("run_id"))
    relevant_spec = manifest.get("relevant_spec")
    adr_ids: list[str] = []
    if isinstance(relevant_spec, str):
        spec_text = project.document_path(relevant_spec).read_text(encoding="utf-8-sig")
        spec_metadata = _spec_frontmatter(spec_text)
        raw_adrs = spec_metadata.get("adrs", spec_metadata.get("adr", []))
        if isinstance(raw_adrs, list):
            adr_ids = [str(item) for item in raw_adrs]
    for index, commit in enumerate(commits if deep_build else []):
        trailers = commit.get("trailers")
        if not isinstance(trailers, dict):
            raise WorkflowError(f"Commit {commit.get('sha')} has malformed trailers")
        if task_id not in trailers.get("task", []):
            raise WorkflowError(
                f"Commit {commit.get('sha')} requires trailer ARIA-Task: {task_id}"
            )
        if index == len(commits) - 1:
            if run_id not in trailers.get("run", []):
                raise WorkflowError(f"Final commit requires trailer ARIA-Run: {run_id}")
            if isinstance(relevant_spec, str) and relevant_spec not in trailers.get(
                "spec", []
            ):
                raise WorkflowError(
                    f"Final commit requires trailer ARIA-Spec: {relevant_spec}"
                )
            missing_adrs = sorted(set(adr_ids) - set(trailers.get("adr", [])))
            if missing_adrs:
                raise WorkflowError(
                    f"Final commit is missing ARIA-ADR trailers: {missing_adrs}"
                )
    file_rows: list[dict[str, object]] = []
    for relative in sorted(changed_files):
        path = project.code_root.joinpath(*PurePosixPath(relative).parts)
        file_rows.append(
            {
                "path": relative,
                "sha256": _sha256_bytes(path.read_bytes()) if path.is_file() else None,
            }
        )
    return {
        "base_commit": base_commit,
        "head_commit": head_commit,
        "commits": commits,
        "commit_required": deep_build,
        "changed_files": file_rows,
    }


def role_target_identity(
    project: ProjectConfig,
    run_root: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    route = manifest.get("route")
    intent = route.get("intent") if isinstance(route, dict) else None
    context = manifest.get("context")
    context_sha = context.get("context_sha256") if isinstance(context, dict) else None
    outputs_root = run_root / "outputs"
    output_rows: list[dict[str, object]] = []
    if outputs_root.is_dir():
        for path in sorted(item for item in outputs_root.rglob("*") if item.is_file()):
            relative = path.relative_to(run_root).as_posix()
            if PurePosixPath(relative).is_relative_to(PurePosixPath("outputs/roles")):
                continue
            if relative == "outputs/user-approval.json":
                continue
            output_rows.append(
                {
                    "path": relative,
                    "sha256": _sha256_bytes(path.read_bytes()),
                    "size": path.stat().st_size,
                }
            )
    subject: dict[str, object] = {
        "intent": intent,
        "mode": route.get("mode") if isinstance(route, dict) else None,
        "context_sha256": context_sha,
        "outputs": output_rows,
    }
    if intent == "build":
        current = git_snapshot(project.code_root, project.git_ignore_prefixes)
        changed = sorted(_changed_since_start(project, manifest))
        files: list[dict[str, object]] = []
        for relative in changed:
            path = project.code_root.joinpath(*PurePosixPath(relative).parts)
            files.append(
                {
                    "path": relative,
                    "sha256": _sha256_bytes(path.read_bytes()) if path.is_file() else None,
                }
            )
        baseline = context.get("git") if isinstance(context, dict) else None
        current_changes = current.get("changes")
        subject["git"] = {
            "base_head": baseline.get("head") if isinstance(baseline, dict) else None,
            "head": current.get("head"),
            "working_tree_sha256": canonical_sha(
                current_changes.get("paths", {})
                if isinstance(current_changes, dict)
                else {}
            ),
            "changed_files": files,
        }
    elif intent == "review":
        scope_path = run_root / "scope.json"
        current = git_snapshot(project.code_root, project.git_ignore_prefixes)
        current_changes = current.get("changes")
        subject["review_scope"] = {
            "scope_sha256": context.get("scope_sha256")
            if isinstance(context, dict)
            else None,
            "scope_artifact_sha256": _sha256_bytes(scope_path.read_bytes())
            if scope_path.is_file()
            else None,
            "git_head": current.get("head"),
            "working_tree_sha256": canonical_sha(
                current_changes.get("paths", {})
                if isinstance(current_changes, dict)
                else {}
            ),
        }
    target_kind = f"final-{intent or 'unknown'}-subject"
    return {
        "kind": target_kind,
        "sha256": canonical_sha(subject),
        "subject": subject,
    }


def project_role_target(project: ProjectConfig, *, run_id: str) -> dict[str, object]:
    run_root = _run_root(project, run_id)
    manifest_path = run_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Run manifest is unreadable: {run_id}") from error
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise WorkflowError(f"Run manifest identity mismatch: {run_id}")
    return role_target_identity(project, run_root, manifest)


def _validate_role_evidence(
    project: ProjectConfig,
    run_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
) -> list[dict[str, object]]:
    contract = manifest.get("role_contract")
    required = contract.get("required_roles", []) if isinstance(contract, dict) else []
    if not isinstance(required, list) or not all(
        isinstance(role, str) for role in required
    ):
        raise WorkflowError("Run role contract is malformed")
    raw_evidence = result.get("role_evidence", [])
    if not isinstance(raw_evidence, list):
        raise WorkflowError("role_evidence must be a list")
    rows: dict[str, dict[str, object]] = {}
    context = manifest.get("context")
    context_sha = context.get("context_sha256") if isinstance(context, dict) else None
    target = role_target_identity(project, run_root, manifest)
    for index, raw in enumerate(raw_evidence):
        if not isinstance(raw, dict):
            raise WorkflowError(f"Role evidence {index} must be a mapping")
        role = raw.get("role")
        agent_id = raw.get("agent_id")
        if not isinstance(role, str) or role not in required:
            raise WorkflowError(
                f"Role evidence {index} is not required by this run: {role!r}"
            )
        if role in rows:
            raise WorkflowError(f"Duplicate role evidence: {role}")
        if (
            not isinstance(agent_id, str)
            or not agent_id.strip()
            or agent_id.strip().lower() in {"orchestrator", "main", "root"}
        ):
            raise WorkflowError(f"Role {role} requires an independent agent_id")
        artifact = _runtime_artifact(
            run_root, raw.get("artifact_path"), f"Role {role} artifact"
        )
        content = artifact.read_bytes()
        artifact_sha = _sha256_bytes(content)
        if raw.get("artifact_sha256") != artifact_sha:
            raise WorkflowError(f"Role {role} artifact SHA mismatch")
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowError(f"Role {role} artifact must be UTF-8 JSON") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise WorkflowError(f"Role {role} artifact schema mismatch")
        expected = {
            "run_id": manifest.get("run_id"),
            "role": role,
            "agent_id": agent_id,
            "context_sha256": context_sha,
            "target_kind": target["kind"],
            "target_sha256": target["sha256"],
            "independent": True,
            "changed_code": False,
            "changed_documents": False,
            "git_operations": False,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise WorkflowError(
                    f"Role {role} artifact {key} mismatch: {payload.get(key)!r}!={value!r}"
                )
        if role == "functional_coverage_reviewer":
            functional_contract = manifest.get("functional_coverage_contract")
            if not isinstance(functional_contract, dict):
                raise WorkflowError("Functional coverage role has no run contract")
            functional_artifact = _runtime_artifact(
                run_root,
                functional_contract.get("artifact"),
                "Functional coverage role target",
            )
            functional_expected = {
                "functional_coverage_sha256": _sha256_bytes(
                    functional_artifact.read_bytes()
                ),
                "functional_coverage_scope_sha256": (
                    context.get("scope_sha256") if isinstance(context, dict) else None
                ),
                "test_obligations_reviewed": True,
                "verification_sha256": result.get(
                    "verification_declaration_sha256"
                ),
            }
            for key, value in functional_expected.items():
                if payload.get(key) != value:
                    raise WorkflowError(
                        f"Role {role} artifact {key} mismatch: "
                        f"{payload.get(key)!r}!={value!r}"
                    )
        if payload.get("verdict") not in {"pass", "findings_resolved"}:
            raise WorkflowError(f"Role {role} has no closure-ready verdict")
        summary = payload.get("summary")
        findings = payload.get("findings")
        if not isinstance(summary, str) or not summary.strip():
            raise WorkflowError(f"Role {role} artifact requires summary")
        if not isinstance(findings, list):
            raise WorkflowError(f"Role {role} artifact findings must be a list")
        for finding_index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                raise WorkflowError(
                    f"Role {role} finding {finding_index} must be a mapping"
                )
            for key in ("finding", "evidence", "resolution"):
                if (
                    not isinstance(finding.get(key), str)
                    or not str(finding[key]).strip()
                ):
                    raise WorkflowError(
                        f"Role {role} finding {finding_index} requires {key}"
                    )
        rows[role] = {
            "role": role,
            "agent_id": agent_id,
            "artifact_path": safe_relative_path(str(raw["artifact_path"])),
            "artifact_sha256": artifact_sha,
            "verdict": payload["verdict"],
            "finding_count": len(findings),
            "summary": summary.strip(),
            "target_kind": target["kind"],
            "target_sha256": target["sha256"],
        }
    missing = [role for role in required if role not in rows]
    if missing:
        raise WorkflowError(f"Missing required independent role artifacts: {missing}")
    if {"c1_reviewer", "c2_reviewer"}.issubset(rows) and (
        rows["c1_reviewer"]["agent_id"] == rows["c2_reviewer"]["agent_id"]
    ):
        raise WorkflowError("C1 and C2 must be performed by different agents")
    return [rows[role] for role in required]


def _validate_user_approval(
    run_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
) -> dict[str, object]:
    approval = manifest.get("user_approval")
    if not isinstance(approval, dict):
        raise WorkflowError(
            "Completed deep design requires exact user approval of the proposal"
        )
    artifact = _runtime_artifact(
        run_root, approval.get("artifact_path"), "User approval artifact"
    )
    content = artifact.read_bytes()
    if approval.get("artifact_sha256") != _sha256_bytes(content):
        raise WorkflowError("User approval artifact SHA mismatch")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError("User approval artifact must be UTF-8 JSON") from error
    expected = {
        "schema_version": 1,
        "run_id": manifest.get("run_id"),
        "task_id": manifest.get("task_id"),
        "decision": "approved",
        "actor": "user",
        "proposal_sha256": manifest.get("proposal_sha256"),
        "spec_sha256": result.get("spec_sha256"),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise WorkflowError(f"User approval {key} mismatch")
    statement = payload.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise WorkflowError("User approval requires the user's explicit statement")
    return {
        "actor": "user",
        "decision": "approved",
        "statement": statement.strip(),
        "proposal_sha256": payload["proposal_sha256"],
        "spec_sha256": payload["spec_sha256"],
        "artifact_path": safe_relative_path(str(approval["artifact_path"])),
        "artifact_sha256": approval["artifact_sha256"],
    }


def _validate_review_coverage(
    run_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
    scope_files: list[object],
) -> dict[str, object]:
    artifact = _runtime_artifact(
        run_root, result.get("coverage_path"), "Review coverage artifact"
    )
    content = artifact.read_bytes()
    actual_sha = _sha256_bytes(content)
    if result.get("coverage_sha256") != actual_sha:
        raise WorkflowError("Review coverage artifact SHA mismatch")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError("Review coverage artifact must be UTF-8 JSON") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise WorkflowError("Review coverage artifact schema mismatch")
    context = manifest.get("context")
    expected_scope = context.get("scope_sha256") if isinstance(context, dict) else None
    expected = {
        "run_id": manifest.get("run_id"),
        "scope_sha256": expected_scope,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise WorkflowError(f"Review coverage {key} mismatch")
    expected_files = {
        str(row["path"])
        for row in scope_files
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    scope_path = run_root / "scope.json"
    stored_scope = json.loads(scope_path.read_text(encoding="utf-8"))
    expected_excluded = {
        (str(row.get("path")), str(row.get("reason")))
        for row in stored_scope.get("excluded", [])
        if isinstance(row, dict)
    }
    excluded_rows = payload.get("excluded", [])
    if not isinstance(excluded_rows, list):
        raise WorkflowError("Review coverage excluded inventory must be a list")
    actual_excluded: set[tuple[str, str]] = set()
    for index, row in enumerate(excluded_rows):
        if not isinstance(row, dict):
            raise WorkflowError(f"Excluded coverage {index} must be a mapping")
        path_value = row.get("path")
        reason = row.get("reason")
        disposition = row.get("disposition")
        if not isinstance(path_value, str) or not isinstance(reason, str):
            raise WorkflowError(f"Excluded coverage {index} requires path and reason")
        if disposition not in {"boundary-accepted", "separately-reviewed"}:
            raise WorkflowError(
                f"Excluded coverage {path_value} requires an explicit disposition"
            )
        actual_excluded.add((safe_relative_path(path_value), reason))
    if actual_excluded != expected_excluded:
        raise WorkflowError(
            "Review coverage must acknowledge the exact excluded inventory; "
            f"missing={sorted(expected_excluded - actual_excluded)}; "
            f"unexpected={sorted(actual_excluded - expected_excluded)}"
        )
    rows = payload.get("files")
    if not isinstance(rows, list):
        raise WorkflowError("Review coverage files must be a list")
    covered: dict[str, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise WorkflowError(f"Review coverage file {index} must be a mapping")
        relative = row.get("path")
        status = row.get("status")
        if not isinstance(relative, str):
            raise WorkflowError(f"Review coverage file {index} requires path")
        normalized = safe_relative_path(relative)
        if normalized not in expected_files:
            raise WorkflowError(f"Review coverage contains an out-of-scope file: {normalized}")
        if normalized in covered:
            raise WorkflowError(f"Review coverage contains a duplicate file: {normalized}")
        if status != "reviewed":
            raise WorkflowError(
                f"Review coverage file {normalized} must be reviewed; "
                "non-source boundaries belong in the captured excluded inventory"
            )
        covered[normalized] = str(status)
    if set(covered) != expected_files:
        raise WorkflowError(
            "Review coverage must account for every scope file; "
            f"missing={sorted(expected_files - set(covered))}"
        )
    plan = manifest.get("assurance_plan")
    required_dimensions = (
        plan.get("review_dimensions", []) if isinstance(plan, dict) else []
    )
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, list):
        raise WorkflowError("Review coverage dimensions must be a list")
    dimension_rows: dict[str, dict[str, object]] = {}
    for index, row in enumerate(dimensions):
        if not isinstance(row, dict) or not isinstance(row.get("dimension"), str):
            raise WorkflowError(f"Review dimension {index} must be a mapping with dimension")
        name = str(row["dimension"])
        if name not in required_dimensions:
            raise WorkflowError(f"Unexpected review dimension: {name}")
        if name in dimension_rows:
            raise WorkflowError(f"Duplicate review dimension: {name}")
        status = row.get("status")
        evidence = row.get("evidence")
        if status not in {"reviewed", "not_applicable"}:
            raise WorkflowError(f"Review dimension {name} has invalid status")
        if not isinstance(evidence, str) or len(evidence.strip()) < 10:
            raise WorkflowError(f"Review dimension {name} requires evidence/rationale")
        dimension_rows[name] = row
    missing_dimensions = sorted(set(map(str, required_dimensions)) - set(dimension_rows))
    if missing_dimensions:
        raise WorkflowError(f"Missing review dimensions: {missing_dimensions}")
    request = manifest.get("review_request")
    repository = isinstance(request, dict) and request.get("target_type") == "repository"
    reviewed_dimensions = {
        name for name, row in dimension_rows.items() if row.get("status") == "reviewed"
    }
    mandatory = {"correctness", "architecture", "maintainability", "tests"}
    missing_mandatory = sorted(mandatory.intersection(required_dimensions) - reviewed_dimensions)
    if missing_mandatory:
        raise WorkflowError(
            f"Core review dimensions cannot be not_applicable: {missing_mandatory}"
        )
    if repository and reviewed_dimensions != set(map(str, required_dimensions)):
        raise WorkflowError(
            "Repository assurance must review every planned dimension; "
            f"not_reviewed={sorted(set(map(str, required_dimensions)) - reviewed_dimensions)}"
        )
    return {
        "artifact_path": safe_relative_path(str(result["coverage_path"])),
        "artifact_sha256": actual_sha,
        "scope_files": len(expected_files),
        "reviewed_files": len(expected_files),
        "skipped_files": 0,
        "excluded_inventory": len(expected_excluded),
        "dimensions": list(dimension_rows),
    }


def _validate_functional_coverage(
    run_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
) -> dict[str, object] | None:
    contract = manifest.get("functional_coverage_contract")
    if not isinstance(contract, dict) or contract.get("required") is not True:
        return None
    expected_path = contract.get("artifact")
    supplied_path = result.get("functional_coverage_path")
    if supplied_path != expected_path:
        raise WorkflowError(
            "Functional coverage must use the run contract artifact path"
        )
    artifact = _runtime_artifact(
        run_root, supplied_path, "Functional coverage artifact"
    )
    if artifact.suffix.lower() != ".md":
        raise WorkflowError("Functional coverage artifact must be Markdown")
    content = artifact.read_bytes()
    actual_sha = _sha256_bytes(content)
    if result.get("functional_coverage_sha256") != actual_sha:
        raise WorkflowError("Functional coverage artifact SHA mismatch")
    context = manifest.get("context")
    expected_scope = context.get("scope_sha256") if isinstance(context, dict) else None
    if result.get("functional_coverage_scope_sha256") != expected_scope:
        raise WorkflowError("Functional coverage scope SHA does not match the run")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkflowError("Functional coverage artifact must be UTF-8 Markdown") from error
    lines = text.splitlines()
    headings = contract.get("headings")
    if not isinstance(headings, list) or not all(
        isinstance(heading, str) and heading for heading in headings
    ):
        raise WorkflowError("Functional coverage contract headings are malformed")
    positions: list[int] = []
    for heading in headings:
        matches = [index for index, line in enumerate(lines) if line.strip() == heading]
        if len(matches) != 1:
            raise WorkflowError(
                f"Functional coverage requires exactly one heading: {heading}"
            )
        positions.append(matches[0])
    if positions != sorted(positions):
        raise WorkflowError("Functional coverage headings must follow contract order")
    for index, heading in enumerate(headings):
        start = positions[index] + 1
        end = positions[index + 1] if index + 1 < len(positions) else len(lines)
        if not any(line.strip() for line in lines[start:end]):
            raise WorkflowError(
                f"Functional coverage heading has no analysis: {heading}"
            )
    return {
        "artifact_path": safe_relative_path(str(supplied_path)),
        "artifact_sha256": actual_sha,
        "scope_sha256": expected_scope,
        "headings": list(headings),
    }


def _validate_system_map_candidate(
    project: ProjectConfig,
    run_root: Path,
    manifest: dict[str, object],
    result: dict[str, object],
) -> dict[str, object] | None:
    candidate_value = result.get("system_map_candidate_path")
    if candidate_value is None:
        return None
    route = manifest.get("route")
    request = manifest.get("review_request")
    if not (
        isinstance(route, dict)
        and route.get("intent") == "review"
        and route.get("mode") == "deep"
        and isinstance(request, dict)
        and request.get("target_type") == "repository"
    ):
        raise WorkflowError(
            "SYSTEM_MAP may be replaced only by a deep repository review"
        )
    candidate = _runtime_artifact(
        run_root, candidate_value, "System map candidate"
    )
    content = candidate.read_bytes()
    actual_sha = _sha256_bytes(content)
    if result.get("system_map_sha256") != actual_sha:
        raise WorkflowError("System map candidate SHA mismatch")
    git = git_snapshot(project.code_root, project.git_ignore_prefixes)
    inspected = inspect_system_map_file(project, candidate, git)
    if inspected.get("valid") is not True:
        raise WorkflowError(
            f"System map candidate is invalid: {inspected.get('errors')}"
        )
    if inspected.get("fresh") is not True:
        raise WorkflowError(
            "System map candidate must identify the current Git HEAD and working tree"
        )
    current = load_system_map(project, git)
    if current.get("valid") is not True:
        raise WorkflowError("Current SYSTEM_MAP is invalid; repair it offline first")
    candidate_content = inspected.get("content")
    current_content = current.get("content")
    if not isinstance(candidate_content, dict) or not isinstance(current_content, dict):
        raise WorkflowError("SYSTEM_MAP comparison content is unavailable")

    def ids(payload: dict[str, object], key: str) -> set[str]:
        rows = payload.get(key, [])
        if not isinstance(rows, list):
            return set()
        return {
            str(row["id"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }

    def rows_by_id(
        payload: dict[str, object], key: str
    ) -> dict[str, dict[str, object]]:
        rows = payload.get(key, [])
        if not isinstance(rows, list):
            return {}
        return {
            str(row["id"]): row
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }

    for key in ("components", "shared_primitives", "critical_flows"):
        removed = sorted(ids(current_content, key) - ids(candidate_content, key))
        if removed:
            raise WorkflowError(
                f"SYSTEM_MAP candidate cannot drop existing {key}: {removed}"
            )
    for key, anchor_field in (
        ("components", "paths"),
        ("shared_primitives", "paths"),
        ("critical_flows", "steps"),
    ):
        current_rows = rows_by_id(current_content, key)
        candidate_rows = rows_by_id(candidate_content, key)
        for row_id, current_row in current_rows.items():
            candidate_row = candidate_rows[row_id]
            current_anchors = current_row.get(anchor_field)
            candidate_anchors = candidate_row.get(anchor_field)
            if not (
                isinstance(current_anchors, list)
                and isinstance(candidate_anchors, list)
                and set(map(str, current_anchors)).intersection(
                    map(str, candidate_anchors)
                )
            ):
                raise WorkflowError(
                    f"SYSTEM_MAP candidate loses the semantic anchor for {key} {row_id}"
                )
    current_dimensions = current_content.get("dimensions")
    candidate_dimensions = candidate_content.get("dimensions")
    if isinstance(current_dimensions, dict) and isinstance(candidate_dimensions, dict):
        for dimension, values in current_dimensions.items():
            candidate_values = candidate_dimensions.get(dimension)
            if not isinstance(values, list) or not isinstance(candidate_values, list):
                raise WorkflowError(
                    f"SYSTEM_MAP candidate cannot drop dimension {dimension}"
                )
            removed_values = sorted(set(map(str, values)) - set(map(str, candidate_values)))
            if removed_values:
                raise WorkflowError(
                    f"SYSTEM_MAP candidate cannot drop {dimension} values: {removed_values}"
                )
    return {
        "candidate_path": safe_relative_path(str(candidate_value)),
        "sha256": actual_sha,
        "git_head": inspected.get("current_git_head"),
        "working_tree_sha256": inspected.get("current_working_tree_sha256"),
        "summary": inspected.get("summary"),
    }


def _validate_result(
    project: ProjectConfig,
    run_root: Path,
    manifest: dict[str, object],
    result: object,
) -> dict[str, object]:
    if not isinstance(result, dict):
        raise WorkflowError("Run result must be a JSON object")
    status = result.get("status")
    route = manifest.get("route")
    deep_spec = isinstance(route, dict) and route.get("mechanism") == "spec"
    if status not in {"completed", "blocked", "proposed"}:
        raise WorkflowError("Run result status must be completed, proposed or blocked")
    if status == "proposed" and not deep_spec:
        raise WorkflowError("Only deep design/spec can produce a proposal")
    for key in ("summary", "read_back", "review", "closure"):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError(f"Run result requires non-empty {key}")
    lessons = result.get("lessons", [])
    resolutions = result.get("lesson_resolutions", [])
    if not isinstance(lessons, list) or not isinstance(resolutions, list):
        raise WorkflowError("lessons and lesson_resolutions must be lists")
    for index, lesson in enumerate(lessons):
        if not isinstance(lesson, dict):
            raise WorkflowError(f"Lesson {index} must be a mapping")
        if lesson.get("kind") not in {"error", "correction", "successful_pattern"}:
            raise WorkflowError(f"Lesson {index} has unsupported kind")
        if lesson.get("scope", "project") not in {"global", "project"}:
            raise WorkflowError(f"Lesson {index} has unsupported scope")
        for key in ("trigger", "finding", "countermeasure", "evidence"):
            if not isinstance(lesson.get(key), str) or not str(lesson[key]).strip():
                raise WorkflowError(f"Lesson {index} requires {key}")
    for index, resolution in enumerate(resolutions):
        if (
            not isinstance(resolution, dict)
            or not isinstance(resolution.get("lesson_id"), str)
            or not isinstance(resolution.get("evidence"), str)
        ):
            raise WorkflowError(
                f"Lesson resolution {index} requires lesson_id and evidence"
            )
    feature_bundle = None
    intent = route.get("intent") if isinstance(route, dict) else None
    if status == "completed" and intent == "build":
        changed_files = result.get("changed_files")
        tests = result.get("tests")
        if not isinstance(changed_files, list) or not all(
            isinstance(item, str) and item for item in changed_files
        ):
            raise WorkflowError("Completed build requires changed_files")
        normalized_changed = [safe_relative_path(item) for item in changed_files]
        actual_changed = _changed_since_start(project, manifest)
        no_change_reason = result.get("no_change_reason")
        declared_changed = set(normalized_changed)
        if actual_changed and no_change_reason is not None:
            raise WorkflowError(
                "Build cannot use no_change_reason when Git shows actual changes"
            )
        if not actual_changed and not (
            isinstance(no_change_reason, str) and no_change_reason.strip()
        ):
            raise WorkflowError(
                "Completed build with no Git delta requires a non-empty no_change_reason"
            )
        if declared_changed != actual_changed:
            missing = sorted(actual_changed - declared_changed)
            unproven = sorted(declared_changed - actual_changed)
            raise WorkflowError(
                "Build changed_files must exactly match Git read-back; "
                f"missing={missing}, unproven={unproven}"
            )
        if not isinstance(tests, list) or not tests:
            raise WorkflowError("Completed build requires tests with actual output")
        for index, test in enumerate(tests):
            if (
                not isinstance(test, dict)
                or not isinstance(test.get("command"), str)
                or not str(test["command"]).strip()
            ):
                raise WorkflowError(f"Build test {index} requires a command")
            if test.get("exit_code") != 0:
                raise WorkflowError(f"Build test {index} has no successful exit_code")
            if not isinstance(manifest.get("execution_contract"), dict):
                output = _runtime_artifact(
                    run_root, test.get("output_path"), f"Build test {index} output"
                )
                expected_sha = test.get("output_sha256")
                actual_sha = _sha256_bytes(output.read_bytes())
                if expected_sha != actual_sha:
                    raise WorkflowError(f"Build test {index} output SHA mismatch")
        start_plan = manifest.get("assurance_plan")
        if not isinstance(start_plan, dict):
            raise WorkflowError("Build run has no assurance plan")
        current_git = git_snapshot(project.code_root, project.git_ignore_prefixes)
        current_map = load_system_map(project, current_git)
        if current_map.get("valid") is not True:
            raise WorkflowError("Build closure cannot use an invalid system map")
        actual_plan = build_assurance_plan(
            task=str(manifest.get("task", "")),
            route=route if isinstance(route, dict) else {},
            changed_paths=sorted(actual_changed),
            risk_flags=[
                str(value)
                for value in (
                    route.get("risk_signals", []) if isinstance(route, dict) else []
                )
            ],
            stack_text=read_project_text(project, project.files.stack),
            target_type=None,
        )
        actual_plan = apply_system_map_impact(
            actual_plan,
            system_map=current_map,
            changed_paths=sorted(actual_changed),
        )
        plan = merge_assurance_plans(start_plan, actual_plan)
        execution_receipts = validate_execution_bundle(
            project, run_root, manifest
        )
        validated_tests = validate_test_evidence(
            run_root=run_root,
            plan=plan,
            evidence=tests,
            execution_receipts=execution_receipts,
        )
        result["tests"] = validated_tests
        result["assurance_evidence"] = {
            "start_plan": start_plan,
            "closure_plan": actual_plan,
            "effective_plan": plan,
            "actual_changed_paths": sorted(actual_changed),
        }
        result["trace_evidence"] = {
            "task_id": manifest.get("task_id"),
            "git": _build_trace(project, manifest, actual_changed),
            "verification": [
                test for test in validated_tests
            ],
        }
        feature_bundle = _validate_feature_contract_artifact(
            run_root, manifest, result
        )
        if feature_bundle is not None:
            result["feature_contract_evidence"] = feature_bundle[1]
            result["convergence_evidence"] = _validate_convergence_artifact(
                run_root,
                manifest,
                result,
                feature_contract=feature_bundle[0],
                feature_contract_sha256=str(feature_bundle[1]["sha256"]),
                verification=validated_tests,
                changed_files=sorted(actual_changed),
                no_change_reason=(
                    no_change_reason if isinstance(no_change_reason, str) else None
                ),
            )
            result["trace_evidence"]["feature_contract"] = (
                _portable_feature_contract(feature_bundle[0], feature_bundle[1])
            )
            result["trace_evidence"]["convergence"] = result[
                "convergence_evidence"
            ]
    if status in {"completed", "proposed"} and intent == "design":
        deliverable = result.get("deliverable")
        if not isinstance(deliverable, str) or not deliverable.strip():
            raise WorkflowError("Completed design requires a deliverable summary")
        if isinstance(route, dict) and route.get("mechanism") == "spec":
            candidate = _runtime_artifact(
                run_root, result.get("spec_candidate_path"), "Spec candidate"
            )
            target_value = result.get("spec_target")
            if not isinstance(target_value, str):
                raise WorkflowError("Deep design requires spec_target")
            target = _spec_relative(project, target_value)
            active_root = PurePosixPath(project.files.specs) / "active"
            if not PurePosixPath(target).is_relative_to(active_root):
                raise WorkflowError(
                    "Deep design spec_target must be under specs/active"
                )
            if candidate.suffix.lower() != ".md":
                raise WorkflowError("Deep design spec candidate must be Markdown")
            if result.get("spec_sha256") != _sha256_bytes(candidate.read_bytes()):
                raise WorkflowError("Deep design spec candidate SHA mismatch")
            result["trace_evidence"] = _validate_design_trace(
                project, run_root, manifest, result
            )
            if status == "completed":
                result["user_approval"] = _validate_user_approval(
                    run_root, manifest, result
                )
        else:
            result["trace_evidence"] = {
                "task_id": manifest.get("task_id"),
                "spec": None,
                "adrs": [],
                "research": [],
                "references": [],
            }
        feature_bundle = _validate_feature_contract_artifact(
            run_root, manifest, result
        )
        if feature_bundle is not None:
            result["feature_contract_evidence"] = feature_bundle[1]
            result["trace_evidence"]["feature_contract"] = (
                _portable_feature_contract(feature_bundle[0], feature_bundle[1])
            )
            if isinstance(route, dict) and route.get("mechanism") == "spec":
                spec_text = candidate.read_text(encoding="utf-8")
                spec_frontmatter = _spec_frontmatter(spec_text)
                expected_contract_sha = feature_bundle[1]["sha256"]
                if spec_frontmatter.get("feature_contract_sha256") != expected_contract_sha:
                    raise WorkflowError(
                        "Deep design spec frontmatter feature_contract_sha256 must "
                        "match the reviewed Feature Contract"
                    )
    if status == "completed" and intent == "review":
        findings = result.get("findings")
        if not isinstance(findings, list):
            raise WorkflowError("Completed review requires a findings list")
        scope_sha = result.get("scope_sha256")
        context = manifest.get("context")
        expected = context.get("scope_sha256") if isinstance(context, dict) else None
        if scope_sha != expected:
            raise WorkflowError("Review result scope SHA does not match the run")
        scope_path = run_root / "scope.json"
        try:
            stored_scope = json.loads(scope_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkflowError("Stored review scope is unreadable") from error
        if not isinstance(stored_scope, dict) or stored_scope.get("sha256") != expected:
            raise WorkflowError("Stored review scope identity does not match the run")
        scope_files = stored_scope.get("files")
        if not isinstance(scope_files, list) or not scope_files:
            raise WorkflowError("Completed review requires a non-empty stored scope")
        allowed_files = {
            str(row["path"])
            for row in scope_files
            if isinstance(row, dict) and isinstance(row.get("path"), str)
        }
        severities = {"info", "low", "medium", "high", "critical"}
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                raise WorkflowError(f"Review finding {index} must be an object")
            severity = finding.get("severity")
            if severity not in severities:
                raise WorkflowError(f"Review finding {index} has invalid severity")
            file_value = finding.get("file")
            if not isinstance(file_value, str):
                raise WorkflowError(f"Review finding {index} requires a file")
            normalized_file = safe_relative_path(file_value)
            if normalized_file not in allowed_files:
                raise WorkflowError(
                    f"Review finding {index} is outside the captured scope: {normalized_file}"
                )
            line = finding.get("line")
            if isinstance(line, bool) or not isinstance(line, int) or line < 1:
                raise WorkflowError(
                    f"Review finding {index} requires a positive line number"
                )
            evidence = finding.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise WorkflowError(f"Review finding {index} requires evidence")
            confidence = finding.get("confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
            ):
                raise WorkflowError(
                    f"Review finding {index} confidence must be between 0 and 1"
                )
        request = manifest.get("review_request")
        if not isinstance(request, dict) or not isinstance(
            request.get("target_type"), str
        ):
            raise WorkflowError("Review request is missing from the run")
        current_scope = build_review_scope(
            project,
            target_type=str(request["target_type"]),
            target=(str(request["target"]) if request.get("target") else None),
            spec=(
                str(manifest["relevant_spec"])
                if manifest.get("relevant_spec")
                else None
            ),
        )
        if current_scope.get("sha256") != expected:
            raise WorkflowError("Review scope drifted after start; create a new run")
        result["functional_coverage"] = _validate_functional_coverage(
            run_root, manifest, result
        )
        result["coverage"] = _validate_review_coverage(
            run_root, manifest, result, scope_files
        )
        result["system_map_evidence"] = _validate_system_map_candidate(
            project, run_root, manifest, result
        )
        plan = manifest.get("assurance_plan")
        if not isinstance(plan, dict):
            raise WorkflowError("Review run has no assurance plan")
        result.setdefault(
            "verification_declaration_sha256",
            canonical_sha(result.get("verification", [])),
        )
        execution_receipts = validate_execution_bundle(
            project, run_root, manifest
        )
        result["verification"] = validate_test_evidence(
            run_root=run_root,
            plan=plan,
            evidence=result.get("verification", []),
            execution_receipts=execution_receipts,
        )
        result["trace_evidence"] = {
            "task_id": manifest.get("task_id"),
            "scope_sha256": expected,
            "git_head": (
                manifest.get("context", {}).get("git", {}).get("head")
                if isinstance(manifest.get("context"), dict)
                and isinstance(manifest.get("context", {}).get("git"), dict)
                else None
            ),
        }
    if status in {"completed", "proposed"}:
        result["role_evidence"] = _validate_role_evidence(
            project, run_root, manifest, result
        )
    return result


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _portable_artifact(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in ("id", "path", "sha256", "revision", "references")
        if value.get(key) is not None
    }


def _portable_trace(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {"task_id": value.get("task_id")}
    spec = _portable_artifact(value.get("spec"))
    if spec is not None:
        result["spec"] = spec
    result["adrs"] = [
        artifact
        for artifact in (_portable_artifact(item) for item in value.get("adrs", []))
        if artifact is not None
    ]
    result["research"] = [
        artifact
        for artifact in (_portable_artifact(item) for item in value.get("research", []))
        if artifact is not None
    ]
    references = value.get("references", [])
    result["references"] = references if isinstance(references, list) else []
    if isinstance(value.get("assessments"), dict):
        result["assessments"] = value["assessments"]
    if isinstance(value.get("git"), dict):
        git = value["git"]
        result["implementation"] = {
            "run_id": None,
            "base_commit": git.get("base_commit"),
            "head_commit": git.get("head_commit"),
            "commits": [
                str(commit.get("sha"))
                for commit in git.get("commits", [])
                if isinstance(commit, dict) and isinstance(commit.get("sha"), str)
            ],
            "commit_details": git.get("commits", []),
            "changed_files": git.get("changed_files", []),
        }
    if isinstance(value.get("verification"), list):
        result["verification"] = value["verification"]
    if isinstance(value.get("feature_contract"), dict):
        result["feature_contract"] = value["feature_contract"]
    if isinstance(value.get("convergence"), dict):
        result["convergence"] = value["convergence"]
    if value.get("scope_sha256") is not None:
        result["scope_sha256"] = value.get("scope_sha256")
        result["git_head"] = value.get("git_head")
    return result


def _design_publications(
    project: ProjectConfig,
    run_root: Path,
    trace: dict[str, object],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    spec = trace.get("spec")
    if isinstance(spec, dict):
        artifacts.append(spec)
    artifacts.extend(
        item for item in trace.get("research", []) if isinstance(item, dict)
    )
    artifacts.extend(item for item in trace.get("adrs", []) if isinstance(item, dict))
    for index, artifact in enumerate(artifacts):
        relative = artifact.get("path")
        candidate_relative = artifact.get("candidate_path")
        if not isinstance(relative, str) or not isinstance(candidate_relative, str):
            raise WorkflowError("Design trace artifact lacks publication paths")
        path = project.document_path(relative)
        candidate = _runtime_artifact(
            run_root, candidate_relative, f"Design publication {relative}"
        )
        content = candidate.read_bytes()
        if _sha256_bytes(content) != artifact.get("sha256"):
            raise WorkflowError(f"Design publication SHA drifted: {relative}")
        existed = path.is_file()
        before = path.read_bytes() if existed else b""
        if existed and before != content:
            raise WorkflowError(f"Approved design artifact is immutable: {relative}")
        rows.append(
            {
                "path": path,
                "relative": relative,
                "before": before,
                "after": content,
                "existed": existed,
                "snapshot": f"DESIGN-{index:02d}.preimage.md",
            }
        )
    return rows


def _system_map_publication(
    project: ProjectConfig,
    run_root: Path,
    result: dict[str, object],
) -> list[dict[str, object]]:
    evidence = result.get("system_map_evidence")
    if not isinstance(evidence, dict):
        return []
    candidate = _runtime_artifact(
        run_root, evidence.get("candidate_path"), "System map publication"
    )
    content = candidate.read_bytes()
    if _sha256_bytes(content) != evidence.get("sha256"):
        raise WorkflowError("System map publication SHA drifted")
    target = project.document_path(project.files.system_map)
    before = target.read_bytes() if target.is_file() else b""
    return [
        {
            "path": target,
            "relative": project.files.system_map,
            "before": before,
            "after": content,
            "existed": target.is_file(),
            "snapshot": "SYSTEM_MAP.preimage.yaml",
        }
    ]


def _ensure_trace_task(
    state: dict[str, object],
    *,
    task_id: str,
    title: str,
    preferred_stage_id: str | None,
) -> dict[str, object] | None:
    if state.get("schema_version") == 1:
        return None
    existing = task_index(state).get(task_id)
    if existing is not None:
        return existing["task"]
    stages = state.get("stages")
    if not isinstance(stages, list):
        raise WorkflowError("STATE stages are malformed during closure")
    target_stage: dict[str, object] | None = None
    if preferred_stage_id:
        target_stage = next(
            (
                stage
                for stage in stages
                if isinstance(stage, dict) and stage.get("id") == preferred_stage_id
            ),
            None,
        )
    if target_stage is None and state.get("profile") == "roadmap":
        focus = state.get("focus")
        focus_stage = focus.get("stage_id") if isinstance(focus, dict) else None
        target_stage = next(
            (
                stage
                for stage in stages
                if isinstance(stage, dict) and stage.get("id") == focus_stage
            ),
            None,
        )
    if target_stage is None:
        target_stage = next(
            (
                stage
                for stage in stages
                if isinstance(stage, dict) and stage.get("id") == "frontier"
            ),
            None,
        )
    if target_stage is None:
        target_stage = {
            "id": "frontier",
            "title": "Frontier",
            "status": "in_progress",
            "exit_criteria": [],
            "tasks": [],
        }
        stages.append(target_stage)
    tasks = target_stage.setdefault("tasks", [])
    if not isinstance(tasks, list):
        raise WorkflowError("STATE target stage tasks are malformed")
    task = {
        "id": task_id,
        "title": title,
        "status": "in_progress",
        "priority": len(tasks) + 1,
        "depends_on": [],
        "spec": None,
        "adr": [],
        "references": [],
    }
    tasks.append(task)
    return task


def _allowed_closure_target(project: ProjectConfig, relative: str) -> bool:
    candidate = PurePosixPath(safe_relative_path(relative))
    exact = {
        PurePosixPath(project.files.state),
        PurePosixPath(project.files.history),
        PurePosixPath(project.files.system_map),
    }
    if candidate in exact:
        return True
    return candidate.suffix.lower() == ".md" and any(
        candidate.is_relative_to(PurePosixPath(root))
        for root in (project.files.specs, project.files.adr, project.files.knowledge)
    )


def _recover_incomplete_active_closure(
    project: ProjectConfig,
    *,
    run_id: str,
    manifest: dict[str, object],
) -> bool:
    """Restore a prepared project transaction before input identity validation."""
    journal_path = _run_root(project, run_id) / "closure-journal.json"
    if not journal_path.is_file():
        return False
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Closure recovery journal is unreadable: {run_id}") from error
    if not isinstance(journal, dict):
        raise WorkflowError(f"Closure recovery journal is malformed: {run_id}")
    phase = journal.get("phase")
    if phase in {"run-committed", "rolled-back"}:
        return False
    if journal.get("schema_version") != 2 or phase not in {
        "prepared",
        "project-committed",
    }:
        raise WorkflowError(f"Closure recovery journal is invalid: {run_id}")
    expected_roots = {
        "framework": str(project.framework_root.resolve(strict=True)),
        "docs": str(project.docs_root.resolve(strict=True)),
        "code": str(project.code_root.resolve(strict=True)),
        "runtime": str(project.runtime_root.resolve(strict=True)),
    }
    if journal.get("project") != project.project_id or journal.get("roots") != expected_roots:
        raise WorkflowError(
            "Closure recovery journal project/root identity mismatch; refusing writes"
        )
    rows = journal.get("targets")
    if not isinstance(rows, list) or not rows:
        raise WorkflowError("Closure recovery journal has no target inventory")
    snapshot_root = _run_root(project, run_id) / "closure-preimage"
    targets: list[tuple[dict[str, object], Path, Path, bytes]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise WorkflowError(f"Closure recovery target {index} is malformed")
        relative_value = row.get("relative")
        snapshot_value = row.get("snapshot")
        if not isinstance(relative_value, str) or not _allowed_closure_target(
            project, relative_value
        ):
            raise WorkflowError(f"Closure recovery target {index} is outside policy")
        relative = safe_relative_path(relative_value)
        if relative in seen:
            raise WorkflowError(f"Duplicate closure recovery target: {relative}")
        seen.add(relative)
        if (
            not isinstance(snapshot_value, str)
            or Path(snapshot_value).name != snapshot_value
            or snapshot_value in {"", ".", ".."}
        ):
            raise WorkflowError(f"Closure recovery snapshot {index} is unsafe")
        target = project.document_path(relative)
        snapshot = snapshot_root / snapshot_value
        try:
            before = snapshot.read_bytes()
        except OSError as error:
            raise WorkflowError(
                f"Closure recovery preimage is unreadable: {snapshot_value}"
            ) from error
        if row.get("before_sha256") != _sha256_bytes(before):
            raise WorkflowError(
                f"Closure recovery preimage SHA mismatch: {snapshot_value}"
            )
        if not isinstance(row.get("after_sha256"), str) or row.get("existed") not in {
            True,
            False,
        }:
            raise WorkflowError(f"Closure recovery target {index} lacks identity")
        targets.append((row, target, snapshot, before))
    required = {project.files.state, project.files.history}
    if not required.issubset(seen):
        raise WorkflowError("Closure recovery journal lacks STATE/HISTORY targets")

    manifest_status = manifest.get("status")
    if manifest_status not in {"started", "awaiting_user_approval"}:
        if phase != "project-committed":
            raise WorkflowError(
                "Closed run has a non-committed closure recovery journal"
            )
        for row, target, _snapshot, _before in targets:
            if not target.is_file() or _sha256_bytes(target.read_bytes()) != row.get(
                "after_sha256"
            ):
                raise WorkflowError(
                    f"Committed closure target drifted before recovery: {row['relative']}"
                )
        if manifest.get("result_sha256") != journal.get("result_sha256"):
            raise WorkflowError("Committed closure journal/result identity mismatch")
        atomic_write_json(
            journal_path,
            {
                **journal,
                "phase": "run-committed",
                "recovered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
        )
        return True

    lock_path = project.runtime_root / "locks" / "project-state.lock"
    with exclusive_lock(lock_path, timeout_seconds=120.0):
        # A recovery retry may itself die between targets. Accept only the exact
        # preimage or the exact intended postimage; never overwrite later edits.
        for row, target, _snapshot, _before in targets:
            if target.is_file():
                current_sha = _sha256_bytes(target.read_bytes())
                if current_sha not in {
                    row.get("before_sha256"),
                    row.get("after_sha256"),
                }:
                    raise WorkflowError(
                        f"Closure recovery target has unrelated changes: {row['relative']}"
                    )
            elif row.get("existed") is True:
                raise WorkflowError(
                    f"Closure recovery target unexpectedly disappeared: {row['relative']}"
                )
        for row, target, _snapshot, before in targets:
            if row.get("existed") is True:
                atomic_write_bytes(target, before)
            elif target.exists():
                target.unlink()
        for row, target, _snapshot, before in targets:
            if row.get("existed") is True:
                if target.read_bytes() != before:
                    raise WorkflowError(
                        f"Closure recovery read-back failed: {row['relative']}"
                    )
            elif target.exists():
                raise WorkflowError(
                    f"Closure recovery delete read-back failed: {row['relative']}"
                )
        atomic_write_json(
            journal_path,
            {
                **journal,
                "phase": "rolled-back",
                "recovered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
        )
    return True


def _active_project_closure(
    project: ProjectConfig,
    *,
    run_id: str,
    manifest: dict[str, object],
    result: dict[str, object],
) -> list[dict[str, object]]:
    state_path = project.document_path(project.files.state)
    history_path = project.document_path(project.files.history)
    lock_path = project.runtime_root / "locks" / "project-state.lock"
    with exclusive_lock(lock_path, timeout_seconds=120.0):
        history_status = verify_history(project)
        if history_status.get("ok") is not True:
            raise WorkflowError("Cannot close active run with an invalid history chain")
        events = history_events(project)
        existing_events = [event for event in events if event.get("run_id") == run_id]
        if len(existing_events) > 1:
            raise WorkflowError(f"History contains duplicate run events: {run_id}")
        state_before = state_path.read_bytes()
        history_before = history_path.read_bytes()
        snapshot_root = _run_root(project, run_id) / "closure-preimage"
        atomic_write_bytes(snapshot_root / "STATE.yaml", state_before)
        atomic_write_bytes(snapshot_root / "HISTORY.jsonl", history_before)
        state = parse_project_state(
            state_before.decode("utf-8-sig"),
            project_id=project.project_id,
            expected_profile=project.state_profile,
        )
        checkpoint = state.get("history_checkpoint")
        checkpoint_matches = (
            isinstance(checkpoint, dict)
            and checkpoint.get("sequence") == history_status.get("events")
            and checkpoint.get("event_sha256") == history_status.get("head_sha256")
        )
        recovering_existing_head = bool(
            existing_events and events and existing_events[0] is events[-1]
        )
        if not checkpoint_matches and not recovering_existing_head:
            raise WorkflowError(
                "STATE/HISTORY checkpoint mismatch; recover the last incomplete run "
                "before closing another active run"
            )
        frontier = state_current(state)
        route = manifest.get("route")
        if not isinstance(route, dict):
            raise WorkflowError("Run route is missing during active closure")
        task = str(manifest.get("task", "")).strip()
        task_id = str(manifest.get("task_id"))
        selected = manifest.get("task_selection")
        preferred_stage_id = (
            str(selected.get("stage_id"))
            if isinstance(selected, dict) and selected.get("stage_id")
            else None
        )
        task_row = _ensure_trace_task(
            state,
            task_id=task_id,
            title=task,
            preferred_stage_id=preferred_stage_id,
        )
        existing_trace = (
            task_row.get("trace", {})
            if isinstance(task_row, dict) and isinstance(task_row.get("trace"), dict)
            else frontier.get("trace", {})
            if isinstance(frontier.get("trace"), dict)
            else {}
        )
        result_trace = _portable_trace(result.get("trace_evidence"))
        trace: dict[str, object] = dict(existing_trace)
        for key, value in result_trace.items():
            if key == "implementation" and isinstance(value, dict):
                value = {**value, "run_id": run_id}
            if value not in (None, [], {}):
                trace[key] = value
        trace["task_id"] = task_id
        relevant_spec = manifest.get("relevant_spec")
        if "spec" not in trace and isinstance(relevant_spec, str):
            spec_path = project.document_path(relevant_spec)
            spec_text = spec_path.read_text(encoding="utf-8-sig")
            metadata = _spec_frontmatter(spec_text)
            trace["spec"] = {
                "id": str(metadata.get("id") or PurePosixPath(relevant_spec).stem),
                "path": relevant_spec,
                "sha256": _sha256_bytes(spec_path.read_bytes()),
                "revision": int(metadata.get("revision", 1)),
            }
        effective_spec = (
            str(trace["spec"].get("path"))
            if isinstance(trace.get("spec"), dict)
            and isinstance(trace["spec"].get("path"), str)
            else None
        )
        publications = (
            _design_publications(
                project,
                _run_root(project, run_id),
                result.get("trace_evidence", {}),
            )
            if result["status"] == "completed" and route.get("mechanism") == "spec"
            else []
        )
        if result["status"] == "completed" and route.get("intent") == "review":
            publications.extend(
                _system_map_publication(project, _run_root(project, run_id), result)
            )
        for publication in publications:
            atomic_write_bytes(
                snapshot_root / str(publication["snapshot"]),
                bytes(publication["before"]),
            )

        if existing_events:
            event = existing_events[0]
            existing_result = event.get("result")
            if not isinstance(existing_result, dict) or existing_result.get(
                "result_sha256"
            ) != canonical_sha(result):
                raise WorkflowError("Existing history event has a different result SHA")
            if event is not events[-1]:
                return [
                    {
                        "path": project.files.history,
                        "before_sha256": _sha256_bytes(history_before),
                        "after_sha256": _sha256_bytes(history_before),
                        "read_back_sha256": _sha256_bytes(history_path.read_bytes()),
                        "recovered_existing_run": True,
                    }
                ]
            history_after = history_before
            task_id = str(event["task_id"])
            timestamp = str(event["timestamp"])
            event_git_head = str(event["git_head"])
            event_result = event.get("result")
            if isinstance(event_result, dict) and isinstance(
                event_result.get("trace"), dict
            ):
                trace = dict(event_result["trace"])
                effective_spec = (
                    str(trace["spec"].get("path"))
                    if isinstance(trace.get("spec"), dict)
                    else None
                )
        else:
            git = git_snapshot(project.code_root, project.git_ignore_prefixes)
            implementation = trace.get("implementation")
            event_git_head = (
                str(implementation.get("head_commit"))
                if isinstance(implementation, dict)
                and isinstance(implementation.get("head_commit"), str)
                else str(git["head"])
            )
            timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            refs = [f"run://{project.project_id}/{run_id}"]
            if isinstance(trace.get("spec"), dict):
                refs.append(f"spec://{project.project_id}/{trace['spec'].get('id')}")
            refs.extend(
                f"adr://{project.project_id}/{row.get('id')}"
                for row in trace.get("adrs", [])
                if isinstance(row, dict)
            )
            refs.extend(
                f"research://{project.project_id}/{row.get('id')}"
                for row in trace.get("research", [])
                if isinstance(row, dict)
            )
            if isinstance(implementation, dict):
                refs.extend(
                    f"git://{project.project_id}/{commit}"
                    for commit in implementation.get("commits", [])
                )
            map_evidence = result.get("system_map_evidence")
            if isinstance(map_evidence, dict):
                refs.append(
                    f"system-map://{project.project_id}/{map_evidence.get('sha256')}"
                )
            functional_coverage = result.get("functional_coverage")
            if isinstance(functional_coverage, dict):
                refs.append(
                    "functional-coverage://"
                    f"{project.project_id}/{functional_coverage.get('artifact_sha256')}"
                )
            event = {
                "schema_version": 1,
                "sequence": int(history_status.get("events", 0)) + 1,
                "timestamp": timestamp,
                "type": (
                    "task_completed"
                    if result["status"] == "completed"
                    else "task_blocked"
                ),
                "project_id": project.project_id,
                "run_id": run_id,
                "task_id": task_id,
                "git_head": event_git_head,
                "previous_event_sha256": history_status.get("head_sha256"),
                "refs": refs,
                "result": {
                    "summary": result["summary"],
                    "intent": route.get("intent"),
                    "mode": route.get("mode"),
                    "read_back": result["read_back"],
                    "review": result["review"],
                    "closure": result["closure"],
                    "spec": effective_spec,
                    "trace": trace,
                    "roles": result.get("role_evidence", []),
                    "functional_coverage": result.get("functional_coverage"),
                    "user_approval": result.get("user_approval"),
                    "system_map": result.get("system_map_evidence"),
                    "result_sha256": canonical_sha(result),
                },
            }
            event["event_sha256"] = canonical_sha(event)
            event_line = (
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            history_base = history_before
            if history_base and not history_base.endswith(b"\n"):
                history_base += b"\n"
            history_after = history_base + event_line

        state_trace: dict[str, object] = {
            key: trace.get(key)
            for key in ("spec", "adrs", "research", "references", "assessments")
            if trace.get(key) not in (None, [], {})
        }
        implementation = trace.get("implementation")
        if isinstance(implementation, dict):
            state_trace["implementation"] = {
                "run_id": implementation.get("run_id"),
                "base_commit": implementation.get("base_commit"),
                "head_commit": implementation.get("head_commit"),
                "commits": implementation.get("commits", []),
                "changed_files": [
                    str(row.get("path"))
                    for row in implementation.get("changed_files", [])
                    if isinstance(row, dict) and isinstance(row.get("path"), str)
                ],
            }
        state_trace["history"] = {
            "sequence": event["sequence"],
            "event_sha256": event["event_sha256"],
        }
        adr_ids = [
            str(row.get("id"))
            for row in trace.get("adrs", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        ]
        reference_ids = [
            str(row.get("id"))
            for row in trace.get("references", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        ]
        if state.get("schema_version") == 1:
            state["frontier"] = {
                "task_id": task_id,
                "task_summary": task,
                "intent": route.get("intent"),
                "mode": route.get("mode"),
                "stage": result["status"],
                "next_action": None
                if result["status"] == "completed"
                else result["closure"],
                "spec": effective_spec,
                "adr": adr_ids,
                "references": reference_ids,
                "trace": state_trace,
                "git_head": event_git_head,
            }
        else:
            roadmap_task = task_index(state).get(task_id)
            if isinstance(task_row, dict):
                if result["status"] != "completed":
                    task_row["status"] = "blocked"
                elif route.get("intent") == "build":
                    task_row["status"] = "done"
                    task_row["commit"] = event_git_head
                    task_row["completed_at"] = timestamp
                elif route.get("intent") == "design" and task_row.get("status") not in {
                    "done",
                    "dropped",
                }:
                    task_row["status"] = "ready"
                    if effective_spec is not None:
                        task_row["spec"] = effective_spec
                elif route.get("intent") == "review":
                    task_row["status"] = "done"
                    task_row["completed_at"] = timestamp
                task_row["adr"] = adr_ids
                task_row["references"] = reference_ids
                task_row["trace"] = state_trace
            if result["status"] == "completed":
                next_task = select_next_task(state)
                state["current"] = {
                    "task_id": None,
                    "task_summary": None,
                    "stage_id": None,
                    "intent": None,
                    "mode": None,
                    "status": None,
                    "next_action": (
                        str(next_task.get("task"))
                        if isinstance(next_task, dict) and not next_task.get("blocked")
                        else None
                    ),
                    "spec": None,
                    "adr": [],
                    "references": [],
                    "git_head": event_git_head,
                }
            else:
                state["current"] = {
                    "task_id": task_id,
                    "task_summary": task,
                    "stage_id": (
                        roadmap_task.get("stage_id")
                        if roadmap_task is not None
                        else None
                    ),
                    "intent": route.get("intent"),
                    "mode": route.get("mode"),
                    "status": "blocked",
                    "next_action": result["closure"],
                    "spec": effective_spec,
                    "adr": adr_ids,
                    "references": reference_ids,
                    "git_head": event_git_head,
                }
        state["last_verified"] = {
            "kind": "run_closure",
            "result": {
                "status": result["status"],
                "read_back": result["read_back"],
                "review": result["review"],
            },
            "refs": [f"run://{project.project_id}/{run_id}"],
        }
        if result["status"] == "completed":
            state["last_completed"] = {
                "task_id": task_id,
                "summary": result["summary"],
                "git_head": event_git_head,
                "completed_at": timestamp,
            }
        state["history_checkpoint"] = {
            "sequence": event["sequence"],
            "event_sha256": event["event_sha256"],
        }
        state_after = yaml.safe_dump(
            state,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).encode("utf-8")
        if len(state_after) > project.state_budget_bytes:
            raise WorkflowError(
                f"Active STATE would exceed its compact budget: {len(state_after)}>{project.state_budget_bytes}"
            )
        journal_targets: list[dict[str, object]] = [
            {
                "kind": "history",
                "relative": project.files.history,
                "snapshot": "HISTORY.jsonl",
                "existed": True,
                "before_sha256": _sha256_bytes(history_before),
                "after_sha256": _sha256_bytes(history_after),
            },
            {
                "kind": "state",
                "relative": project.files.state,
                "snapshot": "STATE.yaml",
                "existed": True,
                "before_sha256": _sha256_bytes(state_before),
                "after_sha256": _sha256_bytes(state_after),
            },
        ]
        journal_targets.extend(
            {
                "kind": "publication",
                "relative": publication["relative"],
                "snapshot": publication["snapshot"],
                "existed": publication["existed"],
                "before_sha256": _sha256_bytes(bytes(publication["before"])),
                "after_sha256": _sha256_bytes(bytes(publication["after"])),
            }
            for publication in publications
        )
        journal = {
            "schema_version": 2,
            "project": project.project_id,
            "run_id": run_id,
            "phase": "prepared",
            "event_sha256": event["event_sha256"],
            "result_sha256": canonical_sha(result),
            "roots": {
                "framework": str(project.framework_root.resolve(strict=True)),
                "docs": str(project.docs_root.resolve(strict=True)),
                "code": str(project.code_root.resolve(strict=True)),
                "runtime": str(project.runtime_root.resolve(strict=True)),
            },
            "targets": journal_targets,
        }
        try:
            journal_path = _run_root(project, run_id) / "closure-journal.json"
            atomic_write_json(journal_path, journal)
            for publication in publications:
                atomic_write_bytes(
                    Path(publication["path"]), bytes(publication["after"])
                )
            if history_after != history_before:
                atomic_write_bytes(history_path, history_after)
            atomic_write_bytes(state_path, state_after)
            if (
                history_path.read_bytes() != history_after
                or state_path.read_bytes() != state_after
                or any(
                    Path(publication["path"]).read_bytes()
                    != bytes(publication["after"])
                    for publication in publications
                )
            ):
                raise WorkflowError(
                    "Active trace document/STATE/HISTORY read-back mismatch"
                )
            verified = verify_history(project)
            if (
                verified.get("ok") is not True
                or verified.get("head_sha256") != event["event_sha256"]
            ):
                raise WorkflowError("Active HISTORY verification failed after write")
            atomic_write_json(
                journal_path,
                {
                    **journal,
                    "phase": "project-committed",
                },
            )
        except Exception:
            atomic_write_bytes(history_path, history_before)
            atomic_write_bytes(state_path, state_before)
            for publication in publications:
                path = Path(publication["path"])
                if publication["existed"]:
                    atomic_write_bytes(path, bytes(publication["before"]))
                elif path.exists():
                    path.unlink()
            if "journal_path" in locals() and journal_path.is_file():
                atomic_write_json(journal_path, {**journal, "phase": "rolled-back"})
            raise
        writes: list[dict[str, object]] = [
            {
                "path": project.files.history,
                "before_sha256": _sha256_bytes(history_before),
                "after_sha256": _sha256_bytes(history_after),
                "read_back_sha256": _sha256_bytes(history_path.read_bytes()),
            },
            {
                "path": project.files.state,
                "before_sha256": _sha256_bytes(state_before),
                "after_sha256": _sha256_bytes(state_after),
                "read_back_sha256": _sha256_bytes(state_path.read_bytes()),
            },
        ]
        for publication in publications:
            path = Path(publication["path"])
            writes.append(
                {
                    "path": publication["relative"],
                    "before_sha256": _sha256_bytes(bytes(publication["before"])),
                    "after_sha256": _sha256_bytes(bytes(publication["after"])),
                    "read_back_sha256": _sha256_bytes(path.read_bytes()),
                }
            )
        return writes


def _sync_project_backlog(
    project: ProjectConfig,
    *,
    actor_id: str | None,
    device_id: str | None,
) -> dict[str, object] | None:
    if project.framework_version not in {"1.5.0", "1.5.1", "1.5.2", "1.5.3", "1.5.4", "1.5.5"}:
        return None
    from aria.access import load_access_policy
    from aria.backlog import load_backlog, sync_backlog

    if load_access_policy(project).get("status") != "active":
        return None
    snapshot = git_snapshot(project.code_root, project.git_ignore_prefixes)
    return sync_backlog(
        project,
        expected_revision=int(load_backlog(project)["revision"]),
        actor_id=actor_id,
        device_id=device_id,
        version=project.framework_version,
        branch=snapshot.get("branch"),
    )


def close_project_run(
    project: ProjectConfig,
    *,
    run_id: str,
    result_path: Path,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    try:
        raw_result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Run result is unreadable: {result_path}") from error
    if isinstance(raw_result, dict):
        raw_result.pop("verification_declaration_sha256", None)
    run_root = _run_root(project, run_id)
    stored_result = run_root / "result.json"
    lock = project.runtime_root / "locks" / f"{run_id}.lock"
    # Final closure intentionally performs two complete evidence/Git read-backs.
    # On a large repository that can legitimately outlive the generic 5-second
    # lock budget.  A duplicate closer must wait and observe "already closed",
    # not fail merely because the first verifier is still doing useful work.
    with exclusive_lock(lock, timeout_seconds=120.0):
        manifest = read_project_run(project, run_id)
        manifest = _recover_feature_contract_amendment_locked(
            project, run_id, manifest
        )
        if project.mode == "active":
            _recover_incomplete_active_closure(
                project,
                run_id=run_id,
                manifest=manifest,
            )
            manifest = read_project_run(project, run_id)
        if manifest.get("managed_lifecycle") is True and manifest.get("status") in {
            "completed",
            "blocked",
            "proposed",
        }:
            _commit_managed_lifecycle_terminal(
                project, manifest, str(manifest.get("status"))
            )
            backlog_sync = _sync_project_backlog(
                project, actor_id=actor_id, device_id=device_id
            )
            return {
                "ok": True,
                "run_id": run_id,
                "status": manifest.get("status"),
                "result_path": manifest.get("result_path"),
                "result_sha256": manifest.get("result_sha256"),
                "project_writes": manifest.get("product_writes", []),
                "backlog_sync": backlog_sync,
                "idempotent": True,
                "note": "Managed lifecycle terminal state reconciled from the closed run",
            }
        _require_managed_lifecycle_phase(project, manifest, "converge")
        proposal_request = (
            isinstance(raw_result, dict) and raw_result.get("status") == "proposed"
        )
        allowed_statuses = (
            {"started", "awaiting_user_approval"} if proposal_request else {"started"}
        )
        if manifest.get("status") not in allowed_statuses:
            raise WorkflowError(f"Run is already closed: {run_id}")
        requested_blocked = (
            isinstance(raw_result, dict) and raw_result.get("status") == "blocked"
        )
        stale_reason = _validate_run_identity(
            project, manifest, allow_stale=requested_blocked
        )
        result = _validate_result(project, run_root, manifest, raw_result)
        if result["status"] in {"completed", "proposed"}:
            # Re-read the final Git/scope/output/role subject once more after all
            # validators have run. This closes the practical TOCTOU window where
            # a reviewer or test artifact changes immediately after its first read.
            result = _validate_result(project, run_root, manifest, result)
        if result["status"] == "proposed":
            proposal_path = run_root / "proposal.json"
            atomic_write_json(proposal_path, result)
            proposal_read_back = json.loads(proposal_path.read_text(encoding="utf-8"))
            if proposal_read_back != result:
                raise WorkflowError(f"Proposal read-back mismatch: {run_id}")
            manifest.pop("user_approval", None)
            manifest["status"] = "awaiting_user_approval"
            manifest["proposal_path"] = str(proposal_path)
            manifest["proposal_sha256"] = canonical_sha(result)
            manifest["proposal_spec_sha256"] = result.get("spec_sha256")
            manifest["proposal_updated_at"] = (
                datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )
            manifest["role_evidence"] = result.get("role_evidence", [])
            atomic_write_json(run_root / "manifest.json", manifest)
            final_proposal = read_project_run(project, run_id)
            _commit_managed_lifecycle_terminal(
                project, final_proposal, str(result["status"])
            )
            return {
                "ok": True,
                "run_id": run_id,
                "status": final_proposal["status"],
                "proposal_path": str(proposal_path),
                "proposal_sha256": final_proposal["proposal_sha256"],
                "spec_candidate_path": str(
                    _runtime_artifact(
                        run_root,
                        result.get("spec_candidate_path"),
                        "Spec candidate",
                    )
                ),
                "spec_sha256": result.get("spec_sha256"),
                "project_writes": [],
                "note": (
                    "Proposal is verified and waiting for exact user approval; "
                    "project documents and code were not changed"
                ),
            }
        atomic_write_json(stored_result, result)
        result_read_back = json.loads(stored_result.read_text(encoding="utf-8"))
        if result_read_back != result:
            raise WorkflowError(f"Result read-back mismatch: {run_id}")
        project_writes = (
            _active_project_closure(
                project,
                run_id=run_id,
                manifest=manifest,
                result=result,
            )
            if project.mode == "active" and stale_reason is None
            else []
        )
        lesson_events = LessonStore(
            project.framework_root, project.runtime_root
        ).append(
            project_id=project.project_id,
            run_id=run_id,
            lessons=result.get("lessons", []),
            resolutions=result.get("lesson_resolutions", []),
        )
        manifest["status"] = result["status"]
        manifest["closed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        manifest["result_path"] = str(stored_result)
        manifest["result_sha256"] = canonical_sha(result)
        manifest["state_updated"] = bool(project_writes)
        manifest["history_updated"] = bool(project_writes)
        manifest["product_writes"] = project_writes
        manifest["behavior_lessons_appended"] = [
            str(event.get("lesson_id")) for event in lesson_events
        ]
        manifest["role_evidence"] = result.get("role_evidence", [])
        manifest["user_approval"] = result.get(
            "user_approval", manifest.get("user_approval")
        )
        manifest["context_stale_reason"] = stale_reason
        atomic_write_json(run_root / "manifest.json", manifest)
        _commit_managed_lifecycle_terminal(
            project, manifest, str(result["status"])
        )
        journal_path = run_root / "closure-journal.json"
        if project.mode == "active" and stale_reason is None and journal_path.is_file():
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise WorkflowError(
                    f"Closure journal is unreadable after project commit: {run_id}"
                ) from error
            if not isinstance(journal, dict) or journal.get("phase") != "project-committed":
                raise WorkflowError(
                    f"Closure journal is not project-committed: {run_id}"
                )
            atomic_write_json(
                journal_path,
                {
                    **journal,
                    "phase": "run-committed",
                    "result_sha256": manifest["result_sha256"],
                },
            )
    final = read_project_run(project, run_id)
    backlog_sync = _sync_project_backlog(
        project, actor_id=actor_id, device_id=device_id
    )
    return {
        "ok": True,
        "run_id": run_id,
        "status": final["status"],
        "result_path": str(stored_result),
        "result_sha256": final["result_sha256"],
        "project_writes": final["product_writes"],
        "backlog_sync": backlog_sync,
        "note": (
            f"Run closed as blocked on stale context: {final['context_stale_reason']}"
            if final.get("context_stale_reason")
            else (
                "Project mode is shadow; STATE and HISTORY were not changed"
                if project.mode == "shadow"
                else "Active project state was updated with recoverable read-back"
            )
        ),
    }


def approve_project_spec(
    project: ProjectConfig, *, run_id: str, approval_path: Path
) -> dict[str, object]:
    try:
        raw_approval = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkflowError(f"User approval is unreadable: {approval_path}") from error
    if not isinstance(raw_approval, dict):
        raise WorkflowError("User approval must be a JSON object")
    run_root = _run_root(project, run_id)
    lock = project.runtime_root / "locks" / f"{run_id}.lock"
    approved_result_path = run_root / "approved-result-input.json"
    with exclusive_lock(lock):
        manifest = read_project_run(project, run_id)
        if manifest.get("status") != "awaiting_user_approval":
            raise WorkflowError(f"Run is not waiting for user approval: {run_id}")
        stale_reason = _validate_run_identity(project, manifest)
        if stale_reason is not None:
            raise WorkflowError(stale_reason)
        proposal_path_value = manifest.get("proposal_path")
        if not isinstance(proposal_path_value, str):
            raise WorkflowError("Run proposal path is missing")
        proposal_path = Path(proposal_path_value)
        if proposal_path.parent != run_root or not proposal_path.is_file():
            raise WorkflowError("Run proposal artifact is missing or outside the run")
        try:
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkflowError("Run proposal artifact is unreadable") from error
        proposal_sha = canonical_sha(proposal)
        expected = {
            "decision": "approved",
            "actor": "user",
            "proposal_sha256": manifest.get("proposal_sha256"),
            "spec_sha256": manifest.get("proposal_spec_sha256"),
        }
        for key, value in expected.items():
            if raw_approval.get(key) != value:
                raise WorkflowError(f"User approval {key} mismatch")
        if proposal_sha != manifest.get("proposal_sha256"):
            raise WorkflowError("Stored proposal SHA mismatch")
        statement = raw_approval.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise WorkflowError("User approval requires a non-empty statement")
        approval_payload = {
            "schema_version": 1,
            "run_id": run_id,
            "task_id": manifest.get("task_id"),
            "decision": "approved",
            "actor": "user",
            "statement": statement.strip(),
            "proposal_sha256": proposal_sha,
            "spec_sha256": manifest.get("proposal_spec_sha256"),
            "approved_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        approval_artifact = run_root / "outputs" / "user-approval.json"
        atomic_write_json(approval_artifact, approval_payload)
        approval_sha = _sha256_bytes(approval_artifact.read_bytes())
        approval_relative = approval_artifact.relative_to(run_root).as_posix()
        manifest["user_approval"] = {
            "artifact_path": approval_relative,
            "artifact_sha256": approval_sha,
        }
        manifest["status"] = "started"
        atomic_write_json(run_root / "manifest.json", manifest)
        if not isinstance(proposal, dict):
            raise WorkflowError("Stored proposal must be a JSON object")
        approved_result = dict(proposal)
        approved_result["status"] = "completed"
        atomic_write_json(approved_result_path, approved_result)
    return close_project_run(project, run_id=run_id, result_path=approved_result_path)


def _task_trace_status(
    state: dict[str, object],
    events: list[dict[str, object]],
    task_id: str | None,
) -> dict[str, object] | None:
    selected_id = task_id
    if selected_id is None:
        current = state_current(state)
        if isinstance(current.get("task_id"), str):
            selected_id = str(current["task_id"])
        else:
            last_completed = state.get("last_completed")
            if isinstance(last_completed, dict) and isinstance(
                last_completed.get("task_id"), str
            ):
                selected_id = str(last_completed["task_id"])
    if selected_id is None:
        return None
    row = task_index(state).get(selected_id)
    task = row.get("task") if isinstance(row, dict) else None
    stage_id = row.get("stage_id") if isinstance(row, dict) else None
    if task is None and state_profile(state) == "frontier":
        frontier = state_current(state)
        if frontier.get("task_id") == selected_id:
            task = frontier
            stage_id = frontier.get("stage")
    matched = [event for event in events if event.get("task_id") == selected_id]
    return {
        "task_id": selected_id,
        "stage_id": stage_id,
        "state": task,
        "history_events": [
            {
                "sequence": event.get("sequence"),
                "timestamp": event.get("timestamp"),
                "type": event.get("type"),
                "run_id": event.get("run_id"),
                "git_head": event.get("git_head"),
                "refs": event.get("refs", []),
                "trace": (
                    event.get("result", {}).get("trace")
                    if isinstance(event.get("result"), dict)
                    else None
                ),
                "roles": (
                    event.get("result", {}).get("roles", [])
                    if isinstance(event.get("result"), dict)
                    else []
                ),
                "user_approval": (
                    event.get("result", {}).get("user_approval")
                    if isinstance(event.get("result"), dict)
                    else None
                ),
                "event_sha256": event.get("event_sha256"),
            }
            for event in matched
        ],
    }


def project_status(
    project: ProjectConfig, *, task_id: str | None = None
) -> dict[str, object]:
    doctor = run_project_doctor(project)
    state, _ = _state_snapshot(project)
    current = state_current(state)
    runs_root = project.runtime_root / "runs"
    runs: list[dict[str, object]] = []
    if runs_root.is_dir():
        for path in sorted(runs_root.glob("*/manifest.json"), reverse=True)[:10]:
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict):
                runs.append(
                    {
                        "run_id": manifest.get("run_id"),
                        "status": manifest.get("status"),
                        "task": manifest.get("task"),
                        "route": manifest.get("route"),
                    }
                )
    git = git_snapshot(project.code_root, project.git_ignore_prefixes)
    events = (
        history_events(project) if verify_history(project).get("ok") is True else []
    )
    lessons = LessonStore(project.framework_root, project.runtime_root).snapshot(
        project_id=project.project_id,
        task=(task_id or str(current.get("task_summary") or "project status")),
    )
    return {
        "schema_version": 1,
        "ok": doctor.get("ok") is True,
        "project": project.project_id,
        "mode": project.mode,
        "state_profile": state_profile(state),
        "current": current,
        "frontier": current,
        "roadmap": roadmap_overview(state),
        "git": git,
        "history": verify_history(project),
        "trace": _task_trace_status(state, events, task_id),
        "behavior_lessons": lessons,
        "recent_runs": runs,
        "doctor": doctor,
    }
