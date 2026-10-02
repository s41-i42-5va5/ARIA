from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aria.activity import load_activity, parse_activity_event
from aria.activity_coordinator import submit_activity_event
from aria.collaborative_backlog import (
    ProviderIdentity,
    load_collaborative_backlog,
    parse_backlog_request,
)
from aria.collaborative_backlog_coordinator import submit_collaborative_backlog_request
from aria.collaborative_runtime import authorize_collaborative_actor, machine_runtime_root
from aria.collaborative_state import (
    load_collaborative_state,
    validate_collaborative_history,
)
from aria.collaborative_state_coordinator import submit_state_acceptance
from aria.control_worktree_sync import (
    ControlWriter,
    prepare_control_mutation,
    serialized_control_operation,
    synchronize_control_worktree,
)
from aria.io import atomic_write_bytes, json_bytes
from aria.errors import ConfigurationError, WorkflowError
from aria.github_integration import GitHubBoundOpenPullRequest, GitHubIntegrationAcceptance
from aria.project import ProjectConfig
from aria.provider import ProviderActor, ProviderInspection, ProviderTeamMember


class IntegrationVerifier(Protocol):
    def verify(
        self,
        *,
        repository_id: str,
        integration_branch: str,
        pull_request_number: int,
        previous_accepted_head: str | None = None,
    ) -> GitHubIntegrationAcceptance: ...


class _ActorAdapter:
    provider_id = "github"

    def __init__(self, inspection: ProviderInspection) -> None:
        self._inspection = inspection

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return self._inspection


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _after_timestamp(value: str, floor: str) -> str:
    current = datetime.fromisoformat(value[:-1] + "+00:00")
    minimum = datetime.fromisoformat(floor[:-1] + "+00:00") + timedelta(
        microseconds=1
    )
    return max(current, minimum).isoformat().replace("+00:00", "Z")


def collaborative_state_status(project: ProjectConfig) -> dict[str, object]:
    from aria.collaboration import load_control_contract

    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    state = load_collaborative_state(project.docs_root / "STATE.yaml", contract)
    history = (project.docs_root / "HISTORY.jsonl").read_text(encoding="utf-8")
    validate_collaborative_history(history, state)
    return {
        "ok": True,
        "project": project.project_id,
        "revision": state["revision"],
        "integration_branch": state["integration_branch"],
        "accepted_head": state["accepted_head"],
        "accepted_at": state["accepted_at"],
        "backlog_revision": state["backlog_revision"],
        "components": state["components"],
    }


def sync_open_pull_request_review(
    project: ProjectConfig,
    *,
    coordinator_adapter: object,
    candidate: GitHubBoundOpenPullRequest,
    control_writer: ControlWriter,
    coordinator_integration_id: int,
    expected_backlog_revision: int,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if candidate.head_sha is None or not candidate.changed_paths:
        raise ConfigurationError("verified open pull request proof is incomplete")
    with serialized_control_operation(project):
        authorization = authorize_collaborative_actor(
            project, adapter=coordinator_adapter, require_protection=True
        )
        inspection = coordinator_adapter.inspect_collaboration(
            repository_id=authorization.contract.repository_id,
            control_branch=authorization.contract.control_branch,
        )
        members = coordinator_adapter.list_collaborators(
            repository_id=authorization.contract.repository_id
        )
        member = next(
            (
                row for row in members
                if row.provider == "github"
                and row.actor.user_id == candidate.pull_request_author.user_id
                and row.membership.active
            ),
            None,
        )
        if member is None:
            raise WorkflowError("open pull request author is not an active collaborator")
        actor_authorization = authorize_collaborative_actor(
            project,
            adapter=_ActorAdapter(
                ProviderInspection(
                    provider="github",
                    repository_id=inspection.repository_id,
                    actor=member.actor,
                    membership=member.membership,
                    protection=inspection.protection,
                )
            ),
            require_protection=True,
        )
        operation_id = f"review-pr-{candidate.number}-{candidate.head_sha[:16]}"
        request = parse_backlog_request(
            {
                "schema_version": 1,
                "request_id": operation_id,
                "correlation_id": f"correlation-{operation_id}",
                "project_id": project.project_id,
                "action": "review",
                "item_id": candidate.backlog_item_id,
                "payload": {
                    "pull_request": candidate.number,
                    "head_commit": candidate.head_sha,
                    "source_branch": candidate.branch,
                    "changed_paths": list(candidate.changed_paths),
                },
                "requested_at": candidate.updated_at,
            }
        )
        coordinator = ProviderIdentity(
            "github-app",
            ProviderActor(str(coordinator_integration_id), "aria-coordinator", "ARIA Coordinator"),
        )
        prepare_control_mutation(project, operation_id=operation_id)
        backlog = submit_collaborative_backlog_request(
            control_root=project.docs_root,
            runtime_root=machine_runtime_root(project),
            project_id=project.project_id,
            request_value=request,
            authenticated_actor=actor_authorization.actor,
            active_members=actor_authorization.members,
            permissions=actor_authorization.permissions,
            coordinator=coordinator,
            expected_revision=expected_backlog_revision,
            committed_at=_now(),
            acceptance_verified=True,
        )
        remote = synchronize_control_worktree(
            project, writer=control_writer, operation_id=operation_id,
            git_environment=git_environment,
        )
        return {
            "ok": True,
            "item_id": candidate.backlog_item_id,
            "pull_request_number": candidate.number,
            "backlog_revision": backlog["revision"],
            "applied": backlog["applied"],
            "control_commit": remote["control_commit"],
        }


def _member_inspection(
    *,
    acceptance: GitHubIntegrationAcceptance,
    members: tuple[ProviderTeamMember, ...],
    coordinator: ProviderInspection,
) -> ProviderInspection:
    member = next(
        (
            value
            for value in members
            if value.provider == "github"
            and value.actor.user_id == acceptance.pull_request_author.user_id
        ),
        None,
    )
    if member is None or not member.membership.active:
        raise WorkflowError("pull request author is not a live repository collaborator")
    return ProviderInspection(
        provider="github",
        repository_id=coordinator.repository_id,
        actor=member.actor,
        membership=member.membership,
        protection=coordinator.protection,
    )


def sync_accepted_pull_request(
    project: ProjectConfig,
    *,
    coordinator_adapter: object,
    verifier: IntegrationVerifier,
    control_writer: ControlWriter,
    coordinator_integration_id: int,
    item_id: str,
    pull_request_number: int,
    expected_backlog_revision: int,
    expected_state_revision: int,
    committed_at: str | None = None,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
        raise ConfigurationError("coordinator integration id must be positive")
    if not hasattr(coordinator_adapter, "list_collaborators"):
        raise ConfigurationError("state coordinator adapter is invalid")
    pending_path = project.runtime_root / "state-closure" / "pending.json"
    pending_phase = None
    if pending_path.exists():
        try:
            pending_probe = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigurationError("state closure recovery marker is unreadable") from error
        if isinstance(pending_probe, dict):
            pending_phase = pending_probe.get("phase")
    with serialized_control_operation(project):
        authorization = authorize_collaborative_actor(
            project, adapter=coordinator_adapter, require_protection=True
        )
        if "state.write" not in authorization.permissions:
            raise WorkflowError("state write is not permitted")
        coordinator_inspection = coordinator_adapter.inspect_collaboration(
            repository_id=authorization.contract.repository_id,
            control_branch=authorization.contract.control_branch,
        )
        if "admin" not in coordinator_inspection.membership.roles:
            raise WorkflowError("state sync requires repository admin membership")
        current_state = load_collaborative_state(
            project.docs_root / "STATE.yaml", authorization.contract
        )
        acceptance = verifier.verify(
            repository_id=authorization.contract.repository_id,
            integration_branch=authorization.contract.integration_branch,
            pull_request_number=pull_request_number,
            previous_accepted_head=current_state["accepted_head"],
        )
        if acceptance.backlog_item_id != item_id:
            raise WorkflowError("pull request backlog binding does not match requested item")
        members = coordinator_adapter.list_collaborators(
            repository_id=authorization.contract.repository_id
        )
        actor_authorization = authorize_collaborative_actor(
            project,
            adapter=_ActorAdapter(
                _member_inspection(
                    acceptance=acceptance,
                    members=members,
                    coordinator=coordinator_inspection,
                )
            ),
            require_protection=True,
        )
        if "backlog.complete" not in actor_authorization.permissions:
            raise WorkflowError("pull request author cannot complete backlog items")
        existing_acceptance = next(
            (
                event
                for event in current_state["events"]
                if event["pull_request_number"] == acceptance.pull_request_number
            ),
            None,
        )
        activity_event_id = (
            f"activity-complete-pr-{acceptance.pull_request_number}-"
            f"{acceptance.merge_commit[:16]}"
        )
        if existing_acceptance is not None:
            if existing_acceptance["merge_commit"] != acceptance.merge_commit:
                raise WorkflowError("accepted pull request merge commit changed")
            if pending_phase == "applying_documents":
                backlog_probe = load_collaborative_backlog(
                    project.docs_root / "BACKLOG.yaml"
                )
                item_probe = next(
                    (row for row in backlog_probe["items"] if row["id"] == item_id),
                    None,
                )
                activity_probe = load_activity(project.docs_root / "ACTIVITY.yaml")
                if (
                    isinstance(item_probe, dict)
                    and item_probe.get("status") == "done"
                    and activity_event_id in activity_probe["recent_event_ids"]
                ):
                    pending_value = {
                        "schema_version": 1,
                        "project_id": project.project_id,
                        "operation_id": (
                            f"state-pr-{acceptance.pull_request_number}-"
                            f"{acceptance.merge_commit[:16]}"
                        ),
                        "item_id": item_id,
                        "pull_request_number": acceptance.pull_request_number,
                        "expected_backlog_revision": expected_backlog_revision,
                        "expected_state_revision": expected_state_revision,
                        "coordinator_integration_id": coordinator_integration_id,
                        "phase": "documents_complete",
                    }
                    atomic_write_bytes(pending_path, json_bytes(pending_value))
                    pending_phase = "documents_complete"
            if pending_phase != "applying_documents":
                remote = synchronize_control_worktree(
                    project,
                    writer=control_writer,
                    operation_id=(
                        f"state-pr-{acceptance.pull_request_number}-"
                        f"{acceptance.merge_commit[:16]}"
                    ),
                    git_environment=git_environment,
                )
                pending_path.unlink(missing_ok=True)
                current_backlog = load_collaborative_backlog(
                    project.docs_root / "BACKLOG.yaml"
                )
                return {
                    "ok": True,
                    "project": project.project_id,
                    "event_id": existing_acceptance["event_id"],
                    "item_id": item_id,
                    "pull_request_number": acceptance.pull_request_number,
                    "merge_commit": acceptance.merge_commit,
                    "checks": [check.as_mapping() for check in acceptance.required_checks],
                    "backlog_revision": current_backlog["revision"],
                    "state_revision": current_state["revision"],
                    "backlog_applied": False,
                    "state_applied": False,
                    "activity_applied": False,
                    "activity_reason": "already_accepted",
                    "activity_revision": load_activity(
                        project.docs_root / "ACTIVITY.yaml"
                    )["revision"],
                    "recovered": True,
                    "control_commit": remote["control_commit"],
                    "remote_recovered": remote["recovered"],
                }
        activity_snapshot = load_activity(project.docs_root / "ACTIVITY.yaml")
        activity_entry = next(
            (
                entry
                for entry in activity_snapshot["active_work"]
                if entry["task_id"] == item_id
                and entry["stage"] == "waiting_for_ci"
                and entry["pr_number"] == acceptance.pull_request_number
            ),
            None,
        )
        if activity_entry is None:
            raise WorkflowError(
                "merged pull request requires matching waiting_for_ci activity"
            )
        if (
            activity_entry["actor"]["provider"] != "github"
            or activity_entry["actor"]["user_id"]
            != actor_authorization.actor.actor.user_id
        ):
            raise WorkflowError("waiting activity actor does not match pull request author")
        check_refs = [
            "github-check-sha256:"
            + hashlib.sha256(
                f"{check.name}:{check.app_id}".encode("utf-8")
            ).hexdigest()
            for check in acceptance.required_checks
        ]
        evidence_refs = sorted(
            {
                f"github-pr:{acceptance.pull_request_number}",
                f"git-commit:{acceptance.merge_commit}",
                *check_refs,
            }
        )
        operation_id = (
            f"state-pr-{acceptance.pull_request_number}-{acceptance.merge_commit[:16]}"
        )
        pending_identity = {
            "schema_version": 1,
            "project_id": project.project_id,
            "operation_id": operation_id,
            "item_id": item_id,
            "pull_request_number": acceptance.pull_request_number,
            "expected_backlog_revision": expected_backlog_revision,
            "expected_state_revision": expected_state_revision,
            "coordinator_integration_id": coordinator_integration_id,
        }
        if pending_path.exists():
            try:
                pending_value = json.loads(pending_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ConfigurationError("state closure recovery marker is unreadable") from error
            if not isinstance(pending_value, dict) or any(
                pending_value.get(key) != value
                for key, value in pending_identity.items()
            ):
                raise WorkflowError("another state closure recovery is pending")
        atomic_write_bytes(
            pending_path,
            json_bytes({**pending_identity, "phase": "applying_documents"}),
        )
        timestamp = committed_at or _now()
        request = parse_backlog_request(
            {
                "schema_version": 1,
                "request_id": operation_id,
                "correlation_id": f"correlation-{operation_id}",
                "project_id": project.project_id,
                "action": "complete",
                "item_id": item_id,
                "payload": {
                    "evidence_refs": evidence_refs,
                    "pull_request": acceptance.pull_request_number,
                    "merge_commit": acceptance.merge_commit,
                    "source_commit": acceptance.source_commit or acceptance.merge_commit,
                    "source_branch": acceptance.source_branch,
                    "changed_paths": list(acceptance.changed_paths),
                },
                "requested_at": acceptance.merged_at,
            }
        )
        coordinator = ProviderIdentity(
            "github-app",
            ProviderActor(
                str(coordinator_integration_id),
                "aria-coordinator",
                "ARIA Coordinator",
            ),
        )
        backlog = submit_collaborative_backlog_request(
            control_root=project.docs_root,
            runtime_root=machine_runtime_root(project),
            project_id=project.project_id,
            request_value=request,
            authenticated_actor=actor_authorization.actor,
            active_members=actor_authorization.members,
            permissions=actor_authorization.permissions,
            coordinator=coordinator,
            expected_revision=expected_backlog_revision,
            committed_at=timestamp,
            acceptance_verified=True,
        )
        state = submit_state_acceptance(
            control_root=project.docs_root,
            runtime_root=machine_runtime_root(project),
            project_id=project.project_id,
            backlog_item=backlog["item"],
            backlog_revision=backlog["revision"],
            acceptance=acceptance,
            coordinator=coordinator,
            event_id=operation_id,
            expected_revision=expected_state_revision,
        )
        activity_time = _after_timestamp(timestamp, activity_entry["observed_at"])
        event = parse_activity_event(
            {
                "schema_version": 1,
                "event_id": activity_event_id,
                "project_id": project.project_id,
                "task_id": item_id,
                "actor": {
                    "provider": "github",
                    "user_id": actor_authorization.actor.actor.user_id,
                    "username_snapshot": actor_authorization.actor.actor.username_snapshot,
                },
                "source": "coordinator",
                "stage": "completed",
                "branch": activity_entry["branch"],
                "pr_number": acceptance.pull_request_number,
                "note": None,
                "observed_at": activity_time,
            }
        )
        activity = submit_activity_event(
            control_root=project.docs_root,
            runtime_root=machine_runtime_root(project),
            project_id=project.project_id,
            event_value=event,
            request_id=activity_event_id,
            correlation_id=f"correlation-{activity_event_id}",
            expected_revision=int(activity_snapshot["revision"]),
            authorized_source="coordinator",
            authorized_provider="github",
            authorized_actor=actor_authorization.actor.actor,
            received_at=activity_time,
        )
        atomic_write_bytes(
            pending_path,
            json_bytes({**pending_identity, "phase": "documents_complete"}),
        )
        remote = synchronize_control_worktree(
            project,
            writer=control_writer,
            operation_id=operation_id,
            git_environment=git_environment,
        )
        pending_path.unlink()
        return {
            "ok": True,
            "project": project.project_id,
            "event_id": operation_id,
            "item_id": item_id,
            "pull_request_number": acceptance.pull_request_number,
            "merge_commit": acceptance.merge_commit,
            "checks": [check.as_mapping() for check in acceptance.required_checks],
            "backlog_revision": backlog["revision"],
            "state_revision": state["revision"],
            "backlog_applied": backlog["applied"],
            "state_applied": state["applied"],
            "activity_applied": activity["applied"],
            "activity_reason": activity["reason"],
            "activity_revision": activity["revision"],
            "recovered": (
                backlog["recovered"] or state["recovered"] or activity["recovered"]
            ),
            "control_commit": remote["control_commit"],
            "remote_recovered": remote["recovered"],
        }


def recover_pending_state_closure(
    project: ProjectConfig,
    *,
    coordinator_adapter: object,
    verifier: IntegrationVerifier,
    control_writer: ControlWriter,
    coordinator_integration_id: int,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object] | None:
    path = project.runtime_root / "state-closure" / "pending.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("state closure recovery marker is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("project_id") != project.project_id
        or value.get("phase") not in {"applying_documents", "documents_complete"}
        or value.get("coordinator_integration_id") != coordinator_integration_id
        or type(value.get("pull_request_number")) is not int
        or type(value.get("expected_backlog_revision")) is not int
        or type(value.get("expected_state_revision")) is not int
        or not isinstance(value.get("item_id"), str)
    ):
        raise ConfigurationError("state closure recovery marker is invalid")
    return sync_accepted_pull_request(
        project,
        coordinator_adapter=coordinator_adapter,
        verifier=verifier,
        control_writer=control_writer,
        coordinator_integration_id=coordinator_integration_id,
        item_id=value["item_id"],
        pull_request_number=value["pull_request_number"],
        expected_backlog_revision=value["expected_backlog_revision"],
        expected_state_revision=value["expected_state_revision"],
        git_environment=git_environment,
    )
