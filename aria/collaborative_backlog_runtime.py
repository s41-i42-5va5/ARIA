from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from aria.collaborative_backlog import (
    ACTIONS,
    ProviderIdentity,
    load_collaborative_backlog,
    parse_backlog_request,
)
from aria.collaborative_backlog_coordinator import (
    submit_collaborative_backlog_request,
)
from aria.collaborative_runtime import (
    AuthenticatedCollaborativeAdapter,
    authorize_collaborative_actor,
    machine_runtime_root,
)
from aria.control_worktree_sync import (
    ControlWriter,
    prepare_control_mutation,
    recover_pending_control_sync,
    serialized_control_operation,
    synchronize_control_worktree,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.project import ProjectConfig
from aria.provider import (
    ProviderActor,
)
from aria.file_scope import scope_sets_overlap
from aria.collaboration import load_control_contract
from aria.collaborative_documents import (
    plan_legacy_collaborative_upgrade,
    upgrade_legacy_collaborative_documents,
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def collaborative_backlog_status(
    project: ProjectConfig,
    *,
    actor_user_id: str | None = None,
    include_done: bool = False,
) -> dict[str, object]:
    backlog = load_collaborative_backlog(project.docs_root / "BACKLOG.yaml")
    if backlog["project_id"] != project.project_id:
        raise WorkflowError("BACKLOG.yaml belongs to another project")
    canonical = list(backlog["items"])
    by_id = {str(item["id"]): item for item in canonical}
    items: list[dict[str, object]] = []
    for source in canonical:
        item = dict(source)
        blockers: list[str] = []
        if item.get("kind") == "idea":
            blockers.append("owner_triage_required")
        for dependency in item.get("dependencies", []):
            if by_id[str(dependency)]["status"] != "done":
                blockers.append(f"dependency:{dependency}")
        if item.get("scope_paths"):
            for other in canonical:
                if other["id"] == item["id"] or other.get("lease") is None:
                    continue
                if scope_sets_overlap(item["scope_paths"], other["lease"]["scope_paths"]):
                    blockers.append(f"scope_conflict:{other['id']}")
        item["start_blockers"] = sorted(set(blockers))
        items.append(item)
    if not include_done:
        items = [item for item in items if item["status"] != "done"]
    if actor_user_id is not None:
        items = [
            item
            for item in items
            if item["assignee"] is not None
            and item["assignee"]["user_id"] == actor_user_id
        ]
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    recommended = [
        str(item["id"])
        for item in sorted(
            items,
            key=lambda row: (priority_order.get(str(row["priority"]), 99), str(row["id"])),
        )
        if item["status"] == "assigned" and not item["start_blockers"]
    ]
    return {
        "ok": True,
        "project": project.project_id,
        "revision": backlog["revision"],
        "count": len(items),
        "items": items,
        "recommended_order": recommended,
    }


def authenticated_backlog_status(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    include_done: bool = False,
) -> dict[str, object]:
    authorization = authorize_collaborative_actor(
        project, adapter=adapter, require_protection=False
    )
    if "state.read" not in authorization.permissions:
        raise WorkflowError("backlog read is not permitted")
    result = collaborative_backlog_status(
        project,
        actor_user_id=authorization.actor.actor.user_id,
        include_done=include_done,
    )
    return {**result, "actor": authorization.actor.as_mapping()}


def _submit_authenticated_backlog_action(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    control_writer: ControlWriter,
    coordinator_integration_id: int,
    expected_revision: int,
    action: str,
    item_id: str | None,
    payload: dict[str, object],
    request_id: str | None = None,
    requested_at: str | None = None,
    committed_at: str | None = None,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if action not in ACTIONS:
        raise ConfigurationError("backlog action is invalid")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ConfigurationError("expected backlog revision is invalid")
    if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
        raise ConfigurationError("coordinator integration id must be a positive integer")
    authorization = authorize_collaborative_actor(
        project, adapter=adapter, require_protection=True
    )
    actor = authorization.actor
    if action == "claim":
        from aria.collaborative_doctor import run_collaborative_project_doctor

        preflight = run_collaborative_project_doctor(project)
        checks = [
            row for row in preflight.get("checks", []) if isinstance(row, dict)
        ]
        repository_contract = next(
            (row for row in checks if row.get("id") == "repository_contract"),
            None,
        )
        failed = [
            row.get("id")
            for row in checks
            if row.get("blocking") and not row.get("ok")
        ]
        if repository_contract is None or repository_contract.get("ok") is not True:
            failed.append("repository_contract")
        if preflight.get("ok") is not True or failed:
            raise WorkflowError(f"backlog claim project preflight failed: {failed}")
    if action == "claim" and payload == {}:
        payload = {"branch": f"work/{actor.actor.username_snapshot}"}
    operation = {
        "project_id": project.project_id,
        "expected_revision": expected_revision,
        "action": action,
        "item_id": item_id,
        "payload": payload,
        "provider": actor.provider,
        "user_id": actor.actor.user_id,
    }
    digest = hashlib.sha256(
        json.dumps(
            operation,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    operation_id = request_id or f"backlog-r{expected_revision + 1}-{digest[:16]}"
    observed_at = requested_at or _utc_now()
    request = parse_backlog_request(
        {
            "schema_version": 1,
            "request_id": operation_id,
            "correlation_id": f"correlation-{operation_id}",
            "project_id": project.project_id,
            "action": action,
            "item_id": item_id,
            "payload": payload,
            "requested_at": observed_at,
        }
    )
    coordinator = ProviderIdentity(
        provider="github-app",
        actor=ProviderActor(
            user_id=str(coordinator_integration_id),
            username_snapshot="aria-coordinator",
            display_name_snapshot="ARIA Coordinator",
        ),
    )
    if action == "recover":
        recover_pending_control_sync(
            project,
            writer=control_writer,
            git_environment=git_environment,
        )
        target_documents, upgrade_changes = plan_legacy_collaborative_upgrade(
            project.docs_root,
            contract=load_control_contract(project.docs_root / "CONTROL.yaml"),
            coordinator_integration_id=coordinator_integration_id,
        )
        if any(upgrade_changes.values()):
            upgrade_operation_id = "policy-upgrade-" + hashlib.sha256(
                operation_id.encode("utf-8")
            ).hexdigest()[:16]
            prepare_control_mutation(
                project,
                operation_id=upgrade_operation_id,
                target_documents=target_documents,
            )
            upgrade_legacy_collaborative_documents(
                project.docs_root,
                contract=load_control_contract(project.docs_root / "CONTROL.yaml"),
                coordinator_integration_id=coordinator_integration_id,
            )
            synchronize_control_worktree(
                project,
                writer=control_writer,
                operation_id=upgrade_operation_id,
                git_environment=git_environment,
            )
    prepare_control_mutation(project, operation_id=operation_id)
    result = submit_collaborative_backlog_request(
        control_root=project.docs_root,
        runtime_root=machine_runtime_root(project),
        project_id=project.project_id,
        request_value=request,
        authenticated_actor=actor,
        active_members=authorization.members,
        permissions=authorization.permissions,
        coordinator=coordinator,
        expected_revision=expected_revision,
        committed_at=committed_at or _utc_now(),
    )
    remote = synchronize_control_worktree(
        project,
        writer=control_writer,
        operation_id=operation_id,
        git_environment=git_environment,
    )
    return {
        "ok": True,
        "project": project.project_id,
        "request_id": operation_id,
        "applied": result["applied"],
        "reason": result["reason"],
        "recovered": result["recovered"],
        "revision": result["revision"],
        "item": result["item"],
        "control_commit": remote["control_commit"],
        "remote_recovered": remote["recovered"],
    }


def submit_authenticated_backlog_action(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    control_writer: ControlWriter,
    coordinator_integration_id: int,
    expected_revision: int,
    action: str,
    item_id: str | None,
    payload: dict[str, object],
    request_id: str | None = None,
    requested_at: str | None = None,
    committed_at: str | None = None,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    with serialized_control_operation(project):
        return _submit_authenticated_backlog_action(
            project,
            adapter=adapter,
            control_writer=control_writer,
            coordinator_integration_id=coordinator_integration_id,
            expected_revision=expected_revision,
            action=action,
            item_id=item_id,
            payload=payload,
            request_id=request_id,
            requested_at=requested_at,
            committed_at=committed_at,
            git_environment=git_environment,
        )
