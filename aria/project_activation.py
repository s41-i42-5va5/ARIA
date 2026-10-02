from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aria.errors import WorkflowError
from aria.integrity import engine_state
from aria.io import atomic_write_bytes, atomic_write_json, exclusive_lock
from aria.project import (
    ProjectConfig,
    canonical_sha,
    git_snapshot,
    load_project,
    run_project_doctor,
    verify_history,
)
from aria.project_state import parse_project_state
from aria.registry import read_registry, render_registry
from aria.simple_run import close_project_run, start_project_run


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _doctor_or_raise(project: ProjectConfig, label: str) -> dict[str, object]:
    result = run_project_doctor(project)
    if result.get("ok") is not True:
        failed = [row["id"] for row in result.get("checks", []) if not row.get("ok")]
        raise WorkflowError(f"{label} doctor failed: {failed}")
    return result


def run_project_canary(
    project: ProjectConfig,
    *,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    """Exercise a real active closure on an isolated copy of current project docs."""
    _doctor_or_raise(project, "Source project")
    canary_id = (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    root = project.runtime_root / "canaries" / canary_id
    docs = root / "docs"
    runtime = root / "runtime"
    shutil.copytree(project.docs_root, docs, copy_function=shutil.copy2)
    source_state = project.document_path(project.files.state).read_bytes()
    source_history = project.document_path(project.files.history).read_bytes()
    engine_sha = str(engine_state(project.framework_root)["sha256"])
    canary = replace(
        project,
        mode="active",
        activation_engine_sha256=engine_sha,
        docs_root=docs,
        project_path=docs / "PROJECT.yaml",
        runtime_root=runtime,
    )
    history_before = verify_history(canary)
    started = start_project_run(
        canary,
        task=f"ARIA active closure canary {canary_id}",
        intent="design",
        mode="quick",
        actor_id=actor_id,
        device_id=device_id,
    )
    result_path = runtime / "canary-result.json"
    result = {
        "status": "completed",
        "summary": "Active closure canary completed on isolated project copy",
        "deliverable": "Verified active STATE/HISTORY transaction without product writes",
        "read_back": "Copied STATE and HISTORY were read back after active closure",
        "review": "Original project hashes remained unchanged",
        "closure": "Canary event exists only in isolated runtime copy",
    }
    atomic_write_json(result_path, result)
    closed = close_project_run(
        canary,
        run_id=str(started["run_id"]),
        result_path=result_path,
        actor_id=actor_id,
        device_id=device_id,
    )
    canary_doctor = _doctor_or_raise(canary, "Canary project")
    history_after = verify_history(canary)
    source_unchanged = (
        project.document_path(project.files.state).read_bytes() == source_state
        and project.document_path(project.files.history).read_bytes() == source_history
    )
    if (
        closed.get("status") != "completed"
        or history_after.get("events") != int(history_before.get("events", 0)) + 1
        or not source_unchanged
    ):
        raise WorkflowError(
            "Active canary final read-back did not satisfy its contract: "
            f"close={closed.get('status')}; "
            f"events={history_before.get('events')}->{history_after.get('events')}; "
            f"source_unchanged={source_unchanged}"
        )
    report = {
        "schema_version": 1,
        "project": project.project_id,
        "canary_id": canary_id,
        "ok": True,
        "engine_sha256": engine_sha,
        "source_state_sha256": _sha256(source_state),
        "source_history_sha256": _sha256(source_history),
        "source_unchanged": True,
        "canary_history_before": history_before.get("events"),
        "canary_history_after": history_after.get("events"),
        "canary_history_head_sha256": history_after.get("head_sha256"),
        "run_id": started["run_id"],
        "writes": closed.get("project_writes", []),
        "doctor_ok": canary_doctor.get("ok"),
        "root": str(root),
    }
    atomic_write_json(root / "canary-report.json", report)
    return report


def _active_registry(registry_before: bytes, project_id: str, engine_sha: str) -> bytes:
    try:
        text = registry_before.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise WorkflowError(f"Project registry is not UTF-8: {error}") from error
    header = re.compile(rf"(?m)^\[projects\.{re.escape(project_id)}\]\s*$").search(text)
    if header is None:
        raise WorkflowError(f"Project registry section is missing: {project_id}")
    following = re.compile(r"(?m)^\[").search(text, header.end())
    end = following.start() if following is not None else len(text)
    section = text[header.start() : end]
    if re.search(r"(?m)^mode\s*=", section):
        section = re.sub(r'(?m)^mode\s*=\s*"[^"]*"\s*$', 'mode = "active"', section)
    else:
        section = section.rstrip() + '\nmode = "active"\n'
    if re.search(r"(?m)^engine_sha256\s*=", section):
        section = re.sub(
            r'(?m)^engine_sha256\s*=\s*"[^"]*"\s*$',
            f'engine_sha256 = "{engine_sha}"',
            section,
        )
    else:
        section = section.rstrip() + f'\nengine_sha256 = "{engine_sha}"\n'
    if not section.endswith(("\n", "\r")):
        section += "\n"
    return (text[: header.start()] + section + text[end:]).encode("utf-8")


def _recover_pending_cutovers(project: ProjectConfig) -> bool:
    cutovers = project.runtime_root / "cutovers"
    if not cutovers.is_dir():
        return False
    recovered = False
    for journal_path in sorted(cutovers.glob("*/cutover-journal.json")):
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WorkflowError(
                f"Activation recovery journal is unreadable: {journal_path}"
            ) from error
        if not isinstance(journal, dict) or journal.get("schema_version") != 2:
            raise WorkflowError(f"Activation recovery journal is invalid: {journal_path}")
        if journal.get("phase") != "prepared":
            continue
        if journal.get("project") != project.project_id:
            raise WorkflowError(
                f"Activation recovery project mismatch: {journal_path}"
            )
        expected_roots = {
            "framework": str(project.framework_root.resolve(strict=True)),
            "docs": str(project.docs_root.resolve(strict=True)),
            "code": str(project.code_root.resolve(strict=True)),
            "runtime": str(project.runtime_root.resolve(strict=True)),
            "registry": str(project.registry_path.resolve(strict=True)),
        }
        if journal.get("roots") != expected_roots:
            raise WorkflowError(
                "Activation recovery root identity mismatch; refusing cross-project writes"
            )
        snapshot = journal_path.parent
        state_before = (snapshot / "STATE.before.yaml").read_bytes()
        history_before = (snapshot / "HISTORY.before.jsonl").read_bytes()
        registry_before = (snapshot / "projects.before.toml").read_bytes()
        expected = journal.get("before_sha256")
        actual = {
            "state": _sha256(state_before),
            "history": _sha256(history_before),
            "registry": _sha256(registry_before),
        }
        if expected != actual:
            raise WorkflowError(
                f"Activation recovery preimage SHA mismatch: {journal_path}"
            )
        after_sha = journal.get("after_sha256")
        before_entry = journal.get("registry_entry_before")
        after_entry = journal.get("registry_entry_after")
        if (
            not isinstance(after_sha, dict)
            or not isinstance(before_entry, dict)
            or not isinstance(after_entry, dict)
        ):
            raise WorkflowError(
                f"Activation recovery journal lacks CAS identities: {journal_path}"
            )
        registry_path = project.registry_path
        state_path = project.document_path(project.files.state)
        history_path = project.document_path(project.files.history)
        with ExitStack() as locks:
            locks.enter_context(
                exclusive_lock(
                    registry_path.with_suffix(".lock"), timeout_seconds=120.0
                )
            )
            locks.enter_context(
                exclusive_lock(
                    project.runtime_root / "locks" / "project-state.lock",
                    timeout_seconds=120.0,
                )
            )
            current_state_sha = _sha256(state_path.read_bytes())
            current_history_sha = _sha256(history_path.read_bytes())
            if current_state_sha not in {expected["state"], after_sha.get("state")}:
                raise WorkflowError(
                    "Activation recovery STATE has unrelated changes; refusing rollback"
                )
            if current_history_sha not in {
                expected["history"],
                after_sha.get("history"),
            }:
                raise WorkflowError(
                    "Activation recovery HISTORY has unrelated changes; refusing rollback"
                )
            registry_payload = read_registry(registry_path)
            projects = dict(registry_payload.get("projects", {}))
            current_entry = projects.get(project.project_id)
            if current_entry != before_entry and current_entry != after_entry:
                raise WorkflowError(
                    "Activation recovery registry entry has unrelated changes; "
                    "refusing lost update"
                )
            projects[project.project_id] = dict(before_entry)
            registry_recovered = render_registry(
                {"schema_version": 1, "projects": projects}
            )
            atomic_write_bytes(registry_path, registry_recovered)
            atomic_write_bytes(history_path, history_before)
            atomic_write_bytes(state_path, state_before)
            recovered_payload = read_registry(registry_path)
            if (
                recovered_payload.get("projects", {}).get(project.project_id)
                != before_entry
                or history_path.read_bytes() != history_before
                or state_path.read_bytes() != state_before
            ):
                raise WorkflowError(
                    f"Activation recovery read-back failed: {journal_path}"
                )
            atomic_write_json(
                journal_path,
                {**journal, "phase": "rolled_back", "recovered_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")},
            )
        recovered = True
    return recovered


def activate_project(
    project: ProjectConfig,
    *,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    """Canary and atomically activate a shadow project or rebind a stale active one."""
    if _recover_pending_cutovers(project):
        project = load_project(
            project.project_id,
            framework_root=project.framework_root,
            runtime_root=project.registry_path.parent,
            registry_path=project.registry_path,
        )
    engine_sha = str(engine_state(project.framework_root)["sha256"])
    if project.mode not in {"shadow", "active"}:
        raise WorkflowError(f"Unsupported project mode: {project.mode}")
    rebind = project.mode == "active"
    if rebind and project.activation_engine_sha256 == engine_sha:
        raise WorkflowError(
            f"Project is already active on the current engine: {project.project_id}"
        )
    validation_project = (
        replace(project, mode="shadow", activation_engine_sha256=None)
        if rebind
        else project
    )
    _doctor_or_raise(validation_project, "Pre-cutover project")
    canary = (
        run_project_canary(
            validation_project, actor_id=actor_id, device_id=device_id
        )
        if actor_id is not None or device_id is not None
        else run_project_canary(validation_project)
    )
    if canary.get("engine_sha256") != engine_sha:
        raise WorkflowError("Engine changed between canary and active cutover")
    cutover_id = (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    state_path = project.document_path(project.files.state)
    history_path = project.document_path(project.files.history)
    registry_path = project.registry_path
    lock = project.runtime_root / "locks" / "project-state.lock"
    registry_lock = registry_path.with_suffix(".lock")
    with ExitStack() as locks:
        locks.enter_context(exclusive_lock(registry_lock, timeout_seconds=120.0))
        locks.enter_context(exclusive_lock(lock, timeout_seconds=120.0))
        state_before = state_path.read_bytes()
        history_before = history_path.read_bytes()
        registry_before = registry_path.read_bytes()
        registry_payload = read_registry(registry_path)
        registered = registry_payload.get("projects", {}).get(project.project_id)
        try:
            registered_docs = (
                Path(str(registered.get("docs_root"))).resolve(strict=True)
                if isinstance(registered, dict)
                else None
            )
            registered_code = (
                Path(str(registered.get("code_root"))).resolve(strict=True)
                if isinstance(registered, dict)
                else None
            )
        except OSError as error:
            raise WorkflowError(
                f"Project registry roots changed before cutover: {project.project_id}"
            ) from error
        registry_identity_matches = (
            isinstance(registered, dict)
            and registered.get("mode") == project.mode
            and registered.get("engine_sha256")
            == project.activation_engine_sha256
            and registered_docs == project.docs_root.resolve(strict=True)
            and registered_code == project.code_root.resolve(strict=True)
        )
        if not registry_identity_matches:
            raise WorkflowError(
                f"Project registry entry changed before cutover: {project.project_id}"
            )
        if str(engine_state(project.framework_root)["sha256"]) != engine_sha:
            raise WorkflowError("Engine changed before locked active cutover")
        history_status = verify_history(project)
        if history_status.get("ok") is not True:
            raise WorkflowError("Cannot activate a project with invalid HISTORY")
        state = parse_project_state(
            state_before.decode("utf-8-sig"),
            project_id=project.project_id,
            expected_profile=project.state_profile,
        )
        checkpoint = state.get("history_checkpoint")
        if not (
            isinstance(checkpoint, dict)
            and checkpoint.get("sequence") == history_status.get("events")
            and checkpoint.get("event_sha256") == history_status.get("head_sha256")
        ):
            raise WorkflowError(
                "Cannot activate while STATE/HISTORY checkpoint requires recovery"
            )
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        git_head = str(
            git_snapshot(project.code_root, project.git_ignore_prefixes)["head"]
        )
        event: dict[str, object] = {
            "schema_version": 1,
            "sequence": int(history_status.get("events", 0)) + 1,
            "timestamp": timestamp,
            "type": "aria_engine_rebind" if rebind else "aria_active_cutover",
            "project_id": project.project_id,
            "run_id": f"cutover:{cutover_id}",
            "task_id": None,
            "git_head": git_head,
            "previous_event_sha256": history_status.get("head_sha256"),
            "refs": [
                f"engine://aria-codex/{engine_sha}",
                f"canary://{project.project_id}/{canary['canary_id']}",
            ],
            "result": {
                "summary": (
                    "Active project rebound to a verified ARIA engine"
                    if rebind
                    else "Project activated after isolated live closure canary"
                ),
                "canary_history_head_sha256": canary["canary_history_head_sha256"],
                "engine_sha256": engine_sha,
                "previous_engine_sha256": project.activation_engine_sha256,
            },
        }
        event["event_sha256"] = canonical_sha(event)
        history_after = history_before
        if history_after and not history_after.endswith(b"\n"):
            history_after += b"\n"
        history_after += (
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        state["history_checkpoint"] = {
            "sequence": event["sequence"],
            "event_sha256": event["event_sha256"],
        }
        state["last_verified"] = {
            "kind": "aria_engine_rebind" if rebind else "aria_active_cutover",
            "result": {
                "status": "active",
                "engine_sha256": engine_sha,
                "canary_id": canary["canary_id"],
            },
            "refs": [f"cutover://{project.project_id}/{cutover_id}"],
        }
        if "updated" in state:
            state["updated"] = timestamp
        state_after = yaml.safe_dump(
            state,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).encode("utf-8")
        if len(state_after) > project.state_budget_bytes:
            raise WorkflowError("Active cutover would exceed the STATE budget")
        registry_after = _active_registry(
            registry_before, project.project_id, engine_sha
        )
        registry_entry_before = dict(registered)
        registry_entry_after = {
            **registry_entry_before,
            "mode": "active",
            "engine_sha256": engine_sha,
        }
        snapshot = project.runtime_root / "cutovers" / cutover_id
        atomic_write_bytes(snapshot / "STATE.before.yaml", state_before)
        atomic_write_bytes(snapshot / "HISTORY.before.jsonl", history_before)
        atomic_write_bytes(snapshot / "projects.before.toml", registry_before)
        atomic_write_json(snapshot / "canary-report.json", canary)
        journal_path = snapshot / "cutover-journal.json"
        journal = {
            "schema_version": 2,
            "project": project.project_id,
            "cutover_id": cutover_id,
            "phase": "prepared",
            "engine_sha256": engine_sha,
            "roots": {
                "framework": str(project.framework_root.resolve(strict=True)),
                "docs": str(project.docs_root.resolve(strict=True)),
                "code": str(project.code_root.resolve(strict=True)),
                "runtime": str(project.runtime_root.resolve(strict=True)),
                "registry": str(project.registry_path.resolve(strict=True)),
            },
            "registry_entry_before": registry_entry_before,
            "registry_entry_after": registry_entry_after,
            "before_sha256": {
                "state": _sha256(state_before),
                "history": _sha256(history_before),
                "registry": _sha256(registry_before),
            },
            "after_sha256": {
                "state": _sha256(state_after),
                "history": _sha256(history_after),
                "registry": _sha256(registry_after),
            },
        }
        atomic_write_json(journal_path, journal)
        try:
            atomic_write_bytes(history_path, history_after)
            atomic_write_bytes(state_path, state_after)
            atomic_write_bytes(registry_path, registry_after)
            active = load_project(
                project.project_id,
                framework_root=project.framework_root,
                runtime_root=registry_path.parent,
                registry_path=registry_path,
            )
            doctor = _doctor_or_raise(active, "Active project")
            verified = verify_history(active)
            if (
                active.mode != "active"
                or active.activation_engine_sha256 != engine_sha
                or verified.get("head_sha256") != event["event_sha256"]
                or history_path.read_bytes() != history_after
                or state_path.read_bytes() != state_after
                or registry_path.read_bytes() != registry_after
            ):
                raise WorkflowError("Active cutover read-back verification failed")
            atomic_write_json(
                journal_path,
                {
                    **journal,
                    "phase": "committed",
                    "event_sha256": event["event_sha256"],
                },
            )
        except Exception:
            atomic_write_bytes(registry_path, registry_before)
            atomic_write_bytes(history_path, history_before)
            atomic_write_bytes(state_path, state_before)
            atomic_write_json(
                journal_path,
                {**journal, "phase": "rolled_back"},
            )
            raise
        report = {
            "schema_version": 1,
            "project": project.project_id,
            "cutover_id": cutover_id,
            "mode": "active",
            "action": "engine-rebind" if rebind else "active-cutover",
            "engine_sha256": engine_sha,
            "event_sequence": event["sequence"],
            "event_sha256": event["event_sha256"],
            "canary": canary,
            "doctor_ok": doctor.get("ok"),
            "snapshot": str(snapshot),
            "state_before_sha256": _sha256(state_before),
            "state_after_sha256": _sha256(state_path.read_bytes()),
            "history_before_sha256": _sha256(history_before),
            "history_after_sha256": _sha256(history_path.read_bytes()),
        }
        atomic_write_json(snapshot / "cutover-report.json", report)
        return report
