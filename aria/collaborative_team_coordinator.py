from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from aria.activity import EVENT_ID_RE
from aria.collaborative_backlog import ProviderIdentity
from aria.collaborative_team import (
    collaborative_team_template,
    dump_collaborative_team,
    load_collaborative_team,
    record_team_invitation,
    sync_collaborative_team,
    validate_collaborative_team,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE
from aria.provider import PROVIDER_ID_RE, ProviderTeamMember


TEAM_TRANSACTION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CollaborativeTeamCoordinatorPaths:
    team: Path
    transaction: Path
    lock: Path


def collaborative_team_coordinator_paths(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
) -> CollaborativeTeamCoordinatorPaths:
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    runtime_team = runtime_root / "collaborative-team" / project_id
    return CollaborativeTeamCoordinatorPaths(
        team=control_root / "ARIA_TEAM.yaml",
        transaction=runtime_team / "transaction.json",
        lock=runtime_root / "locks" / f"collaborative-team-{project_id}.lock",
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} must be a string-keyed mapping")
    return value


def _exact(value: dict[str, object], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise ConfigurationError(f"{label} schema keys are invalid")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _team_bytes(team: object) -> bytes:
    return dump_collaborative_team(team).encode("utf-8")


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Cannot read {label}: {path}") from error


def _validate_transaction(value: object, project_id: str) -> dict[str, object]:
    raw = _mapping(value, "collaborative team transaction")
    _exact(
        raw,
        {
            "schema_version",
            "project_id",
            "sync_id",
            "before_exists",
            "before",
            "before_sha256",
            "after",
            "after_sha256",
        },
        "collaborative team transaction",
    )
    if (
        type(raw.get("schema_version")) is not int
        or raw["schema_version"] != TEAM_TRANSACTION_SCHEMA_VERSION
    ):
        raise ConfigurationError("collaborative team transaction schema is invalid")
    if raw.get("project_id") != project_id:
        raise ConfigurationError("collaborative team transaction belongs to another project")
    sync_id = raw.get("sync_id")
    if not isinstance(sync_id, str) or EVENT_ID_RE.fullmatch(sync_id) is None:
        raise ConfigurationError("collaborative team transaction sync id is invalid")
    if type(raw.get("before_exists")) is not bool:
        raise ConfigurationError("collaborative team transaction before_exists is invalid")
    before = validate_collaborative_team(raw.get("before"))
    after = validate_collaborative_team(raw.get("after"))
    if before["project_id"] != project_id or after["project_id"] != project_id:
        raise ConfigurationError("collaborative team transaction project mismatch")
    if (
        before["provider"] != after["provider"]
        or before["repository_id"] != after["repository_id"]
    ):
        raise ConfigurationError("collaborative team transaction identity changed")
    before_hash = raw.get("before_sha256")
    if raw["before_exists"]:
        if not _is_sha256(before_hash) or before_hash != _sha256(_team_bytes(before)):
            raise ConfigurationError("collaborative team transaction before hash is invalid")
    elif before_hash is not None:
        raise ConfigurationError("missing team transaction before hash must be null")
    after_hash = raw.get("after_sha256")
    if not _is_sha256(after_hash) or after_hash != _sha256(_team_bytes(after)):
        raise ConfigurationError("collaborative team transaction after hash is invalid")
    if int(after["revision"]) != int(before["revision"]) + 1:
        raise ConfigurationError("collaborative team transaction revision is invalid")
    if not after["events"] or after["events"][-1]["sync_id"] != sync_id:
        raise ConfigurationError("collaborative team transaction sync does not match audit")
    return raw


def _install(path: Path, content: bytes) -> None:
    atomic_write_bytes(path, content)


def _recover_transaction(
    paths: CollaborativeTeamCoordinatorPaths,
    project_id: str,
) -> bool:
    if not paths.transaction.exists():
        return False
    transaction = _validate_transaction(
        _load_json(paths.transaction, "collaborative team transaction"), project_id
    )
    after = validate_collaborative_team(transaction["after"])
    after_content = _team_bytes(after)
    if not paths.team.exists():
        if transaction["before_exists"]:
            raise WorkflowError("collaborative team recovery found a missing preimage")
        state = "before"
    else:
        current_hash = _sha256(paths.team.read_bytes())
        if current_hash == transaction["after_sha256"]:
            state = "after"
        elif transaction["before_exists"] and current_hash == transaction["before_sha256"]:
            state = "before"
        else:
            raise WorkflowError(
                "collaborative team recovery found state outside the prepared transaction"
            )
    if state == "before":
        _install(paths.team, after_content)
    if paths.team.read_bytes() != after_content:
        raise WorkflowError("collaborative team recovery read-back failed")
    paths.transaction.unlink()
    return True


def sync_collaborative_team_snapshot(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
    provider: str,
    repository_id: str,
    provider_members: tuple[ProviderTeamMember, ...],
    sync_id: str,
    coordinator: ProviderIdentity,
    expected_revision: int,
    checked_at: str,
) -> dict[str, object]:
    if not isinstance(provider, str) or PROVIDER_ID_RE.fullmatch(provider) is None:
        raise ConfigurationError("collaborative team provider is invalid")
    paths = collaborative_team_coordinator_paths(
        control_root=control_root,
        runtime_root=runtime_root,
        project_id=project_id,
    )
    with exclusive_lock(paths.lock):
        recovered = _recover_transaction(paths, project_id)
        team = (
            load_collaborative_team(paths.team)
            if paths.team.exists()
            else collaborative_team_template(
                project_id, provider=provider, repository_id=repository_id
            )
        )
        if team["provider"] != provider or team["repository_id"] != repository_id:
            raise WorkflowError("collaborative team identity does not match this repository")
        result = sync_collaborative_team(
            team,
            provider_members,
            sync_id=sync_id,
            coordinator=coordinator,
            expected_revision=expected_revision,
            checked_at=checked_at,
        )
        if not result["applied"]:
            return {**result, "recovered": recovered, "revision": team["revision"]}
        updated = validate_collaborative_team(result["team"])
        before_exists = paths.team.exists()
        before_content = _team_bytes(team)
        after_content = _team_bytes(updated)
        transaction = {
            "schema_version": TEAM_TRANSACTION_SCHEMA_VERSION,
            "project_id": project_id,
            "sync_id": sync_id,
            "before_exists": before_exists,
            "before": team,
            "before_sha256": _sha256(before_content) if before_exists else None,
            "after": updated,
            "after_sha256": _sha256(after_content),
        }
        _validate_transaction(transaction, project_id)
        _install(paths.transaction, json_bytes(transaction))
        _install(paths.team, after_content)
        if paths.team.read_bytes() != after_content:
            raise WorkflowError("collaborative team coordinator read-back failed")
        paths.transaction.unlink()
        return {**result, "recovered": recovered, "revision": updated["revision"]}


def record_collaborative_team_invitation(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
    provider: str,
    repository_id: str,
    target: ProviderIdentity,
    invited_by: ProviderIdentity,
    coordinator: ProviderIdentity,
    invitation_id: str,
    request_id: str,
    expected_revision: int,
    invited_at: str,
) -> dict[str, object]:
    paths = collaborative_team_coordinator_paths(
        control_root=control_root, runtime_root=runtime_root, project_id=project_id
    )
    with exclusive_lock(paths.lock):
        recovered = _recover_transaction(paths, project_id)
        team = (
            load_collaborative_team(paths.team)
            if paths.team.exists()
            else collaborative_team_template(
                project_id, provider=provider, repository_id=repository_id
            )
        )
        if team["provider"] != provider or team["repository_id"] != repository_id:
            raise WorkflowError("collaborative team identity does not match this repository")
        result = record_team_invitation(
            team,
            target=target,
            invited_by=invited_by,
            coordinator=coordinator,
            invitation_id=invitation_id,
            request_id=request_id,
            expected_revision=expected_revision,
            invited_at=invited_at,
        )
        if not result["applied"]:
            return {**result, "recovered": recovered, "revision": team["revision"]}
        updated = validate_collaborative_team(result["team"])
        before_exists = paths.team.exists()
        before_content = _team_bytes(team)
        after_content = _team_bytes(updated)
        transaction = {
            "schema_version": TEAM_TRANSACTION_SCHEMA_VERSION,
            "project_id": project_id,
            "sync_id": request_id,
            "before_exists": before_exists,
            "before": team,
            "before_sha256": _sha256(before_content) if before_exists else None,
            "after": updated,
            "after_sha256": _sha256(after_content),
        }
        _validate_transaction(transaction, project_id)
        _install(paths.transaction, json_bytes(transaction))
        _install(paths.team, after_content)
        if paths.team.read_bytes() != after_content:
            raise WorkflowError("collaborative team invitation read-back failed")
        paths.transaction.unlink()
        return {**result, "recovered": recovered, "revision": updated["revision"]}
