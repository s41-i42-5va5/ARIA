from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from aria.collaboration import load_control_contract
from aria.collaborative_backlog import ProviderIdentity
from aria.collaborative_team import (
    active_team_identities,
    collaborative_team_template,
    load_collaborative_team,
)
from aria.collaborative_team_coordinator import (
    collaborative_team_coordinator_paths,
    record_collaborative_team_invitation,
    sync_collaborative_team_snapshot,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.project import ProjectConfig
from aria.provider import ProviderActor, ProviderInspection, ProviderTeamMember
from aria.github_team import GitHubInvitationReceipt
from aria.github import OWNER_RE
from aria.control_worktree_sync import (
    ControlWriter,
    OPERATION_RE,
    prepare_control_mutation,
    recover_pending_control_sync,
    serialized_control_operation,
    synchronize_control_worktree,
)
from aria.io import atomic_write_bytes, json_bytes


class AuthenticatedTeamAdapter(Protocol):
    provider_id: str

    def inspect_collaboration(
        self, *, repository_id: str, control_branch: str
    ) -> ProviderInspection: ...

    def list_collaborators(
        self, *, repository_id: str
    ) -> tuple[ProviderTeamMember, ...]: ...


class AuthenticatedCollaboratorManager(Protocol):
    def invite(self, *, username: str, permission: str = "push") -> GitHubInvitationReceipt: ...
    def revoke(self, *, username: str) -> ProviderActor: ...

    def read_invitation(
        self, *, username: str, permission: str
    ) -> GitHubInvitationReceipt | None: ...

    def read_revocation(self, *, username: str) -> ProviderActor | None: ...


def _machine_runtime_root(project: ProjectConfig) -> Path:
    expected = project.runtime_root.parent / project.project_id
    if expected != project.runtime_root:
        raise ConfigurationError("project runtime layout is invalid")
    projects_root = project.runtime_root.parent
    if projects_root.name != "projects":
        raise ConfigurationError("project runtime layout is invalid")
    return projects_root.parent


def _contract(project: ProjectConfig):
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    if contract.project_id != project.project_id:
        raise WorkflowError("CONTROL.yaml belongs to another project")
    return contract


def _require_team_revision(project: ProjectConfig, *, expected_revision: int) -> None:
    contract = _contract(project)
    path = project.docs_root / "ARIA_TEAM.yaml"
    team = (
        load_collaborative_team(path)
        if path.exists()
        else collaborative_team_template(
            project.project_id,
            provider=contract.provider,
            repository_id=contract.repository_id,
        )
    )
    if int(team["revision"]) != expected_revision:
        raise WorkflowError(
            f"Stale collaborative team revision: expected {expected_revision}, "
            f"found {team['revision']}"
        )


def _external_journal(
    project: ProjectConfig,
    *,
    request_id: str,
    operation: str,
    username: str,
    permission: str | None,
    expected_revision: int,
    phase: str,
    receipt: dict[str, object] | None = None,
) -> Path:
    path = (
        _machine_runtime_root(project)
        / "collaborative-team-external"
        / project.project_id
        / f"{hashlib.sha256(request_id.encode('utf-8')).hexdigest()}.json"
    )
    identity = {
        "schema_version": 1,
        "project_id": project.project_id,
        "request_id": request_id,
        "operation": operation,
        "username": username,
        "permission": permission,
        "expected_revision": expected_revision,
    }
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigurationError("team external-operation journal is unreadable") from error
        if not isinstance(existing, dict) or any(
            existing.get(key) != value for key, value in identity.items()
        ):
            raise WorkflowError("team request id is already bound to another operation")
        phases = {"prepared": 0, "external_applied": 1, "committed": 2}
        existing_phase = existing.get("phase")
        if existing_phase not in phases:
            raise ConfigurationError("team external-operation phase is invalid")
        if phases[existing_phase] > phases[phase]:
            return path
    atomic_write_bytes(path, json_bytes({**identity, "phase": phase, "receipt": receipt}))
    return path


def _load_external_journal(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("team external-operation journal is unreadable") from error
    if not isinstance(value, dict):
        raise ConfigurationError("team external-operation journal is invalid")
    return value


def _actor_from_receipt(value: object) -> ProviderActor:
    if not isinstance(value, dict):
        raise ConfigurationError("team external receipt actor is invalid")
    return ProviderActor(
        str(value.get("user_id")),
        str(value.get("username_snapshot")),
        value.get("display_name_snapshot")
        if isinstance(value.get("display_name_snapshot"), str)
        else None,
    )


def collaborative_team_status(project: ProjectConfig) -> dict[str, object]:
    contract = _contract(project)
    paths = collaborative_team_coordinator_paths(
        control_root=project.docs_root,
        runtime_root=_machine_runtime_root(project),
        project_id=project.project_id,
    )
    team = (
        load_collaborative_team(paths.team)
        if paths.team.exists()
        else collaborative_team_template(
            project.project_id,
            provider=contract.provider,
            repository_id=contract.repository_id,
        )
    )
    identities = active_team_identities(team)
    return {
        "ok": True,
        "project": project.project_id,
        "provider": contract.provider,
        "repository_id": contract.repository_id,
        "revision": team["revision"],
        "checked_at": team["checked_at"],
        "active_count": len(identities),
        "members": team["members"],
    }


def _sync_authenticated_team(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedTeamAdapter,
    coordinator_integration_id: int,
    expected_revision: int,
    sync_id: str | None = None,
    checked_at: str | None = None,
    control_writer: ControlWriter,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    contract = _contract(project)
    if contract.provider != "github" or adapter.provider_id != contract.provider:
        raise WorkflowError("authenticated team adapter does not match CONTROL.yaml")
    if (
        type(coordinator_integration_id) is not int
        or coordinator_integration_id <= 0
    ):
        raise ConfigurationError("coordinator integration id must be a positive integer")
    inspection = adapter.inspect_collaboration(
        repository_id=contract.repository_id,
        control_branch=contract.control_branch,
    )
    if (
        inspection.provider != contract.provider
        or inspection.repository_id != contract.repository_id
        or not inspection.membership.active
    ):
        raise WorkflowError("authenticated user is not an active project member")
    if "admin" not in inspection.membership.roles:
        raise WorkflowError("team sync requires repository admin membership")
    if not inspection.protection.coordinator_only:
        raise WorkflowError("team sync requires coordinator-only control branch protection")
    members = adapter.list_collaborators(repository_id=contract.repository_id)
    if not any(
        member.provider == inspection.provider
        and member.actor.user_id == inspection.actor.user_id
        for member in members
    ):
        raise WorkflowError("authenticated user is absent from the active team projection")
    snapshot_bytes = json.dumps(
        [member.as_mapping() for member in members],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    operation_id = sync_id or (
        f"team-sync-r{expected_revision + 1}-"
        f"{hashlib.sha256(snapshot_bytes).hexdigest()[:16]}"
    )
    observed_at = checked_at or datetime.now(UTC).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    coordinator = ProviderIdentity(
        provider="github-app",
        actor=ProviderActor(
            user_id=str(coordinator_integration_id),
            username_snapshot="aria-coordinator",
            display_name_snapshot="ARIA Coordinator",
        ),
    )
    prepare_control_mutation(project, operation_id=operation_id)
    result = sync_collaborative_team_snapshot(
        control_root=project.docs_root,
        runtime_root=_machine_runtime_root(project),
        project_id=project.project_id,
        provider=contract.provider,
        repository_id=contract.repository_id,
        provider_members=members,
        sync_id=operation_id,
        coordinator=coordinator,
        expected_revision=expected_revision,
        checked_at=observed_at,
    )
    team = result["team"]
    remote = synchronize_control_worktree(
        project,
        writer=control_writer,
        operation_id=operation_id,
        git_environment=git_environment,
    )
    return {
        "ok": True,
        "project": project.project_id,
        "sync_id": operation_id,
        "applied": result["applied"],
        "reason": result["reason"],
        "recovered": result["recovered"],
        "revision": result["revision"],
        "checked_at": team["checked_at"],
        "active_count": len(active_team_identities(team)),
        "members": team["members"],
        "control_commit": remote["control_commit"],
        "remote_recovered": remote["recovered"],
    }


def sync_authenticated_team(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedTeamAdapter,
    coordinator_integration_id: int,
    expected_revision: int,
    sync_id: str | None = None,
    checked_at: str | None = None,
    control_writer: ControlWriter,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    with serialized_control_operation(project):
        return _sync_authenticated_team(
            project,
            adapter=adapter,
            coordinator_integration_id=coordinator_integration_id,
            expected_revision=expected_revision,
            sync_id=sync_id,
            checked_at=checked_at,
            control_writer=control_writer,
            git_environment=git_environment,
        )


def invite_authenticated_member(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedTeamAdapter,
    manager: AuthenticatedCollaboratorManager,
    username: str,
    permission: str,
    coordinator_integration_id: int,
    expected_revision: int,
    request_id: str,
    control_writer: ControlWriter,
    invited_at: str | None = None,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if OPERATION_RE.fullmatch(request_id) is None:
        raise ConfigurationError("team invitation request id is invalid")
    if not isinstance(username, str) or OWNER_RE.fullmatch(username) is None:
        raise ConfigurationError("team invitation GitHub username is invalid")
    if permission not in {"pull", "triage", "push", "maintain", "admin"}:
        raise ConfigurationError("team invitation permission is invalid")
    with serialized_control_operation(project):
        contract = _contract(project)
        inspection = adapter.inspect_collaboration(
            repository_id=contract.repository_id,
            control_branch=contract.control_branch,
        )
        if (
            inspection.provider != contract.provider
            or inspection.repository_id != contract.repository_id
            or not inspection.membership.active
            or "admin" not in inspection.membership.roles
        ):
            raise WorkflowError("team invitation requires active repository admin membership")
        if not inspection.protection.coordinator_only:
            raise WorkflowError("team invitation requires protected aria-control")
        journal_path = _external_journal(
            project,
            request_id=request_id,
            operation="invite",
            username=username,
            permission=permission,
            expected_revision=expected_revision,
            phase="prepared",
        )
        journal = _load_external_journal(journal_path)
        if journal.get("phase") == "committed":
            status = collaborative_team_status(project)
            receipt_value = journal.get("receipt")
            state = receipt_value.get("state") if isinstance(receipt_value, dict) else None
            return {
                **status,
                "request_id": request_id,
                "invitation_state": state,
                "recovered": True,
                "control_commit": control_writer.read_head(),
                "remote_recovered": True,
            }
        team_path = project.docs_root / "ARIA_TEAM.yaml"
        if team_path.exists() and any(
            event.get("sync_id") == request_id
            for event in load_collaborative_team(team_path)["events"]
        ):
            remote = recover_pending_control_sync(
                project, writer=control_writer, git_environment=git_environment
            )
            receipt_value = journal.get("receipt")
            state = receipt_value.get("state") if isinstance(receipt_value, dict) else None
            _external_journal(
                project, request_id=request_id, operation="invite", username=username,
                permission=permission, expected_revision=expected_revision,
                phase="committed", receipt=receipt_value if isinstance(receipt_value, dict) else None,
            )
            return {
                **collaborative_team_status(project),
                "request_id": request_id,
                "invitation_state": state,
                "recovered": True,
                "control_commit": remote["control_commit"] if remote else control_writer.read_head(),
                "remote_recovered": remote is not None,
            }
        _require_team_revision(project, expected_revision=expected_revision)
        if journal.get("phase") == "external_applied":
            receipt_value = journal.get("receipt")
            if not isinstance(receipt_value, dict):
                raise ConfigurationError("team invitation recovery receipt is invalid")
            receipt = GitHubInvitationReceipt(
                None
                if receipt_value.get("state") == "active"
                else str(receipt_value.get("invitation_id")),
                _actor_from_receipt(receipt_value.get("actor")),
                str(receipt_value.get("state")),
                permission,
            )
        else:
            reader = getattr(manager, "read_invitation", None)
            receipt = (
                reader(username=username, permission=permission)
                if callable(reader)
                else None
            )
            if receipt is None:
                receipt = manager.invite(username=username, permission=permission)
            _external_journal(
                project,
                request_id=request_id,
                operation="invite",
                username=username,
                permission=permission,
                expected_revision=expected_revision,
                phase="external_applied",
                receipt={
                    "state": receipt.state,
                    "invitation_id": receipt.invitation_id,
                    "actor": receipt.actor.as_mapping(),
                },
            )
        if receipt.state == "active":
            prepare_control_mutation(project, operation_id=request_id)
            result = _sync_authenticated_team(
                project,
                adapter=adapter,
                coordinator_integration_id=coordinator_integration_id,
                expected_revision=expected_revision,
                sync_id=request_id,
                checked_at=invited_at,
                control_writer=control_writer,
                git_environment=git_environment,
            )
            _external_journal(
                project, request_id=request_id, operation="invite", username=username,
                permission=permission, expected_revision=expected_revision,
                phase="committed", receipt={"state": "active", "actor": receipt.actor.as_mapping()},
            )
            return {**result, "invitation_state": "active"}
        observed_at = invited_at or datetime.now(UTC).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        coordinator = ProviderIdentity(
            provider="github-app",
            actor=ProviderActor(
                str(coordinator_integration_id),
                "aria-coordinator",
                "ARIA Coordinator",
            ),
        )
        prepare_control_mutation(project, operation_id=request_id)
        result = record_collaborative_team_invitation(
            control_root=project.docs_root,
            runtime_root=_machine_runtime_root(project),
            project_id=project.project_id,
            provider=contract.provider,
            repository_id=contract.repository_id,
            target=ProviderIdentity(contract.provider, receipt.actor),
            invited_by=ProviderIdentity(contract.provider, inspection.actor),
            coordinator=coordinator,
            invitation_id=str(receipt.invitation_id),
            request_id=request_id,
            expected_revision=expected_revision,
            invited_at=observed_at,
        )
        remote = synchronize_control_worktree(
            project,
            writer=control_writer,
            operation_id=request_id,
            git_environment=git_environment,
        )
        _external_journal(
            project, request_id=request_id, operation="invite", username=username,
            permission=permission, expected_revision=expected_revision,
            phase="committed", receipt={"state": "invited", "actor": receipt.actor.as_mapping()},
        )
        return {
            "ok": True,
            "project": project.project_id,
            "request_id": request_id,
            "invitation_state": "invited",
            "target": receipt.actor.as_mapping(),
            "revision": result["revision"],
            "recovered": result["recovered"],
            "control_commit": remote["control_commit"],
            "remote_recovered": remote["recovered"],
        }


def revoke_authenticated_member(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedTeamAdapter,
    manager: AuthenticatedCollaboratorManager,
    username: str,
    coordinator_integration_id: int,
    expected_revision: int,
    request_id: str,
    control_writer: ControlWriter,
    checked_at: str | None = None,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if OPERATION_RE.fullmatch(request_id) is None:
        raise ConfigurationError("team revocation request id is invalid")
    if not isinstance(username, str) or OWNER_RE.fullmatch(username) is None:
        raise ConfigurationError("team revocation GitHub username is invalid")
    with serialized_control_operation(project):
        contract = _contract(project)
        inspection = adapter.inspect_collaboration(
            repository_id=contract.repository_id,
            control_branch=contract.control_branch,
        )
        if (
            not inspection.membership.active
            or "admin" not in inspection.membership.roles
            or not inspection.protection.coordinator_only
        ):
            raise WorkflowError("team revocation requires active repository admin membership")
        journal_path = _external_journal(
            project,
            request_id=request_id,
            operation="revoke",
            username=username,
            permission=None,
            expected_revision=expected_revision,
            phase="prepared",
        )
        journal = _load_external_journal(journal_path)
        if journal.get("phase") == "committed":
            receipt_value = journal.get("receipt")
            actor = _actor_from_receipt(
                receipt_value.get("actor") if isinstance(receipt_value, dict) else None
            )
            return {
                **collaborative_team_status(project),
                "revoked_actor": actor.as_mapping(),
                "recovered": True,
                "control_commit": control_writer.read_head(),
                "remote_recovered": True,
            }
        team_path = project.docs_root / "ARIA_TEAM.yaml"
        if team_path.exists() and any(
            event.get("sync_id") == request_id
            for event in load_collaborative_team(team_path)["events"]
        ):
            remote = recover_pending_control_sync(
                project, writer=control_writer, git_environment=git_environment
            )
            receipt_value = journal.get("receipt")
            actor = _actor_from_receipt(
                receipt_value.get("actor") if isinstance(receipt_value, dict) else None
            )
            _external_journal(
                project, request_id=request_id, operation="revoke", username=username,
                permission=None, expected_revision=expected_revision,
                phase="committed", receipt={"actor": actor.as_mapping()},
            )
            return {
                **collaborative_team_status(project),
                "revoked_actor": actor.as_mapping(),
                "recovered": True,
                "control_commit": remote["control_commit"] if remote else control_writer.read_head(),
                "remote_recovered": remote is not None,
            }
        _require_team_revision(project, expected_revision=expected_revision)
        if journal.get("phase") == "external_applied":
            receipt_value = journal.get("receipt")
            revoked = _actor_from_receipt(
                receipt_value.get("actor") if isinstance(receipt_value, dict) else None
            )
        else:
            reader = getattr(manager, "read_revocation", None)
            revoked = reader(username=username) if callable(reader) else None
            if revoked is None:
                revoked = manager.revoke(username=username)
            _external_journal(
                project,
                request_id=request_id,
                operation="revoke",
                username=username,
                permission=None,
                expected_revision=expected_revision,
                phase="external_applied",
                receipt={"actor": revoked.as_mapping()},
            )
        prepare_control_mutation(project, operation_id=request_id)
        result = _sync_authenticated_team(
            project,
            adapter=adapter,
            coordinator_integration_id=coordinator_integration_id,
            expected_revision=expected_revision,
            sync_id=request_id,
            checked_at=checked_at,
            control_writer=control_writer,
            git_environment=git_environment,
        )
        _external_journal(
            project, request_id=request_id, operation="revoke", username=username,
            permission=None, expected_revision=expected_revision,
            phase="committed", receipt={"actor": revoked.as_mapping()},
        )
        return {**result, "revoked_actor": revoked.as_mapping()}
