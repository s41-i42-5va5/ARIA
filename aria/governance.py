from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from aria.errors import ConfigurationError, WorkflowError
from aria.project import ProjectConfig, git_is_ancestor, git_snapshot


ACTIVE_RUN_STATUSES = {"started", "awaiting_user_approval"}
TERMINAL_RUN_STATUSES = {"completed", "blocked", "proposed"}
DECISION_ID_RE = re.compile(r"DEC-[A-Za-z0-9._:-]+", re.IGNORECASE)


def _manifest_rows(project: ProjectConfig) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    runs_root = project.runtime_root / "runs"
    if not runs_root.is_dir():
        return rows
    for path in sorted(runs_root.glob("*/manifest.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            rows.append(
                {
                    "run_id": path.parent.name,
                    "status": "unreadable",
                    "manifest_path": str(path),
                    "error": str(error),
                }
            )
            continue
        if not isinstance(raw, dict):
            rows.append(
                {
                    "run_id": path.parent.name,
                    "status": "unreadable",
                    "manifest_path": str(path),
                    "error": "manifest must be a JSON object",
                }
            )
            continue
        rows.append({**raw, "manifest_path": str(path)})
    return rows


def _is_build_run(manifest: dict[str, object]) -> bool:
    route = manifest.get("route")
    return isinstance(route, dict) and route.get("intent") == "build"


def _governance_binding(manifest: dict[str, object]) -> dict[str, object] | None:
    value = manifest.get("governance")
    return value if isinstance(value, dict) else None


def _markdown_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) is not None
        for cell in cells
    )


def load_decision_registry(project: ProjectConfig) -> dict[str, object] | None:
    governance = project.governance
    if governance is None or governance.decision_registry is None:
        return None
    path = project.code_path(governance.decision_registry)
    try:
        content = path.read_bytes()
        text = content.decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(
            f"Decision registry is unreadable: {path}: {error}"
        ) from error
    lines = text.splitlines()
    header_index: int | None = None
    id_index: int | None = None
    status_index: int | None = None
    for index in range(len(lines) - 1):
        header = _markdown_cells(lines[index])
        separator = _markdown_cells(lines[index + 1])
        if not header or len(header) != len(separator) or not _is_separator(separator):
            continue
        normalized = [cell.casefold() for cell in header]
        try:
            candidate_id = normalized.index("id")
        except ValueError:
            continue
        candidate_status = next(
            (
                position
                for position, value in enumerate(normalized)
                if value in {"status", "статус"}
            ),
            None,
        )
        if candidate_status is None:
            continue
        header_index = index
        id_index = candidate_id
        status_index = candidate_status
        break
    if header_index is None or id_index is None or status_index is None:
        raise ConfigurationError(
            "Decision registry must contain a Markdown table with ID and status columns"
        )
    decisions: dict[str, dict[str, object]] = {}
    for line_number, line in enumerate(lines[header_index + 2 :], start=header_index + 3):
        cells = _markdown_cells(line)
        if not cells:
            if decisions:
                break
            continue
        if max(id_index, status_index) >= len(cells):
            raise ConfigurationError(
                f"Decision registry row {line_number} has fewer cells than its header"
            )
        decision_id = cells[id_index].strip().upper()
        if DECISION_ID_RE.fullmatch(decision_id) is None:
            if decisions:
                break
            continue
        if decision_id in decisions:
            raise ConfigurationError(
                f"Decision registry contains duplicate id: {decision_id}"
            )
        status = cells[status_index].strip().upper()
        if not status:
            raise ConfigurationError(
                f"Decision registry status is empty: {decision_id}"
            )
        decisions[decision_id] = {
            "id": decision_id,
            "status": status,
            "line": line_number,
        }
    if not decisions:
        raise ConfigurationError("Decision registry contains no DEC-* rows")
    return {
        "path": governance.decision_registry,
        "sha256": hashlib.sha256(content).hexdigest(),
        "decisions": decisions,
    }


def decision_reconciliation_plan(project: ProjectConfig) -> dict[str, object]:
    registry = load_decision_registry(project)
    if registry is None:
        return {
            "configured": False,
            "registry": None,
            "actions": [],
        }
    from aria.backlog import load_backlog

    backlog = load_backlog(project)
    decisions = registry["decisions"]
    actions: list[dict[str, object]] = []
    for item in backlog["items"]:
        if not isinstance(item, dict) or item.get("status") == "done":
            continue
        requirements = item.get("requirements")
        if not isinstance(requirements, list):
            continue
        decision_ids = [
            str(value).upper()
            for value in requirements
            if isinstance(value, str) and DECISION_ID_RE.fullmatch(value)
        ]
        if (
            item.get("type") != "clarification"
            or not decision_ids
            or len(decision_ids) != len(requirements)
            or not all(
            isinstance(decisions.get(decision_id), dict)
            and decisions[decision_id].get("status") == "ACCEPTED"
            for decision_id in decision_ids
            )
        ):
            continue
        actions.append(
            {
                "item_id": item.get("id"),
                "current_status": item.get("status"),
                "target_status": "done",
                "decision_ids": decision_ids,
                "acceptance": item.get("acceptance"),
                "requires_acceptance_confirmation": True,
                "evidence": [
                    f"decision:{decision_id}:{registry['sha256']}"
                    for decision_id in decision_ids
                ],
                "reason": "accepted decision resolves the linked clarification",
            }
        )
    return {
        "configured": True,
        "registry": {
            "path": registry["path"],
            "sha256": registry["sha256"],
            "decisions": len(decisions),
        },
        "backlog_revision": backlog["revision"],
        "actions": actions,
    }


def governance_diagnostics(project: ProjectConfig) -> dict[str, object]:
    issues: list[dict[str, object]] = []
    governance = project.governance
    if governance is None:
        return {
            "configured": False,
            "ok": True,
            "state": "LEGACY_UNENFORCED",
            "issues": [
                {
                    "id": "governance_contract_missing",
                    "blocking": False,
                    "detail": "PROJECT.yaml has no executable governance contract",
                }
            ],
            "active_build_runs": [],
            "decision_reconciliation": {
                "configured": False,
                "registry": None,
                "actions": [],
            },
        }
    manifests = _manifest_rows(project)
    unreadable = [row for row in manifests if row.get("status") == "unreadable"]
    for row in unreadable:
        issues.append(
            {
                "id": "run_manifest_unreadable",
                "blocking": True,
                "detail": f"{row.get('run_id')}: {row.get('error')}",
            }
        )
    active_builds = [
        row
        for row in manifests
        if row.get("status") in ACTIVE_RUN_STATUSES and _is_build_run(row)
    ]
    if len(active_builds) > 1:
        issues.append(
            {
                "id": "multiple_active_build_runs",
                "blocking": True,
                "detail": [row.get("run_id") for row in active_builds],
            }
        )
    try:
        snapshot = git_snapshot(project.code_root, project.git_ignore_prefixes)
    except ConfigurationError as error:
        issues.append(
            {
                "id": "git_state_unavailable",
                "blocking": True,
                "detail": str(error),
            }
        )
        snapshot = None
    if (
        governance.require_active_run_for_writes
        and isinstance(snapshot, dict)
        and snapshot.get("dirty") is True
        and not active_builds
    ):
        issues.append(
            {
                "id": "unmanaged_worktree_changes",
                "blocking": True,
                "detail": snapshot.get("changes", {}).get("paths", []),
            }
        )
    from aria.backlog import load_backlog

    backlog = load_backlog(project)
    item_index = {
        str(item.get("id")): item
        for item in backlog["items"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for manifest in active_builds:
        binding = _governance_binding(manifest)
        item_id = binding.get("backlog_item_id") if binding is not None else None
        item = item_index.get(str(item_id)) if isinstance(item_id, str) else None
        if binding is None or binding.get("enforced") is not True or item is None:
            issues.append(
                {
                    "id": "active_run_without_backlog_binding",
                    "blocking": True,
                    "detail": manifest.get("run_id"),
                }
            )
            continue
        if item.get("status") != "in_progress" or item.get("assignee") != binding.get(
            "actor_id"
        ):
            issues.append(
                {
                    "id": "active_run_backlog_mismatch",
                    "blocking": True,
                    "detail": {
                        "run_id": manifest.get("run_id"),
                        "item_id": item_id,
                        "status": item.get("status"),
                        "assignee": item.get("assignee"),
                    },
                }
            )
    for manifest in manifests:
        if manifest.get("status") not in {"completed", "blocked"}:
            continue
        binding = _governance_binding(manifest)
        item_id = binding.get("backlog_item_id") if binding is not None else None
        if not isinstance(item_id, str) or item_id not in item_index:
            continue
        expected = "done" if manifest.get("status") == "completed" else "blocked"
        if item_index[item_id].get("status") != expected:
            issues.append(
                {
                    "id": "terminal_run_requires_backlog_reconciliation",
                    "blocking": True,
                    "detail": {
                        "run_id": manifest.get("run_id"),
                        "item_id": item_id,
                        "run_status": manifest.get("status"),
                        "backlog_status": item_index[item_id].get("status"),
                    },
                }
            )
    decision_plan = decision_reconciliation_plan(project)
    if decision_plan["actions"]:
        issues.append(
            {
                "id": "accepted_decisions_require_reconciliation",
                "blocking": True,
                "detail": [row["item_id"] for row in decision_plan["actions"]],
            }
        )
    blocking = [row for row in issues if row.get("blocking") is True]
    return {
        "configured": True,
        "ok": not blocking,
        "state": "READY" if not blocking else "RECOVERY_REQUIRED",
        "status_authority": governance.status_authority,
        "require_active_run_for_writes": governance.require_active_run_for_writes,
        "git": snapshot,
        "active_build_runs": [row.get("run_id") for row in active_builds],
        "decision_reconciliation": decision_plan,
        "issues": issues,
    }


def governance_start_guard(
    project: ProjectConfig,
    *,
    intent: str,
    backlog_item_id: str | None,
    actor_id: str | None,
    device_id: str | None,
    git_baseline: dict[str, object],
) -> dict[str, object] | None:
    governance = project.governance
    if governance is None or not governance.require_active_run_for_writes:
        return None
    if intent != "build":
        return {
            "enforced": True,
            "operation": "read",
            "status_authority": governance.status_authority,
        }
    if git_baseline.get("dirty") is True:
        raise WorkflowError(
            "RECOVERY_REQUIRED: product worktree already contains changes before run start"
        )
    active = [
        row
        for row in _manifest_rows(project)
        if row.get("status") in ACTIVE_RUN_STATUSES and _is_build_run(row)
    ]
    if active:
        raise WorkflowError(
            "TASK_ACTIVE: resume the existing build run before starting another: "
            f"{[row.get('run_id') for row in active]}"
        )
    if not isinstance(backlog_item_id, str) or not backlog_item_id.strip():
        raise WorkflowError(
            "TASK_REQUIRED: governed build requires --backlog-item for an in_progress item"
        )
    if not actor_id or not device_id:
        raise WorkflowError(
            "CONTROL_PLANE_UNAVAILABLE: governed build requires actor and device identity"
        )
    from aria.backlog import load_backlog

    backlog = load_backlog(project)
    item = next(
        (row for row in backlog["items"] if row.get("id") == backlog_item_id), None
    )
    if item is None:
        raise WorkflowError(f"Unknown governed backlog item: {backlog_item_id}")
    if item.get("status") != "in_progress" or item.get("assignee") != actor_id:
        raise WorkflowError(
            "TASK_REQUIRED: backlog item must be claimed by the active actor before run start"
        )
    pending_decisions = decision_reconciliation_plan(project)["actions"]
    if pending_decisions:
        raise WorkflowError(
            "RECOVERY_REQUIRED: accepted decisions and backlog disagree; run governance reconciliation"
        )
    return {
        "enforced": True,
        "operation": "write",
        "status_authority": governance.status_authority,
        "backlog_item_id": backlog_item_id,
        "backlog_revision": backlog["revision"],
        "actor_id": actor_id,
        "device_id": device_id,
        "baseline_clean": True,
    }


def governance_preflight(
    project: ProjectConfig,
    *,
    operation: str,
    run_id: str | None = None,
) -> dict[str, object]:
    if operation not in {"read", "write", "resume"}:
        raise ConfigurationError(
            "Governance preflight operation must be read, write or resume"
        )
    diagnostics = governance_diagnostics(project)
    if operation == "read" or not diagnostics["configured"]:
        return {
            "ok": True,
            "project": project.project_id,
            "operation": operation,
            "state": diagnostics["state"],
            "diagnostics": diagnostics,
        }
    if diagnostics["ok"] is not True:
        return {
            "ok": False,
            "project": project.project_id,
            "operation": operation,
            "state": "RECOVERY_REQUIRED",
            "diagnostics": diagnostics,
        }
    if not run_id:
        return {
            "ok": False,
            "project": project.project_id,
            "operation": operation,
            "state": "TASK_REQUIRED",
            "diagnostics": diagnostics,
        }
    manifest = next(
        (row for row in _manifest_rows(project) if row.get("run_id") == run_id), None
    )
    if manifest is None or manifest.get("status") != "started" or not _is_build_run(manifest):
        return {
            "ok": False,
            "project": project.project_id,
            "operation": operation,
            "state": "RECOVERY_REQUIRED",
            "reason": "run is missing, terminal, or not a build run",
            "diagnostics": diagnostics,
        }
    context = manifest.get("context")
    baseline = context.get("git") if isinstance(context, dict) else None
    baseline_head = baseline.get("head") if isinstance(baseline, dict) else None
    current = git_snapshot(project.code_root, project.git_ignore_prefixes)
    try:
        reachable = isinstance(baseline_head, str) and git_is_ancestor(
            project.code_root, baseline_head, str(current.get("head"))
        )
    except ConfigurationError:
        reachable = False
    if not reachable:
        return {
            "ok": False,
            "project": project.project_id,
            "operation": operation,
            "state": "RECOVERY_REQUIRED",
            "reason": "run Git baseline is not an ancestor of the current checkout",
            "diagnostics": diagnostics,
        }
    return {
        "ok": True,
        "project": project.project_id,
        "operation": operation,
        "state": "TASK_ACTIVE",
        "run_id": run_id,
        "backlog_item_id": _governance_binding(manifest).get("backlog_item_id"),
        "diagnostics": diagnostics,
    }
