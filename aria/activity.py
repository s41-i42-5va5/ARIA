from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from aria.errors import ConfigurationError, WorkflowError
from aria.project import PROJECT_ID_RE
from aria.provider import PROVIDER_ID_RE, ProviderActor


ACTIVITY_SCHEMA_VERSION = 1
ACTIVITY_EVENT_SCHEMA_VERSION = 1
LOCAL_STAGES = {
    "analysis",
    "planning",
    "implementation",
    "testing",
    "blocked",
    "ready_for_pr",
}
GITHUB_STAGES = {"in_review", "waiting_for_ci"}
SOURCE_STAGES = {
    "local_aria": LOCAL_STAGES,
    "github": GITHUB_STAGES,
    "coordinator": {"completed"},
}
EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
TASK_ID_RE = re.compile(r"BLG-[A-Z0-9][A-Z0-9._-]{0,63}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
SECRET_NOTE_RE = re.compile(
    r"(?:github_pat_|gh[opusr]_|bearer\s+|-----BEGIN|\bsk-[A-Za-z0-9])",
    re.IGNORECASE,
)
MAX_RECENT_EVENTS = 256
MAX_CLOCK_SKEW = timedelta(minutes=5)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} must be a string-keyed mapping")
    return value


def _exact(mapping: dict[str, object], expected: set[str], label: str) -> None:
    if set(mapping) != expected:
        raise ConfigurationError(f"{label} schema keys are invalid")


def _stamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ConfigurationError(f"{label} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.astimezone(UTC) != parsed:
        raise ConfigurationError(f"{label} timestamp must be UTC")
    return parsed


def _safe_text(
    value: object,
    label: str,
    *,
    maximum: int,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\n" in value
        or "\r" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ConfigurationError(f"{label} must be safe single-line text")
    return value


def _branch(value: object, label: str) -> str:
    branch = _safe_text(value, label, maximum=128)
    if (
        not isinstance(branch, str)
        or BRANCH_RE.fullmatch(branch) is None
        or branch.startswith((".", "-", "/"))
        or branch.endswith((".", "/", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or any(
            not part or part.startswith(".") or part.endswith((".", ".lock"))
            for part in branch.split("/")
        )
    ):
        raise ConfigurationError(f"{label} is not a safe Git branch name")
    return branch


def _note(value: object, label: str) -> str | None:
    note = _safe_text(value, label, maximum=500, optional=True)
    if isinstance(note, str) and SECRET_NOTE_RE.search(note):
        raise ConfigurationError(f"{label} appears to contain a secret")
    return note


def _actor(value: object, label: str) -> tuple[str, ProviderActor]:
    raw = _mapping(value, label)
    _exact(raw, {"provider", "user_id", "username_snapshot"}, label)
    provider = raw.get("provider")
    if not isinstance(provider, str) or PROVIDER_ID_RE.fullmatch(provider) is None:
        raise ConfigurationError(f"{label}.provider is invalid")
    actor = ProviderActor(
        user_id=str(_safe_text(raw.get("user_id"), f"{label}.user_id", maximum=256)),
        username_snapshot=str(
            _safe_text(
                raw.get("username_snapshot"),
                f"{label}.username_snapshot",
                maximum=128,
            )
        ),
    )
    return provider, actor


def activity_template(project_id: str) -> dict[str, object]:
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    return {
        "schema_version": ACTIVITY_SCHEMA_VERSION,
        "project_id": project_id,
        "revision": 0,
        "updated_at": None,
        "active_work": [],
        "recent_event_ids": [],
    }


def parse_activity_event(value: object) -> dict[str, object]:
    raw = _mapping(value, "activity event")
    _exact(
        raw,
        {
            "schema_version",
            "event_id",
            "project_id",
            "task_id",
            "actor",
            "source",
            "stage",
            "branch",
            "pr_number",
            "note",
            "observed_at",
        },
        "activity event",
    )
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ConfigurationError("activity event schema_version must be 1")
    event_id = raw.get("event_id")
    project_id = raw.get("project_id")
    task_id = raw.get("task_id")
    source = raw.get("source")
    stage = raw.get("stage")
    if not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id) is None:
        raise ConfigurationError("activity event_id is invalid")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError("activity project_id is invalid")
    if not isinstance(task_id, str) or TASK_ID_RE.fullmatch(task_id) is None:
        raise ConfigurationError("activity task_id is invalid")
    if source not in SOURCE_STAGES or stage not in SOURCE_STAGES[str(source)]:
        raise ConfigurationError("activity source is not allowed to publish this stage")
    provider, actor = _actor(raw.get("actor"), "activity actor")
    branch = _branch(raw.get("branch"), "activity branch")
    pr_number = raw.get("pr_number")
    if pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
        raise ConfigurationError("activity pr_number is invalid")
    if source == "local_aria" and pr_number is not None:
        raise ConfigurationError("local activity cannot assert a PR number")
    if source in {"github", "coordinator"} and pr_number is None:
        raise ConfigurationError("GitHub/coordinator activity requires a PR number")
    note = _note(raw.get("note"), "activity note")
    _stamp(raw.get("observed_at"), "activity observed_at")
    return {
        **raw,
        "actor": {
            "provider": provider,
            "user_id": actor.user_id,
            "username_snapshot": actor.username_snapshot,
        },
        "branch": branch,
        "note": note,
    }


def validate_activity_snapshot(value: object) -> dict[str, object]:
    raw = _mapping(value, "ACTIVITY.yaml")
    _exact(
        raw,
        {
            "schema_version",
            "project_id",
            "revision",
            "updated_at",
            "active_work",
            "recent_event_ids",
        },
        "ACTIVITY.yaml",
    )
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ConfigurationError("ACTIVITY.yaml schema_version must be 1")
    project_id = raw.get("project_id")
    revision = raw.get("revision")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError("ACTIVITY.yaml project_id is invalid")
    if type(revision) is not int or revision < 0:
        raise ConfigurationError("ACTIVITY.yaml revision is invalid")
    updated_at = raw.get("updated_at")
    if revision == 0:
        if updated_at is not None or raw.get("active_work") != [] or raw.get("recent_event_ids") != []:
            raise ConfigurationError("ACTIVITY.yaml revision zero must be empty")
    else:
        _stamp(updated_at, "ACTIVITY.yaml.updated_at")
    active = raw.get("active_work")
    recent = raw.get("recent_event_ids")
    if not isinstance(active, list) or not isinstance(recent, list):
        raise ConfigurationError("ACTIVITY.yaml collections are invalid")
    if len(recent) > MAX_RECENT_EVENTS or len(recent) != len(set(recent)) or any(
        not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id) is None
        for event_id in recent
    ):
        raise ConfigurationError("ACTIVITY.yaml recent event ids are invalid")
    task_ids: set[str] = set()
    for index, entry_value in enumerate(active):
        entry = _mapping(entry_value, f"activity entry {index}")
        _exact(
            entry,
            {
                "task_id",
                "actor",
                "source",
                "stage",
                "branch",
                "pr_number",
                "note",
                "observed_at",
                "received_at",
                "stale",
                "last_event_id",
            },
            f"activity entry {index}",
        )
        task_id = entry.get("task_id")
        if not isinstance(task_id, str) or TASK_ID_RE.fullmatch(task_id) is None or task_id in task_ids:
            raise ConfigurationError("ACTIVITY.yaml contains invalid or duplicate task")
        task_ids.add(task_id)
        provider, actor = _actor(entry.get("actor"), f"activity entry {index}.actor")
        source = entry.get("source")
        stage = entry.get("stage")
        if source not in SOURCE_STAGES or stage not in SOURCE_STAGES[str(source)]:
            raise ConfigurationError("ACTIVITY.yaml entry source/stage is invalid")
        _branch(entry.get("branch"), "activity entry branch")
        pr_number = entry.get("pr_number")
        if pr_number is not None and (type(pr_number) is not int or pr_number <= 0):
            raise ConfigurationError("ACTIVITY.yaml entry PR is invalid")
        if source == "local_aria" and pr_number is not None:
            raise ConfigurationError("ACTIVITY.yaml local entry cannot assert a PR")
        if source == "github" and pr_number is None:
            raise ConfigurationError("ACTIVITY.yaml GitHub entry requires a PR")
        _note(entry.get("note"), "activity entry note")
        _stamp(entry.get("observed_at"), "activity entry observed_at")
        _stamp(entry.get("received_at"), "activity entry received_at")
        if type(entry.get("stale")) is not bool:
            raise ConfigurationError("ACTIVITY.yaml entry stale flag is invalid")
        event_id = entry.get("last_event_id")
        if not isinstance(event_id, str) or EVENT_ID_RE.fullmatch(event_id) is None:
            raise ConfigurationError("ACTIVITY.yaml entry event id is invalid")
        entry["actor"] = {
            "provider": provider,
            "user_id": actor.user_id,
            "username_snapshot": actor.username_snapshot,
        }
    if revision > 0 and active:
        latest_received = max(
            _stamp(entry["received_at"], "activity entry received_at")
            for entry in active
        )
        if _stamp(updated_at, "ACTIVITY.yaml.updated_at") < latest_received:
            raise ConfigurationError("ACTIVITY.yaml updated_at precedes active work")
    if [entry["task_id"] for entry in active] != sorted(task_ids):
        raise ConfigurationError("ACTIVITY.yaml active work must be sorted by task id")
    return raw


def dump_activity(snapshot: dict[str, object]) -> str:
    validated = validate_activity_snapshot(snapshot)
    content = yaml.safe_dump(validated, allow_unicode=True, sort_keys=False)
    if validate_activity_snapshot(yaml.safe_load(content)) != validated:
        raise ConfigurationError("ACTIVITY.yaml deterministic round-trip failed")
    return content


def load_activity(path: Path) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot read ACTIVITY.yaml: {path}") from error
    return validate_activity_snapshot(value)


def _transition_allowed(current: str | None, source: str, stage: str) -> bool:
    if source == "local_aria":
        return current is None or current in LOCAL_STAGES
    if source == "github":
        if stage == "in_review":
            return current is None or current in LOCAL_STAGES | {"in_review"}
        return current is None or current in LOCAL_STAGES | {
            "in_review",
            "waiting_for_ci",
        }
    return stage == "completed" and current == "waiting_for_ci"


def apply_activity_event(
    snapshot_value: object,
    event_value: object,
    *,
    authorized_source: str,
    authorized_provider: str,
    authorized_actor: ProviderActor,
    received_at: str,
) -> dict[str, object]:
    snapshot = validate_activity_snapshot(snapshot_value)
    event = parse_activity_event(event_value)
    received = _stamp(received_at, "activity received_at")
    observed = _stamp(event["observed_at"], "activity observed_at")
    if int(snapshot["revision"]) > 0 and received < _stamp(
        snapshot["updated_at"], "ACTIVITY.yaml.updated_at"
    ):
        raise WorkflowError("Activity coordinator time cannot move backwards")
    if observed > received + MAX_CLOCK_SKEW:
        raise WorkflowError("Activity event observed_at is too far in the future")
    if event["project_id"] != snapshot["project_id"]:
        raise WorkflowError("Activity event belongs to another project")
    if authorized_source not in SOURCE_STAGES or event["source"] != authorized_source:
        raise WorkflowError("Activity event source does not match authorized source")
    event_actor = _mapping(event["actor"], "activity actor")
    if (
        event_actor.get("provider") != authorized_provider
        or event_actor.get("user_id") != authorized_actor.user_id
    ):
        raise WorkflowError("Activity event actor does not match authorized identity")
    event_id = str(event["event_id"])
    recent = list(snapshot["recent_event_ids"])
    if event_id in recent:
        return {"applied": False, "reason": "duplicate_event", "snapshot": snapshot}
    active = [dict(entry) for entry in snapshot["active_work"]]
    existing = next((entry for entry in active if entry["task_id"] == event["task_id"]), None)
    if existing is not None:
        existing_time = _stamp(existing["observed_at"], "activity existing observed_at")
        if observed < existing_time:
            return {"applied": False, "reason": "late_event", "snapshot": snapshot}
        if observed == existing_time:
            raise WorkflowError("Activity events at the same observed_at conflict")
        existing_actor = _mapping(existing["actor"], "activity existing actor")
        if existing_actor.get("user_id") != authorized_actor.user_id:
            raise WorkflowError("Activity task belongs to another actor")
    current_stage = str(existing["stage"]) if existing is not None else None
    source = str(event["source"])
    stage = str(event["stage"])
    if not _transition_allowed(current_stage, source, stage):
        raise WorkflowError(f"Activity transition is not allowed: {current_stage} -> {stage}")
    if stage == "completed":
        active = [entry for entry in active if entry["task_id"] != event["task_id"]]
    else:
        entry = {
            "task_id": event["task_id"],
            "actor": {
                "provider": authorized_provider,
                "user_id": authorized_actor.user_id,
                "username_snapshot": authorized_actor.username_snapshot,
            },
            "source": source,
            "stage": stage,
            "branch": event["branch"],
            "pr_number": event["pr_number"],
            "note": event["note"],
            "observed_at": event["observed_at"],
            "received_at": received_at,
            "stale": False,
            "last_event_id": event_id,
        }
        active = [row for row in active if row["task_id"] != event["task_id"]]
        active.append(entry)
        active.sort(key=lambda row: str(row["task_id"]))
    recent.append(event_id)
    recent = recent[-MAX_RECENT_EVENTS:]
    updated = {
        "schema_version": ACTIVITY_SCHEMA_VERSION,
        "project_id": snapshot["project_id"],
        "revision": int(snapshot["revision"]) + 1,
        "updated_at": received_at,
        "active_work": active,
        "recent_event_ids": recent,
    }
    validate_activity_snapshot(updated)
    return {"applied": True, "reason": None, "snapshot": updated}


def mark_stale_activity(
    snapshot_value: object,
    *,
    now: str,
    stale_after_seconds: int,
) -> dict[str, object]:
    snapshot = validate_activity_snapshot(snapshot_value)
    current_time = _stamp(now, "activity stale check time")
    if int(snapshot["revision"]) > 0 and current_time < _stamp(
        snapshot["updated_at"], "ACTIVITY.yaml.updated_at"
    ):
        raise WorkflowError("Activity stale check time cannot move backwards")
    if type(stale_after_seconds) is not int or stale_after_seconds <= 0:
        raise ConfigurationError("stale_after_seconds must be a positive integer")
    active: list[dict[str, object]] = []
    changed = False
    for value in snapshot["active_work"]:
        entry = dict(value)
        received = _stamp(entry["received_at"], "activity entry received_at")
        stale = current_time - received > timedelta(seconds=stale_after_seconds)
        if entry["stale"] != stale:
            entry["stale"] = stale
            changed = True
        active.append(entry)
    if not changed:
        return {"applied": False, "reason": "unchanged", "snapshot": snapshot}
    updated = {
        **snapshot,
        "revision": int(snapshot["revision"]) + 1,
        "updated_at": now,
        "active_work": active,
    }
    validate_activity_snapshot(updated)
    return {"applied": True, "reason": None, "snapshot": updated}
