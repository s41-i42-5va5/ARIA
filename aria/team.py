from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_json, exclusive_lock

ACTOR_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
ACTOR_ROLES = {
    "contributor",
    "reviewer",
    "maintainer",
    "release-manager",
    "ci",
}
CLAIM_ROLES = {"contributor", "maintainer", "release-manager", "ci"}
MAX_LEASE_SECONDS = 86400


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise WorkflowError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise WorkflowError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise WorkflowError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(UTC)


def load_team(project: object) -> dict[str, dict[str, object]]:
    files = getattr(project, "files", None)
    relative = getattr(files, "team", None) if files is not None else None
    path = Path(project.docs_root) / (
        relative if isinstance(relative, str) and relative else "ARIA_TEAM.yaml"
    )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"ARIA team configuration is unreadable: {path}") from error
    if not isinstance(raw, dict) or raw.get("schema_version") not in {1, 2}:
        raise ConfigurationError("ARIA_TEAM.yaml schema_version must be 1 or 2")
    schema = int(raw["schema_version"])
    allowed_root = {"schema_version", "actors"} | (
        {"project_id"} if schema == 2 else set()
    )
    if set(raw) != allowed_root:
        raise ConfigurationError("ARIA_TEAM.yaml contains unknown fields")
    if schema == 2 and raw.get("project_id") != getattr(project, "project_id", None):
        raise ConfigurationError("ARIA_TEAM.yaml project identity mismatch")
    rows = raw.get("actors")
    if not isinstance(rows, list) or not rows:
        raise ConfigurationError("ARIA_TEAM.yaml actors must be a non-empty list")
    actors: dict[str, dict[str, object]] = {}
    for index, row in enumerate(rows):
        allowed_actor_fields = {
            "id",
            "type",
            "roles",
            "display_name",
            "contact_email",
            "email_verified",
        }
        if not isinstance(row, dict) or set(row) - allowed_actor_fields:
            raise ConfigurationError(f"Team actor {index} must be a mapping")
        actor_id = row.get("id")
        actor_type = row.get("type")
        roles = row.get("roles")
        if (
            not isinstance(actor_id, str)
            or ACTOR_ID_RE.fullmatch(actor_id) is None
            or actor_type not in {"human", "service"}
            or not isinstance(roles, list)
            or not roles
            or not all(isinstance(role, str) and role in ACTOR_ROLES for role in roles)
            or len(roles) != len(set(roles))
            or (
                row.get("contact_email") is not None
                and (
                    not isinstance(row.get("contact_email"), str)
                    or "@" not in str(row.get("contact_email"))
                )
            )
            or (
                "email_verified" in row
                and not isinstance(row.get("email_verified"), bool)
            )
        ):
            raise ConfigurationError(f"Team actor {index} is malformed")
        if actor_id in actors:
            raise ConfigurationError(f"Duplicate team actor: {actor_id}")
        actors[actor_id] = {
            "id": actor_id,
            "type": actor_type,
            "roles": roles,
            "display_name": row.get("display_name", actor_id),
            **(
                {"contact_email": row.get("contact_email")}
                if row.get("contact_email") is not None
                else {}
            ),
            **(
                {"email_verified": row.get("email_verified", False)}
                if row.get("contact_email") is not None
                else {}
            ),
        }
    return actors


def _actor(
    actors: dict[str, dict[str, object]],
    actor_id: str,
    *,
    allowed_roles: set[str] | None = None,
) -> dict[str, object]:
    actor = actors.get(actor_id)
    if actor is None:
        raise WorkflowError(f"Unknown ARIA actor: {actor_id}")
    roles = {str(value) for value in actor["roles"]}
    if allowed_roles is not None and not roles.intersection(allowed_roles):
        raise WorkflowError(
            f"Actor {actor_id!r} lacks one of the required roles: {sorted(allowed_roles)}"
        )
    return actor


def validate_independent_review(
    project: object, *, contributor_id: str, reviewer_id: str
) -> dict[str, object]:
    actors = load_team(project)
    _actor(actors, contributor_id, allowed_roles=CLAIM_ROLES)
    if contributor_id == reviewer_id:
        raise WorkflowError("Contributor cannot approve their own independent review")
    reviewer = _actor(actors, reviewer_id, allowed_roles={"reviewer", "maintainer"})
    return {
        "ok": True,
        "contributor_id": contributor_id,
        "reviewer_id": reviewer_id,
        "reviewer_roles": reviewer["roles"],
        "independent": True,
    }


def validate_actor_role(
    project: object, *, actor_id: str, allowed_roles: set[str]
) -> dict[str, object]:
    return _actor(load_team(project), actor_id, allowed_roles=allowed_roles)


def _state_paths(project: object) -> tuple[Path, Path]:
    root = Path(project.runtime_root) / "team"
    return root / f"{project.project_id}.json", root / f"{project.project_id}.lock"


def _new_state(project: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": project.project_id,
        "revision": 0,
        "leases": {},
        "last_event": None,
    }


def _load_state(project: object, path: Path) -> dict[str, object]:
    if not path.is_file():
        return _new_state(project)
    import json

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Team state is unreadable: {path}") from error
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("project_id") != project.project_id
        or not isinstance(raw.get("revision"), int)
        or not isinstance(raw.get("leases"), dict)
    ):
        raise WorkflowError("Team state identity or schema is invalid")
    return raw


def _active_leases(state: dict[str, object], now: datetime) -> dict[str, dict[str, object]]:
    raw = state["leases"]
    assert isinstance(raw, dict)
    return {
        task_id: lease
        for task_id, lease in raw.items()
        if isinstance(task_id, str)
        and isinstance(lease, dict)
        and _parse_stamp(lease.get("expires_at"), f"Lease {task_id}") > now
    }


def team_status(project: object, *, now: datetime | None = None) -> dict[str, object]:
    state_path, lock_path = _state_paths(project)
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    with exclusive_lock(lock_path):
        state = _load_state(project, state_path)
        actors = load_team(project)
        leases = _active_leases(state, reference)
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": state["revision"],
            "actors": list(actors.values()),
            "active_leases": list(leases.values()),
        }


def claim_task(
    project: object,
    *,
    task_id: str,
    actor_id: str,
    expected_revision: int,
    ttl_seconds: int,
    now: datetime | None = None,
) -> dict[str, object]:
    if TASK_ID_RE.fullmatch(task_id) is None:
        raise ConfigurationError(f"Invalid task id: {task_id!r}")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
        or isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds < 1
        or ttl_seconds > MAX_LEASE_SECONDS
    ):
        raise ConfigurationError("Revision or lease TTL is outside the allowed range")
    actors = load_team(project)
    _actor(actors, actor_id, allowed_roles=CLAIM_ROLES)
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    state_path, lock_path = _state_paths(project)
    with exclusive_lock(lock_path):
        state = _load_state(project, state_path)
        revision = int(state["revision"])
        if revision != expected_revision:
            raise WorkflowError(
                f"Stale project revision: expected {expected_revision}, actual {revision}"
            )
        active = _active_leases(state, reference)
        existing = active.get(task_id)
        if existing is not None:
            raise WorkflowError(
                f"Task {task_id!r} is already leased by {existing.get('actor_id')!r}"
            )
        lease = {
            "task_id": task_id,
            "actor_id": actor_id,
            "token": uuid.uuid4().hex,
            "claimed_at": _stamp(reference),
            "expires_at": _stamp(reference + timedelta(seconds=ttl_seconds)),
        }
        active[task_id] = lease
        state["leases"] = active
        state["revision"] = revision + 1
        state["last_event"] = {
            "type": "task_claimed",
            "actor_id": actor_id,
            "task_id": task_id,
            "at": _stamp(reference),
        }
        atomic_write_json(state_path, state)
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": state["revision"],
            "lease": lease,
        }


def release_task(
    project: object,
    *,
    task_id: str,
    actor_id: str,
    token: str,
    expected_revision: int,
    now: datetime | None = None,
) -> dict[str, object]:
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    state_path, lock_path = _state_paths(project)
    with exclusive_lock(lock_path):
        state = _load_state(project, state_path)
        revision = int(state["revision"])
        if revision != expected_revision:
            raise WorkflowError(
                f"Stale project revision: expected {expected_revision}, actual {revision}"
            )
        active = _active_leases(state, reference)
        lease = active.get(task_id)
        if lease is None:
            raise WorkflowError(f"Task {task_id!r} has no active lease")
        if lease.get("actor_id") != actor_id or lease.get("token") != token:
            raise WorkflowError("Only the lease owner with the exact token can release it")
        del active[task_id]
        state["leases"] = active
        state["revision"] = revision + 1
        state["last_event"] = {
            "type": "task_released",
            "actor_id": actor_id,
            "task_id": task_id,
            "at": _stamp(reference),
        }
        atomic_write_json(state_path, state)
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": state["revision"],
            "released_task": task_id,
        }
