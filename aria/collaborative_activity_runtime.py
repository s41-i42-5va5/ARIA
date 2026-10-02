from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from aria.activity import LOCAL_STAGES, load_activity, parse_activity_event
from aria.activity_coordinator import submit_activity_event
from aria.collaboration import load_control_contract
from aria.collaborative_backlog import load_collaborative_backlog
from aria.collaborative_runtime import (
    AuthenticatedCollaborativeAdapter,
    authorize_collaborative_actor,
    machine_runtime_root,
)
from aria.control_worktree_sync import (
    ControlWriter,
    prepare_control_mutation,
    serialized_control_operation,
    synchronize_control_worktree,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.github_integration import (
    GitHubBoundOpenPullRequest,
    GitHubBoundPullRequest,
)
from aria.project import ProjectConfig
from aria.provider import ProviderInspection


class _InspectionAdapter:
    provider_id = "github"

    def __init__(self, inspection: ProviderInspection) -> None:
        self.inspection = inspection

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return self.inspection


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def collaborative_activity_status(
    project: ProjectConfig,
    *,
    actor_user_id: str | None = None,
) -> dict[str, object]:
    snapshot = load_activity(project.docs_root / "ACTIVITY.yaml")
    if snapshot["project_id"] != project.project_id:
        raise WorkflowError("ACTIVITY.yaml belongs to another project")
    active = list(snapshot["active_work"])
    if actor_user_id is not None:
        active = [
            entry
            for entry in active
            if entry["actor"]["user_id"] == actor_user_id
        ]
    return {
        "ok": True,
        "project": project.project_id,
        "revision": snapshot["revision"],
        "count": len(active),
        "active_work": active,
    }


def authenticated_activity_status(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
) -> dict[str, object]:
    authorization = authorize_collaborative_actor(
        project, adapter=adapter, require_protection=False
    )
    if "state.read" not in authorization.permissions:
        raise WorkflowError("activity read is not permitted")
    result = collaborative_activity_status(
        project, actor_user_id=authorization.actor.actor.user_id
    )
    return {**result, "actor": authorization.actor.as_mapping()}


def _submit_authenticated_activity(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    control_writer: ControlWriter,
    expected_revision: int,
    task_id: str,
    stage: str,
    branch: str,
    note: str | None = None,
    event_id: str | None = None,
    observed_at: str | None = None,
    received_at: str | None = None,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if type(expected_revision) is not int or expected_revision < 0:
        raise ConfigurationError("expected activity revision is invalid")
    if stage not in LOCAL_STAGES:
        raise ConfigurationError("developer activity stage is invalid")
    authorization = authorize_collaborative_actor(
        project, adapter=adapter, require_protection=True
    )
    if "activity.write" not in authorization.permissions:
        raise WorkflowError("activity write is not permitted")
    backlog = load_collaborative_backlog(project.docs_root / "BACKLOG.yaml")
    if backlog["project_id"] != project.project_id:
        raise WorkflowError("BACKLOG.yaml belongs to another project")
    item = next((row for row in backlog["items"] if row["id"] == task_id), None)
    if item is None:
        raise WorkflowError(f"Unknown backlog item: {task_id}")
    if item["status"] == "done":
        raise WorkflowError("completed backlog item cannot publish developer activity")
    assignee = item["assignee"]
    if (
        assignee is None
        or assignee["provider"] != authorization.actor.provider
        or assignee["user_id"] != authorization.actor.actor.user_id
    ):
        raise WorkflowError("only the backlog assignee may publish developer activity")
    lease = item.get("lease")
    if (
        item["status"] not in {"in_progress", "blocked"}
        or not isinstance(lease, dict)
        or lease.get("branch") != branch
    ):
        raise WorkflowError("developer activity must use the active task lease branch")
    operation = {
        "project_id": project.project_id,
        "expected_revision": expected_revision,
        "task_id": task_id,
        "stage": stage,
        "branch": branch,
        "note": note,
        "provider": authorization.actor.provider,
        "user_id": authorization.actor.actor.user_id,
    }
    digest = hashlib.sha256(
        json.dumps(
            operation,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    supplied_event_id = event_id is not None
    operation_id = event_id or f"activity-r{expected_revision + 1}-{digest[:16]}"
    event = parse_activity_event(
        {
            "schema_version": 1,
            "event_id": operation_id,
            "project_id": project.project_id,
            "task_id": task_id,
            "actor": {
                "provider": authorization.actor.provider,
                "user_id": authorization.actor.actor.user_id,
                "username_snapshot": authorization.actor.actor.username_snapshot,
            },
            "source": "local_aria",
            "stage": stage,
            "branch": branch,
            "pr_number": None,
            "note": note,
            "observed_at": observed_at or _utc_now(),
        }
    )
    prepare_control_mutation(project, operation_id=operation_id)
    result = submit_activity_event(
        control_root=project.docs_root,
        runtime_root=machine_runtime_root(project),
        project_id=project.project_id,
        event_value=event,
        request_id=operation_id,
        correlation_id=f"correlation-{operation_id}",
        expected_revision=expected_revision,
        authorized_source="local_aria",
        authorized_provider=authorization.actor.provider,
        authorized_actor=authorization.actor.actor,
        received_at=received_at or _utc_now(),
        bind_observed_at=supplied_event_id,
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
        "event_id": operation_id,
        "applied": result["applied"],
        "reason": result["reason"],
        "recovered": result["recovered"],
        "revision": result["revision"],
        "control_commit": remote["control_commit"],
        "remote_recovered": remote["recovered"],
    }


def submit_authenticated_activity(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    control_writer: ControlWriter,
    expected_revision: int,
    task_id: str,
    stage: str,
    branch: str,
    note: str | None = None,
    event_id: str | None = None,
    observed_at: str | None = None,
    received_at: str | None = None,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    with serialized_control_operation(project):
        return _submit_authenticated_activity(
            project,
            adapter=adapter,
            control_writer=control_writer,
            expected_revision=expected_revision,
            task_id=task_id,
            stage=stage,
            branch=branch,
            note=note,
            event_id=event_id,
            observed_at=observed_at,
            received_at=received_at,
            git_environment=git_environment,
        )


def _publish_verified_github_activity(
    project: ProjectConfig,
    *,
    coordinator_adapter: object,
    candidate: GitHubBoundOpenPullRequest | GitHubBoundPullRequest,
    stage: str,
    expected_revision: int,
    control_writer: ControlWriter,
    git_environment: dict[str, str] | None = None,
    received_at: str | None = None,
) -> dict[str, object]:
    if stage not in {"in_review", "waiting_for_ci"}:
        raise ConfigurationError("GitHub activity stage is invalid")
    if not isinstance(candidate, (GitHubBoundOpenPullRequest, GitHubBoundPullRequest)):
        raise ConfigurationError("GitHub activity pull request is invalid")
    actor = candidate.pull_request_author
    branch = candidate.branch
    if actor is None or branch is None:
        raise ConfigurationError("GitHub activity candidate lacks author or branch")
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    coordinator_inspection = coordinator_adapter.inspect_collaboration(
        repository_id=contract.repository_id,
        control_branch=contract.control_branch,
    )
    authorization = authorize_collaborative_actor(
        project, adapter=coordinator_adapter, require_protection=True
    )
    if "admin" not in coordinator_inspection.membership.roles:
        raise WorkflowError("GitHub activity publication requires coordinator admin")
    listing = getattr(coordinator_adapter, "list_collaborators", None)
    if not callable(listing):
        raise WorkflowError("GitHub activity publication requires collaborator read-back")
    members = listing(repository_id=authorization.contract.repository_id)
    member = next(
        (
            value
            for value in members
            if value.provider == "github" and value.actor.user_id == actor.user_id
        ),
        None,
    )
    if member is None:
        raise WorkflowError("pull request author is not an active collaborator")
    actor_inspection = ProviderInspection(
        provider="github",
        repository_id=authorization.contract.repository_id,
        actor=member.actor,
        membership=member.membership,
        protection=coordinator_inspection.protection,
    )
    actor_authorization = authorize_collaborative_actor(
        project,
        adapter=_InspectionAdapter(actor_inspection),
        require_protection=True,
    )
    if "activity.write" not in actor_authorization.permissions:
        raise WorkflowError("pull request author cannot publish activity")
    backlog = load_collaborative_backlog(project.docs_root / "BACKLOG.yaml")
    item = next(
        (row for row in backlog["items"] if row["id"] == candidate.backlog_item_id),
        None,
    )
    if item is None or item["status"] == "done":
        raise WorkflowError("pull request activity references an unavailable backlog item")
    assignee = item["assignee"]
    if (
        assignee is None
        or assignee["provider"] != "github"
        or assignee["user_id"] != member.actor.user_id
    ):
        raise WorkflowError("pull request author is not the backlog assignee")
    lease = item.get("lease")
    if not isinstance(lease, dict) or lease.get("branch") != branch:
        raise WorkflowError("pull request branch does not match the active task lease")
    operation_id = (
        f"github-pr-{candidate.number}-{stage}-{candidate.backlog_item_id}"
    )
    observed_at = (
        candidate.updated_at
        if isinstance(candidate, GitHubBoundOpenPullRequest)
        else candidate.merged_at
    )
    event = parse_activity_event(
        {
            "schema_version": 1,
            "event_id": operation_id,
            "project_id": project.project_id,
            "task_id": candidate.backlog_item_id,
            "actor": {
                "provider": "github",
                "user_id": member.actor.user_id,
                "username_snapshot": member.actor.username_snapshot,
            },
            "source": "github",
            "stage": stage,
            "branch": branch,
            "pr_number": candidate.number,
            "note": None,
            "observed_at": observed_at,
        }
    )
    prepare_control_mutation(project, operation_id=operation_id)
    result = submit_activity_event(
        control_root=project.docs_root,
        runtime_root=machine_runtime_root(project),
        project_id=project.project_id,
        event_value=event,
        request_id=operation_id,
        correlation_id=f"correlation-{operation_id}",
        expected_revision=expected_revision,
        authorized_source="github",
        authorized_provider="github",
        authorized_actor=member.actor,
        received_at=received_at or _utc_now(),
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
        "event_id": operation_id,
        "stage": stage,
        "pull_request_number": candidate.number,
        "item_id": candidate.backlog_item_id,
        "applied": result["applied"],
        "reason": result["reason"],
        "revision": result["revision"],
        "recovered": result["recovered"],
        "control_commit": remote["control_commit"],
        "remote_recovered": remote["recovered"],
    }


def publish_verified_github_activity(
    project: ProjectConfig,
    *,
    coordinator_adapter: object,
    candidate: GitHubBoundOpenPullRequest | GitHubBoundPullRequest,
    stage: str,
    expected_revision: int,
    control_writer: ControlWriter,
    git_environment: dict[str, str] | None = None,
    received_at: str | None = None,
) -> dict[str, object]:
    with serialized_control_operation(project):
        return _publish_verified_github_activity(
            project,
            coordinator_adapter=coordinator_adapter,
            candidate=candidate,
            stage=stage,
            expected_revision=expected_revision,
            control_writer=control_writer,
            git_environment=git_environment,
            received_at=received_at,
        )
