from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aria.activity import EVENT_ID_RE
from aria.collaborative_backlog import ProviderIdentity
from aria.errors import ConfigurationError, WorkflowError
from aria.project import PROJECT_ID_RE
from aria.provider import PROVIDER_ID_RE, ProviderActor, ProviderTeamMember, ROLE_RE


COLLABORATIVE_TEAM_SCHEMA_VERSION = 2


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} must be a string-keyed mapping")
    return value


def _exact(value: dict[str, object], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise ConfigurationError(f"{label} schema keys are invalid")


def _stamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ConfigurationError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"{label} is invalid") from error
    if parsed.astimezone(UTC) != parsed:
        raise ConfigurationError(f"{label} must be UTC")
    return value


def _stamp_value(value: object, label: str) -> datetime:
    stamp = _stamp(value, label)
    return datetime.fromisoformat(stamp[:-1] + "+00:00").astimezone(UTC)


def _sha(value: object) -> str:
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _is_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _provider_identity(value: object, label: str) -> ProviderIdentity:
    raw = _mapping(value, label)
    _exact(
        raw,
        {"provider", "user_id", "username_snapshot", "display_name_snapshot"},
        label,
    )
    return ProviderIdentity(
        provider=raw.get("provider"),
        actor=ProviderActor(
            user_id=raw.get("user_id"),
            username_snapshot=raw.get("username_snapshot"),
            display_name_snapshot=raw.get("display_name_snapshot"),
        ),
    )


def collaborative_team_template(
    project_id: str,
    *,
    provider: str,
    repository_id: str,
) -> dict[str, object]:
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    if not isinstance(provider, str) or PROVIDER_ID_RE.fullmatch(provider) is None:
        raise ConfigurationError("team provider is invalid")
    if not isinstance(repository_id, str) or not repository_id.strip():
        raise ConfigurationError("team repository_id is invalid")
    return {
        "schema_version": COLLABORATIVE_TEAM_SCHEMA_VERSION,
        "project_id": project_id,
        "provider": provider,
        "repository_id": repository_id,
        "revision": 0,
        "checked_at": None,
        "members": [],
        "events": [],
    }


def _validate_member(value: object, index: int, provider: str) -> dict[str, object]:
    member = _mapping(value, f"team member {index}")
    legacy_keys = {
            "provider",
            "user_id",
            "username_snapshot",
            "display_name_snapshot",
            "username_history",
            "active",
            "roles",
            "first_seen_at",
            "last_changed_at",
            "revoked_at",
        }
    lifecycle_keys = legacy_keys | {"status", "invited_by", "invited_at", "invitation_id"}
    if frozenset(member) not in {frozenset(legacy_keys), frozenset(lifecycle_keys)}:
        raise ConfigurationError(f"team member {index} schema keys are invalid")
    if member.get("provider") != provider:
        raise ConfigurationError("team member provider mismatch")
    actor = ProviderActor(
        user_id=member.get("user_id"),
        username_snapshot=member.get("username_snapshot"),
        display_name_snapshot=member.get("display_name_snapshot"),
    )
    history = member.get("username_history")
    if not isinstance(history, list) or not history:
        raise ConfigurationError("team username history is invalid")
    for history_index, history_value in enumerate(history):
        row = _mapping(history_value, f"team username history {history_index}")
        _exact(row, {"username", "observed_at"}, "team username history")
        ProviderActor(user_id=actor.user_id, username_snapshot=row.get("username"))
        _stamp(row.get("observed_at"), "team username observed_at")
    if history[-1]["username"] != actor.username_snapshot:
        raise ConfigurationError("team username history does not match current snapshot")
    if type(member.get("active")) is not bool:
        raise ConfigurationError("team member active flag is invalid")
    status = member.get("status", "active" if member["active"] else "revoked")
    if status not in {"invited", "active", "revoked"}:
        raise ConfigurationError("team member lifecycle status is invalid")
    if member["active"] is not (status == "active"):
        raise ConfigurationError("team member active flag/status mismatch")
    roles = member.get("roles")
    if (
        not isinstance(roles, list)
        or roles != sorted(set(roles))
        or any(not isinstance(role, str) or ROLE_RE.fullmatch(role) is None for role in roles)
    ):
        raise ConfigurationError("team member roles are invalid")
    if member["active"] and not roles:
        raise ConfigurationError("active team member requires roles")
    if not member["active"] and roles:
        raise ConfigurationError("revoked team member cannot retain roles")
    first_seen = _stamp_value(member.get("first_seen_at"), "team first_seen_at")
    last_changed = _stamp_value(
        member.get("last_changed_at"), "team last_changed_at"
    )
    if last_changed < first_seen:
        raise ConfigurationError("team member timestamps are out of order")
    if status == "active":
        if member.get("revoked_at") is not None:
            raise ConfigurationError("active team member cannot have revoked_at")
    elif status == "revoked":
        revoked = _stamp_value(member.get("revoked_at"), "team revoked_at")
        if revoked != last_changed:
            raise ConfigurationError("team revoked_at must match last_changed_at")
    elif member.get("revoked_at") is not None:
        raise ConfigurationError("invited team member cannot have revoked_at")
    if set(member) == lifecycle_keys:
        invited_by = member.get("invited_by")
        invited_at = member.get("invited_at")
        invitation_id = member.get("invitation_id")
        if status == "invited":
            _provider_identity(invited_by, "team invited_by")
            invitation_time = _stamp_value(invited_at, "team invited_at")
            if invitation_time != last_changed or invitation_time < first_seen:
                raise ConfigurationError("team invitation timestamps are inconsistent")
            if not isinstance(invitation_id, str) or not invitation_id.isdecimal():
                raise ConfigurationError("team invitation_id is invalid")
        elif invited_by is not None:
            _provider_identity(invited_by, "team invited_by")
            _stamp(invited_at, "team invited_at")
            if invitation_id is not None:
                raise ConfigurationError("non-pending member cannot retain invitation_id")
        elif invited_at is not None or invitation_id is not None:
            raise ConfigurationError("team invitation provenance is incomplete")
    return member


def validate_collaborative_team(value: object) -> dict[str, object]:
    raw = _mapping(value, "ARIA_TEAM.yaml v2")
    _exact(
        raw,
        {
            "schema_version",
            "project_id",
            "provider",
            "repository_id",
            "revision",
            "checked_at",
            "members",
            "events",
        },
        "ARIA_TEAM.yaml v2",
    )
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 2:
        raise ConfigurationError("collaborative team schema_version must be 2")
    template = collaborative_team_template(
        raw.get("project_id"),
        provider=raw.get("provider"),
        repository_id=raw.get("repository_id"),
    )
    revision = raw.get("revision")
    if type(revision) is not int or revision < 0:
        raise ConfigurationError("collaborative team revision is invalid")
    members_value = raw.get("members")
    events = raw.get("events")
    if not isinstance(members_value, list) or not isinstance(events, list):
        raise ConfigurationError("collaborative team collections are invalid")
    members = [
        _validate_member(value, index, str(raw["provider"]))
        for index, value in enumerate(members_value)
    ]
    keys = [(member["provider"], member["user_id"]) for member in members]
    if keys != sorted(set(keys)):
        raise ConfigurationError("team members must be unique and sorted")
    if revision == 0:
        if raw != template:
            raise ConfigurationError("collaborative team revision zero must be empty")
        return raw
    checked_at = _stamp_value(raw.get("checked_at"), "team checked_at")
    if any(
        _stamp_value(member["last_changed_at"], "team last_changed_at") > checked_at
        for member in members
    ):
        raise ConfigurationError("team member change is newer than team snapshot")
    if len(events) != revision:
        raise ConfigurationError("team revision/event count mismatch")
    previous: str | None = None
    sync_ids: set[str] = set()
    for index, event_value in enumerate(events, start=1):
        event = _mapping(event_value, f"team event {index}")
        _exact(
            event,
            {
                "schema_version",
                "sequence",
                "project_id",
                "provider",
                "repository_id",
                "sync_id",
                "provider_snapshot_sha256",
                "added_user_ids",
                "updated_user_ids",
                "revoked_user_ids",
                "checked_at",
                "committed_by",
                "members_sha256",
                "previous_event_sha256",
                "event_sha256",
            },
            f"team event {index}",
        )
        if (
            type(event.get("schema_version")) is not int
            or event["schema_version"] != 1
            or type(event.get("sequence")) is not int
            or event["sequence"] != index
            or event.get("project_id") != raw["project_id"]
            or event.get("provider") != raw["provider"]
            or event.get("repository_id") != raw["repository_id"]
        ):
            raise ConfigurationError("team event identity is invalid")
        sync_id = event.get("sync_id")
        if (
            not isinstance(sync_id, str)
            or EVENT_ID_RE.fullmatch(sync_id) is None
            or sync_id in sync_ids
        ):
            raise ConfigurationError("team event sync_id is invalid or duplicate")
        sync_ids.add(sync_id)
        for field in (
            "provider_snapshot_sha256",
            "members_sha256",
            "event_sha256",
        ):
            if not _is_sha(event.get(field)):
                raise ConfigurationError(f"team event {field} is invalid")
        for field in ("added_user_ids", "updated_user_ids", "revoked_user_ids"):
            ids = event.get(field)
            if (
                not isinstance(ids, list)
                or ids != sorted(set(ids))
                or any(
                    not isinstance(user_id, str)
                    or not user_id
                    or user_id != user_id.strip()
                    or len(user_id) > 256
                    for user_id in ids
                )
            ):
                raise ConfigurationError(f"team event {field} is invalid")
        event_checked_at = _stamp_value(
            event.get("checked_at"), "team event checked_at"
        )
        if index > 1:
            prior_checked_at = _stamp_value(
                events[index - 2]["checked_at"], "prior team event checked_at"
            )
            if event_checked_at < prior_checked_at:
                raise ConfigurationError("team event timestamps are out of order")
        _identity = event.get("committed_by")
        identity = _mapping(_identity, "team committed_by")
        _exact(
            identity,
            {
                "provider",
                "user_id",
                "username_snapshot",
                "display_name_snapshot",
            },
            "team committed_by",
        )
        ProviderIdentity(
            provider=identity.get("provider"),
            actor=ProviderActor(
                user_id=identity.get("user_id"),
                username_snapshot=identity.get("username_snapshot"),
                display_name_snapshot=identity.get("display_name_snapshot"),
            ),
        )
        if event.get("previous_event_sha256") != previous:
            raise WorkflowError("collaborative team event chain is broken")
        calculated = _sha({key: val for key, val in event.items() if key != "event_sha256"})
        if event["event_sha256"] != calculated:
            raise WorkflowError("collaborative team event hash is invalid")
        previous = str(event["event_sha256"])
    if raw["checked_at"] != events[-1]["checked_at"]:
        raise ConfigurationError("team checked_at does not match audit head")
    if events[-1]["members_sha256"] != _sha(members):
        raise WorkflowError("collaborative team state does not match audit head")
    return raw


def dump_collaborative_team(value: object) -> str:
    validated = validate_collaborative_team(value)
    content = yaml.safe_dump(validated, allow_unicode=True, sort_keys=False)
    if validate_collaborative_team(yaml.safe_load(content)) != validated:
        raise ConfigurationError("collaborative team deterministic round-trip failed")
    return content


def load_collaborative_team(path: Path) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot read collaborative team: {path}") from error
    return validate_collaborative_team(value)


def _provider_snapshot(members: tuple[ProviderTeamMember, ...]) -> list[dict[str, object]]:
    return [member.as_mapping() for member in sorted(members, key=lambda row: row.actor.user_id)]


def sync_collaborative_team(
    team_value: object,
    provider_members: tuple[ProviderTeamMember, ...],
    *,
    sync_id: str,
    coordinator: ProviderIdentity,
    expected_revision: int,
    checked_at: str,
) -> dict[str, object]:
    team = validate_collaborative_team(team_value)
    _stamp(checked_at, "team sync checked_at")
    if not isinstance(sync_id, str) or EVENT_ID_RE.fullmatch(sync_id) is None:
        raise ConfigurationError("team sync_id is invalid")
    if not isinstance(provider_members, tuple) or any(
        not isinstance(member, ProviderTeamMember) for member in provider_members
    ):
        raise ConfigurationError("provider team snapshot is invalid")
    snapshot = _provider_snapshot(provider_members)
    snapshot_hash = _sha(snapshot)
    existing_event = next(
        (event for event in team["events"] if event["sync_id"] == sync_id), None
    )
    if existing_event is not None:
        if existing_event["provider_snapshot_sha256"] != snapshot_hash:
            raise WorkflowError("team sync id was reused with a different provider snapshot")
        return {"applied": False, "reason": "duplicate_sync", "team": team}
    if team["revision"] != expected_revision:
        raise WorkflowError(
            f"Stale collaborative team revision: expected {expected_revision}, "
            f"actual {team['revision']}"
        )
    if team["checked_at"] is not None and _stamp_value(
        checked_at, "team sync checked_at"
    ) < _stamp_value(team["checked_at"], "team checked_at"):
        raise WorkflowError("team sync timestamp is older than the current snapshot")
    incoming: dict[tuple[str, str], ProviderTeamMember] = {}
    for member in provider_members:
        key = (member.provider, member.actor.user_id)
        if member.provider != team["provider"]:
            raise WorkflowError("provider team snapshot belongs to another provider")
        if key in incoming:
            raise WorkflowError("provider team snapshot contains duplicate identity")
        incoming[key] = member
    current = {
        (str(member["provider"]), str(member["user_id"])): dict(member)
        for member in team["members"]
    }
    for member in current.values():
        member.setdefault("status", "active" if member["active"] else "revoked")
        member.setdefault("invited_by", None)
        member.setdefault("invited_at", None)
        member.setdefault("invitation_id", None)
    added: list[str] = []
    updated: list[str] = []
    revoked: list[str] = []
    for key, provider_member in incoming.items():
        actor = provider_member.actor
        roles = list(provider_member.membership.roles)
        member = current.get(key)
        if member is None:
            current[key] = {
                "provider": provider_member.provider,
                "user_id": actor.user_id,
                "username_snapshot": actor.username_snapshot,
                "display_name_snapshot": actor.display_name_snapshot,
                "username_history": [
                    {"username": actor.username_snapshot, "observed_at": checked_at}
                ],
                "active": True,
                "status": "active",
                "roles": roles,
                "first_seen_at": checked_at,
                "last_changed_at": checked_at,
                "revoked_at": None,
                "invited_by": None,
                "invited_at": None,
                "invitation_id": None,
            }
            added.append(actor.user_id)
            continue
        changed = (
            not member["active"]
            or member.get("status") != "active"
            or member["username_snapshot"] != actor.username_snapshot
            or member["display_name_snapshot"] != actor.display_name_snapshot
            or member["roles"] != roles
        )
        if member["username_snapshot"] != actor.username_snapshot:
            member["username_history"] = [
                *member["username_history"],
                {"username": actor.username_snapshot, "observed_at": checked_at},
            ]
        member.update(
            {
                "username_snapshot": actor.username_snapshot,
                "display_name_snapshot": actor.display_name_snapshot,
                "active": True,
                "status": "active",
                "roles": roles,
                "revoked_at": None,
                "invitation_id": None,
            }
        )
        if changed:
            member["last_changed_at"] = checked_at
            updated.append(actor.user_id)
    for key, member in current.items():
        if key not in incoming and member["active"]:
            member["active"] = False
            member["status"] = "revoked"
            member["roles"] = []
            member["last_changed_at"] = checked_at
            member["revoked_at"] = checked_at
            revoked.append(str(member["user_id"]))
    members = [current[key] for key in sorted(current)]
    revision = int(team["revision"]) + 1
    previous = team["events"][-1]["event_sha256"] if team["events"] else None
    event = {
        "schema_version": 1,
        "sequence": revision,
        "project_id": team["project_id"],
        "provider": team["provider"],
        "repository_id": team["repository_id"],
        "sync_id": sync_id,
        "provider_snapshot_sha256": snapshot_hash,
        "added_user_ids": sorted(added),
        "updated_user_ids": sorted(updated),
        "revoked_user_ids": sorted(revoked),
        "checked_at": checked_at,
        "committed_by": coordinator.as_mapping(),
        "members_sha256": _sha(members),
        "previous_event_sha256": previous,
    }
    event["event_sha256"] = _sha(event)
    result = {
        **team,
        "revision": revision,
        "checked_at": checked_at,
        "members": members,
        "events": [*team["events"], event],
    }
    validate_collaborative_team(result)
    return {"applied": True, "reason": None, "team": result}


def active_team_identities(team_value: object) -> tuple[ProviderIdentity, ...]:
    team = validate_collaborative_team(team_value)
    return tuple(
        ProviderIdentity(
            provider=str(member["provider"]),
            actor=ProviderActor(
                user_id=str(member["user_id"]),
                username_snapshot=str(member["username_snapshot"]),
                display_name_snapshot=member["display_name_snapshot"],
            ),
        )
        for member in team["members"]
        if member["active"] and member.get("status", "active") == "active"
    )


def record_team_invitation(
    team_value: object,
    *,
    target: ProviderIdentity,
    invited_by: ProviderIdentity,
    coordinator: ProviderIdentity,
    invitation_id: str,
    request_id: str,
    expected_revision: int,
    invited_at: str,
) -> dict[str, object]:
    team = validate_collaborative_team(team_value)
    if target.provider != team["provider"] or invited_by.provider != team["provider"]:
        raise WorkflowError("team invitation identity provider mismatch")
    if coordinator.key in {target.key, invited_by.key}:
        raise WorkflowError("coordinator cannot replace invitation participants")
    if not isinstance(invitation_id, str) or not invitation_id.isdecimal():
        raise ConfigurationError("team invitation id is invalid")
    if not isinstance(request_id, str) or EVENT_ID_RE.fullmatch(request_id) is None:
        raise ConfigurationError("team invitation request id is invalid")
    _stamp(invited_at, "team invitation timestamp")
    snapshot = {
        "target": target.as_mapping(),
        "invited_by": invited_by.as_mapping(),
        "invitation_id": invitation_id,
    }
    snapshot_hash = _sha(snapshot)
    existing_event = next(
        (event for event in team["events"] if event["sync_id"] == request_id), None
    )
    if existing_event is not None:
        if existing_event["provider_snapshot_sha256"] != snapshot_hash:
            raise WorkflowError("team invitation request id was reused with different content")
        return {"applied": False, "reason": "duplicate_invitation", "team": team}
    if team["revision"] != expected_revision:
        raise WorkflowError(
            f"Stale collaborative team revision: expected {expected_revision}, "
            f"actual {team['revision']}"
        )
    members = [dict(member) for member in team["members"]]
    existing = next(
        (member for member in members if (member["provider"], member["user_id"]) == target.key),
        None,
    )
    if existing is not None and existing.get("status", "active" if existing["active"] else "revoked") == "active":
        raise WorkflowError("GitHub user is already an active project member")
    member = {
        "provider": target.provider,
        "user_id": target.actor.user_id,
        "username_snapshot": target.actor.username_snapshot,
        "display_name_snapshot": target.actor.display_name_snapshot,
        "username_history": [
            {"username": target.actor.username_snapshot, "observed_at": invited_at}
        ],
        "active": False,
        "status": "invited",
        "roles": [],
        "first_seen_at": invited_at,
        "last_changed_at": invited_at,
        "revoked_at": None,
        "invited_by": invited_by.as_mapping(),
        "invited_at": invited_at,
        "invitation_id": invitation_id,
    }
    if existing is None:
        members.append(member)
        added = [target.actor.user_id]
        updated: list[str] = []
    else:
        member["first_seen_at"] = existing["first_seen_at"]
        member["username_history"] = [
            *existing["username_history"],
            *(
                [{"username": target.actor.username_snapshot, "observed_at": invited_at}]
                if existing["username_snapshot"] != target.actor.username_snapshot
                else []
            ),
        ]
        members[members.index(existing)] = member
        added = []
        updated = [target.actor.user_id]
    members.sort(key=lambda row: (str(row["provider"]), str(row["user_id"])))
    revision = int(team["revision"]) + 1
    previous = team["events"][-1]["event_sha256"] if team["events"] else None
    event = {
        "schema_version": 1,
        "sequence": revision,
        "project_id": team["project_id"],
        "provider": team["provider"],
        "repository_id": team["repository_id"],
        "sync_id": request_id,
        "provider_snapshot_sha256": snapshot_hash,
        "added_user_ids": added,
        "updated_user_ids": updated,
        "revoked_user_ids": [],
        "checked_at": invited_at,
        "committed_by": coordinator.as_mapping(),
        "members_sha256": _sha(members),
        "previous_event_sha256": previous,
    }
    event["event_sha256"] = _sha(event)
    result = {
        **team,
        "revision": revision,
        "checked_at": invited_at,
        "members": members,
        "events": [*team["events"], event],
    }
    validate_collaborative_team(result)
    return {"applied": True, "reason": None, "team": result}
