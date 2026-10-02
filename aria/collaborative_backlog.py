from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from aria.activity import EVENT_ID_RE, TASK_ID_RE
from aria.errors import ConfigurationError, WorkflowError
from aria.file_scope import normalize_scope, path_allowed, scope_sets_overlap
from aria.project import PROJECT_ID_RE
from aria.provider import PROVIDER_ID_RE, ProviderActor


COLLABORATIVE_BACKLOG_SCHEMA_VERSION = 2
BACKLOG_REQUEST_SCHEMA_VERSION = 1
PRIORITIES = {"P0", "P1", "P2", "P3"}
STATUSES = {
    "open", "assigned", "in_progress", "blocked", "in_review", "done", "cancelled"
}
ACTIONS = {
    "add", "triage", "assign", "claim", "block", "review", "complete",
    "cancel", "amend_scope", "recover",
}
ACTION_PERMISSIONS = {action: f"backlog.{action}" for action in ACTIONS}
ACTION_PERMISSIONS["review"] = "backlog.complete"
ACTION_PERMISSIONS["recover"] = "team.sync"
SOURCE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
EVIDENCE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}")
GIT_OID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
LEGACY_ITEM_KEYS = {
    "id", "title", "description", "priority", "status", "source_id",
    "dependencies", "evidence_required", "evidence_refs", "creator", "assignee",
    "blocked_reason", "created_at", "updated_at",
}
EXTENDED_ITEM_KEYS = LEGACY_ITEM_KEYS | {
    "kind", "triage_owner", "requirements", "acceptance_criteria", "scope_paths",
    "lease", "pull_request", "accepted_commit",
}


@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    actor: ProviderActor

    def __post_init__(self) -> None:
        if PROVIDER_ID_RE.fullmatch(self.provider) is None:
            raise ConfigurationError("backlog identity provider is invalid")

    @property
    def key(self) -> tuple[str, str]:
        return self.provider, self.actor.user_id

    def as_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "user_id": self.actor.user_id,
            "username_snapshot": self.actor.username_snapshot,
            "display_name_snapshot": self.actor.display_name_snapshot,
        }


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} must be a string-keyed mapping")
    return value


def _exact(value: dict[str, object], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise ConfigurationError(f"{label} schema keys are invalid")


def _text(
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
        or "\r" in value
        or "\n" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ConfigurationError(f"{label} must be safe single-line text")
    return value


def _stamp(value: object, label: str) -> str:
    from datetime import UTC, datetime

    if not isinstance(value, str) or not value.endswith("Z"):
        raise ConfigurationError(f"{label} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.astimezone(UTC) != parsed:
        raise ConfigurationError(f"{label} timestamp must be UTC")
    return value


def _text_list(value: object, label: str, *, required: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (required and not value)
        or value != sorted(set(value))
        or any(_text(row, label, maximum=500) is None for row in value)
    ):
        raise ConfigurationError(f"{label} must be a unique sorted text list")
    return value


def _scope(value: object, label: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ConfigurationError(f"{label} is invalid")
    if not value:
        return []
    try:
        return normalize_scope(value)
    except ConfigurationError as error:
        raise ConfigurationError(f"{label} is invalid") from error


def _identity(value: object, label: str) -> ProviderIdentity:
    raw = _mapping(value, label)
    _exact(
        raw,
        {"provider", "user_id", "username_snapshot", "display_name_snapshot"},
        label,
    )
    provider = raw.get("provider")
    if not isinstance(provider, str):
        raise ConfigurationError(f"{label}.provider is invalid")
    return ProviderIdentity(
        provider=provider,
        actor=ProviderActor(
            user_id=str(_text(raw.get("user_id"), f"{label}.user_id", maximum=256)),
            username_snapshot=str(
                _text(
                    raw.get("username_snapshot"),
                    f"{label}.username_snapshot",
                    maximum=128,
                )
            ),
            display_name_snapshot=_text(
                raw.get("display_name_snapshot"),
                f"{label}.display_name_snapshot",
                maximum=256,
                optional=True,
            ),
        ),
    )


def _sha256_json(value: object) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def collaborative_backlog_template(project_id: str) -> dict[str, object]:
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    return {
        "schema_version": COLLABORATIVE_BACKLOG_SCHEMA_VERSION,
        "project_id": project_id,
        "revision": 0,
        "next_item_number": 1,
        "updated_at": None,
        "items": [],
        "events": [],
    }


def next_available_item_number(items: list[dict[str, object]]) -> int:
    item_ids = {str(item.get("id")) for item in items}
    number = 1
    while f"BLG-{number:06d}" in item_ids:
        number += 1
    return number


def parse_backlog_request(value: object) -> dict[str, object]:
    raw = _mapping(value, "backlog request")
    _exact(
        raw,
        {
            "schema_version",
            "request_id",
            "correlation_id",
            "project_id",
            "action",
            "item_id",
            "payload",
            "requested_at",
        },
        "backlog request",
    )
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ConfigurationError("backlog request schema_version must be 1")
    for field in ("request_id", "correlation_id"):
        if not isinstance(raw.get(field), str) or EVENT_ID_RE.fullmatch(raw[field]) is None:
            raise ConfigurationError(f"backlog request {field} is invalid")
    if not isinstance(raw.get("project_id"), str) or PROJECT_ID_RE.fullmatch(
        raw["project_id"]
    ) is None:
        raise ConfigurationError("backlog request project_id is invalid")
    action = raw.get("action")
    if action not in ACTIONS:
        raise ConfigurationError("backlog request action is invalid")
    item_id = raw.get("item_id")
    if action == "add":
        if item_id is not None:
            raise ConfigurationError("backlog add request item_id must be null")
    elif not isinstance(item_id, str) or TASK_ID_RE.fullmatch(item_id) is None:
        raise ConfigurationError("backlog request item_id is invalid")
    payload = _mapping(raw.get("payload"), "backlog request payload")
    expected_payloads = {
        "add": {
            "title",
            "description",
            "priority",
            "source_id",
            "dependencies",
            "evidence_required",
        },
        "triage": {
            "assignee_provider",
            "assignee_user_id",
            "priority",
            "requirements",
            "acceptance_criteria",
            "dependencies",
            "scope_paths",
            "evidence_required",
        },
        "assign": {"assignee_provider", "assignee_user_id"},
        "claim": {"branch"},
        "block": {"reason"},
        "cancel": {"reason"},
        "amend_scope": {"scope_paths"},
        "recover": {
            "requirements", "acceptance_criteria", "scope_paths", "branch", "target_status"
        },
        "review": {"pull_request", "head_commit", "source_branch", "changed_paths"},
        "complete": {
            "evidence_refs",
            "pull_request",
            "merge_commit",
            "source_branch",
            "changed_paths",
            "source_commit",
        },
    }
    _exact(payload, expected_payloads[str(action)], f"backlog {action} payload")
    if action == "add":
        _text(payload.get("title"), "backlog title", maximum=200)
        _text(payload.get("description"), "backlog description", maximum=1000)
        if payload.get("priority") not in PRIORITIES:
            raise ConfigurationError("backlog priority is invalid")
        source_id = payload.get("source_id")
        if source_id is not None and (
            not isinstance(source_id, str) or SOURCE_ID_RE.fullmatch(source_id) is None
        ):
            raise ConfigurationError("backlog source_id is invalid")
        dependencies = payload.get("dependencies")
        if (
            not isinstance(dependencies, list)
            or dependencies != sorted(set(dependencies))
            or any(
                not isinstance(dependency, str)
                or TASK_ID_RE.fullmatch(dependency) is None
                for dependency in dependencies
            )
        ):
            raise ConfigurationError("backlog dependencies are invalid")
        if type(payload.get("evidence_required")) is not bool:
            raise ConfigurationError("backlog evidence_required must be boolean")
    elif action in {"triage", "assign"}:
        provider = payload.get("assignee_provider")
        if not isinstance(provider, str) or PROVIDER_ID_RE.fullmatch(provider) is None:
            raise ConfigurationError("backlog assignee provider is invalid")
        _text(payload.get("assignee_user_id"), "backlog assignee user_id", maximum=256)
        if action == "triage":
            if payload.get("priority") not in PRIORITIES:
                raise ConfigurationError("backlog priority is invalid")
            _text_list(payload.get("requirements"), "backlog requirements", required=True)
            _text_list(
                payload.get("acceptance_criteria"),
                "backlog acceptance criteria",
                required=True,
            )
            dependencies = payload.get("dependencies")
            if (
                not isinstance(dependencies, list)
                or dependencies != sorted(set(dependencies))
                or any(
                    not isinstance(dependency, str)
                    or TASK_ID_RE.fullmatch(dependency) is None
                    for dependency in dependencies
                )
            ):
                raise ConfigurationError("backlog dependencies are invalid")
            _scope(payload.get("scope_paths"), "backlog file scope", required=True)
            if type(payload.get("evidence_required")) is not bool:
                raise ConfigurationError("backlog evidence_required must be boolean")
    elif action == "claim":
        _text(payload.get("branch"), "backlog claim branch", maximum=256)
    elif action in {"block", "cancel"}:
        _text(payload.get("reason"), "backlog blocked reason", maximum=500)
    elif action == "amend_scope":
        _scope(payload.get("scope_paths"), "backlog amended file scope", required=True)
    elif action == "recover":
        _text_list(payload.get("requirements"), "backlog recovery requirements", required=True)
        _text_list(
            payload.get("acceptance_criteria"),
            "backlog recovery acceptance criteria",
            required=True,
        )
        _scope(payload.get("scope_paths"), "backlog recovery file scope", required=True)
        if payload.get("target_status") not in {"assigned", "in_progress", "blocked"}:
            raise ConfigurationError("backlog recovery target status is invalid")
        _text(payload.get("branch"), "backlog recovery branch", maximum=256)
    elif action in {"review", "complete"}:
        if type(payload.get("pull_request")) is not int or payload["pull_request"] <= 0:
            raise ConfigurationError("backlog pull request is invalid")
        commit_key = "head_commit" if action == "review" else "merge_commit"
        if not isinstance(payload.get(commit_key), str) or GIT_OID_RE.fullmatch(
            payload[commit_key]
        ) is None:
            raise ConfigurationError("backlog commit is invalid")
        _text(payload.get("source_branch"), "backlog source branch", maximum=256)
        _scope(payload.get("changed_paths"), "backlog changed paths", required=True)
    if action == "complete":
        refs = payload.get("evidence_refs")
        if (
            not isinstance(refs, list)
            or refs != sorted(set(refs))
            or any(
                not isinstance(ref, str) or EVIDENCE_REF_RE.fullmatch(ref) is None
                for ref in refs
            )
        ):
            raise ConfigurationError("backlog evidence refs are invalid")
        if not isinstance(payload.get("source_commit"), str) or GIT_OID_RE.fullmatch(
            payload["source_commit"]
        ) is None:
            raise ConfigurationError("backlog source commit is invalid")
    _stamp(raw.get("requested_at"), "backlog requested_at")
    return raw


def _validate_item(value: object, index: int) -> dict[str, object]:
    item = _mapping(value, f"backlog item {index}")
    if frozenset(item) not in {frozenset(LEGACY_ITEM_KEYS), frozenset(EXTENDED_ITEM_KEYS)}:
        raise ConfigurationError(f"backlog item {index} schema keys are invalid")
    extended = set(item) == EXTENDED_ITEM_KEYS
    if not isinstance(item.get("id"), str) or TASK_ID_RE.fullmatch(item["id"]) is None:
        raise ConfigurationError("backlog item id is invalid")
    _text(item.get("title"), "backlog item title", maximum=200)
    _text(item.get("description"), "backlog item description", maximum=1000)
    if item.get("priority") not in PRIORITIES or item.get("status") not in STATUSES:
        raise ConfigurationError("backlog item priority/status is invalid")
    source_id = item.get("source_id")
    if source_id is not None and (
        not isinstance(source_id, str) or SOURCE_ID_RE.fullmatch(source_id) is None
    ):
        raise ConfigurationError("backlog item source_id is invalid")
    dependencies = item.get("dependencies")
    refs = item.get("evidence_refs")
    if (
        not isinstance(dependencies, list)
        or dependencies != sorted(set(dependencies))
        or any(
            not isinstance(dependency, str)
            or TASK_ID_RE.fullmatch(dependency) is None
            for dependency in dependencies
        )
    ):
        raise ConfigurationError("backlog item dependencies are invalid")
    if (
        not isinstance(refs, list)
        or refs != sorted(set(refs))
        or any(
            not isinstance(ref, str) or EVIDENCE_REF_RE.fullmatch(ref) is None
            for ref in refs
        )
    ):
        raise ConfigurationError("backlog item evidence refs are invalid")
    if type(item.get("evidence_required")) is not bool:
        raise ConfigurationError("backlog item evidence_required is invalid")
    _identity(item.get("creator"), "backlog item creator")
    if item.get("assignee") is not None:
        _identity(item.get("assignee"), "backlog item assignee")
    if item["status"] in {"assigned", "in_progress", "blocked", "in_review", "done"} and item[
        "assignee"
    ] is None:
        raise ConfigurationError("backlog item status requires an assignee")
    if item["status"] == "open" and item["assignee"] is not None:
        raise ConfigurationError("open backlog item cannot have an assignee")
    if item["status"] == "blocked":
        _text(item.get("blocked_reason"), "backlog blocked reason", maximum=500)
    elif item.get("blocked_reason") is not None:
        raise ConfigurationError("backlog blocked reason exists outside blocked status")
    if item["status"] == "done" and item["evidence_required"] and not refs:
        raise ConfigurationError("completed backlog item requires evidence")
    if extended:
        kind = item.get("kind")
        if kind not in {"idea", "task"}:
            raise ConfigurationError("backlog item kind is invalid")
        _text_list(item.get("requirements"), "backlog item requirements", required=kind == "task")
        _text_list(
            item.get("acceptance_criteria"),
            "backlog item acceptance criteria",
            required=kind == "task",
        )
        scope_paths = _scope(
            item.get("scope_paths"), "backlog item file scope", required=kind == "task"
        )
        if kind == "idea":
            if any(
                item.get(field) is not None
                for field in ("triage_owner", "assignee", "lease", "pull_request", "accepted_commit")
            ) or item["status"] != "open":
                raise ConfigurationError("untriaged idea contains task state")
        else:
            _identity(item.get("triage_owner"), "backlog item triage owner")
        lease = item.get("lease")
        if lease is not None:
            lease = _mapping(lease, "backlog item lease")
            _exact(lease, {"holder", "branch", "scope_paths", "acquired_at"}, "backlog item lease")
            _identity(lease.get("holder"), "backlog item lease holder")
            _text(lease.get("branch"), "backlog item lease branch", maximum=256)
            if _scope(lease.get("scope_paths"), "backlog item lease scope", required=True) != scope_paths:
                raise ConfigurationError("backlog item lease scope mismatch")
            _stamp(lease.get("acquired_at"), "backlog item lease acquired_at")
        if item["status"] in {"in_progress", "blocked", "in_review"} and lease is None:
            raise ConfigurationError("active backlog task requires a file-scope lease")
        if item["status"] in {"open", "assigned", "done", "cancelled"} and lease is not None:
            raise ConfigurationError("inactive backlog task cannot retain a file-scope lease")
        pull_request = item.get("pull_request")
        accepted_commit = item.get("accepted_commit")
        if item["status"] in {"in_review", "done"}:
            if type(pull_request) is not int or pull_request <= 0:
                raise ConfigurationError("completed backlog task pull request is invalid")
            if not isinstance(accepted_commit, str) or GIT_OID_RE.fullmatch(accepted_commit) is None:
                raise ConfigurationError("completed backlog task commit is invalid")
        elif pull_request is not None or accepted_commit is not None:
            raise ConfigurationError("unaccepted backlog task contains acceptance state")
    _stamp(item.get("created_at"), "backlog item created_at")
    _stamp(item.get("updated_at"), "backlog item updated_at")
    return item


def validate_collaborative_backlog(value: object) -> dict[str, object]:
    raw = _mapping(value, "BACKLOG.yaml v2")
    _exact(
        raw,
        {
            "schema_version",
            "project_id",
            "revision",
            "next_item_number",
            "updated_at",
            "items",
            "events",
        },
        "BACKLOG.yaml v2",
    )
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 2:
        raise ConfigurationError("collaborative backlog schema_version must be 2")
    project_id = raw.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError("collaborative backlog project_id is invalid")
    revision = raw.get("revision")
    next_number = raw.get("next_item_number")
    if type(revision) is not int or revision < 0:
        raise ConfigurationError("collaborative backlog revision is invalid")
    if type(next_number) is not int or next_number < 1:
        raise ConfigurationError("collaborative backlog next item number is invalid")
    items_value = raw.get("items")
    events = raw.get("events")
    if not isinstance(items_value, list) or not isinstance(events, list):
        raise ConfigurationError("collaborative backlog collections are invalid")
    items = [_validate_item(value, index) for index, value in enumerate(items_value)]
    item_ids = [str(item["id"]) for item in items]
    if item_ids != sorted(set(item_ids)):
        raise ConfigurationError("collaborative backlog items must be unique and sorted")
    source_ids = [item["source_id"] for item in items if item["source_id"] is not None]
    if len(source_ids) != len(set(source_ids)):
        raise ConfigurationError("collaborative backlog source ids must be unique")
    if any(
        dependency not in set(item_ids)
        for item in items
        for dependency in item["dependencies"]
    ):
        raise ConfigurationError("collaborative backlog dependency does not exist")
    dependencies = {str(item["id"]): set(item["dependencies"]) for item in items}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise ConfigurationError("collaborative backlog dependency cycle exists")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in dependencies[item_id]:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in dependencies:
        visit(item_id)
    if revision == 0:
        if raw.get("updated_at") is not None or items or events or next_number != 1:
            raise ConfigurationError("collaborative backlog revision zero must be empty")
    else:
        _stamp(raw.get("updated_at"), "collaborative backlog updated_at")
    if len(events) != revision:
        raise ConfigurationError("collaborative backlog revision/event count mismatch")
    previous: str | None = None
    request_ids: set[str] = set()
    for index, event_value in enumerate(events, start=1):
        event = _mapping(event_value, f"backlog event {index}")
        _exact(
            event,
            {
                "schema_version",
                "sequence",
                "project_id",
                "request_id",
                "correlation_id",
                "request_fingerprint",
                "action",
                "item_ids",
                "requested_at",
                "committed_at",
                "requested_by",
                "committed_by",
                "items_sha256",
                "previous_event_sha256",
                "event_sha256",
            },
            f"backlog event {index}",
        )
        if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
            raise ConfigurationError("backlog event schema_version is invalid")
        if event.get("sequence") != index or event.get("project_id") != project_id:
            raise ConfigurationError("backlog event sequence/project is invalid")
        request_id = event.get("request_id")
        if (
            not isinstance(request_id, str)
            or EVENT_ID_RE.fullmatch(request_id) is None
            or request_id in request_ids
        ):
            raise ConfigurationError("backlog event request id is invalid or duplicate")
        request_ids.add(request_id)
        correlation_id = event.get("correlation_id")
        if (
            not isinstance(correlation_id, str)
            or EVENT_ID_RE.fullmatch(correlation_id) is None
        ):
            raise ConfigurationError("backlog event correlation id is invalid")
        if not _is_sha256(event.get("request_fingerprint")):
            raise ConfigurationError("backlog event request fingerprint is invalid")
        if event.get("action") not in ACTIONS:
            raise ConfigurationError("backlog event action is invalid")
        event_item_ids = event.get("item_ids")
        if (
            not isinstance(event_item_ids, list)
            or len(event_item_ids) != 1
            or any(item_id not in set(item_ids) for item_id in event_item_ids)
        ):
            raise ConfigurationError("backlog event item ids are invalid")
        if not _is_sha256(event.get("items_sha256")):
            raise ConfigurationError("backlog event items hash is invalid")
        _identity(event.get("requested_by"), "backlog event requested_by")
        _identity(event.get("committed_by"), "backlog event committed_by")
        _stamp(event.get("requested_at"), "backlog event requested_at")
        _stamp(event.get("committed_at"), "backlog event committed_at")
        if event.get("previous_event_sha256") != previous:
            raise WorkflowError("collaborative backlog event chain is broken")
        event_hash = event.get("event_sha256")
        if not _is_sha256(event_hash):
            raise ConfigurationError("backlog event hash format is invalid")
        calculated = _sha256_json({key: val for key, val in event.items() if key != "event_sha256"})
        if event_hash != calculated:
            raise WorkflowError("collaborative backlog event hash is invalid")
        previous = str(event_hash)
    if events:
        if raw["updated_at"] != events[-1]["committed_at"]:
            raise ConfigurationError("collaborative backlog updated_at is not the audit head")
        expected_next = next_available_item_number(items)
        if next_number != expected_next:
            raise ConfigurationError("collaborative backlog next item number is inconsistent")
    if events and events[-1]["items_sha256"] != _sha256_json(items):
        raise WorkflowError("collaborative backlog state does not match audit head")
    return raw


def dump_collaborative_backlog(value: object) -> str:
    validated = validate_collaborative_backlog(value)
    content = yaml.safe_dump(validated, allow_unicode=True, sort_keys=False)
    if validate_collaborative_backlog(yaml.safe_load(content)) != validated:
        raise ConfigurationError("collaborative backlog deterministic round-trip failed")
    return content


def load_collaborative_backlog(path: Path) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot read collaborative backlog: {path}") from error
    return validate_collaborative_backlog(value)


def _same_identity(mapping: object, identity: ProviderIdentity) -> bool:
    current = _identity(mapping, "backlog identity")
    return current.key == identity.key


def backlog_request_fingerprint(
    request_value: object, authenticated_actor: ProviderIdentity
) -> str:
    """Return the canonical content-and-actor fingerprint stored in backlog events."""
    request = parse_backlog_request(request_value)
    fingerprint_request = {
        key: value for key, value in request.items() if key != "requested_at"
    }
    return _sha256_json(
        {
            "request": fingerprint_request,
            "authenticated_provider": authenticated_actor.provider,
            "authenticated_user_id": authenticated_actor.actor.user_id,
        }
    )


def apply_backlog_request(
    backlog_value: object,
    request_value: object,
    *,
    authenticated_actor: ProviderIdentity,
    active_members: tuple[ProviderIdentity, ...],
    permissions: frozenset[str],
    coordinator: ProviderIdentity,
    expected_revision: int,
    committed_at: str,
    acceptance_verified: bool = False,
) -> dict[str, object]:
    backlog = validate_collaborative_backlog(backlog_value)
    request = parse_backlog_request(request_value)
    _stamp(committed_at, "backlog committed_at")
    if backlog["project_id"] != request["project_id"]:
        raise WorkflowError("backlog request belongs to another project")
    fingerprint = backlog_request_fingerprint(request, authenticated_actor)
    existing_event = next(
        (event for event in backlog["events"] if event["request_id"] == request["request_id"]),
        None,
    )
    if existing_event is not None:
        if existing_event["request_fingerprint"] != fingerprint:
            raise WorkflowError("backlog request id was reused with different content")
        return {
            "applied": False,
            "reason": "duplicate_request",
            "backlog": backlog,
            "item": next(
                (item for item in backlog["items"] if item["id"] in existing_event["item_ids"]),
                None,
            ),
        }
    if backlog["revision"] != expected_revision:
        raise WorkflowError(
            f"Stale collaborative backlog revision: expected {expected_revision}, "
            f"actual {backlog['revision']}"
        )
    member_map = {member.key: member for member in active_members}
    if len(member_map) != len(active_members):
        raise ConfigurationError("active project members contain duplicate identities")
    if authenticated_actor.key not in member_map:
        raise WorkflowError("authenticated actor is not an active project member")
    authenticated_actor = member_map[authenticated_actor.key]
    if coordinator.key == authenticated_actor.key:
        raise WorkflowError("coordinator identity cannot replace the requesting actor")
    action = str(request["action"])
    if ACTION_PERMISSIONS[action] not in permissions:
        raise WorkflowError(f"backlog action is not permitted: {action}")
    items = [dict(item) for item in backlog["items"]]
    payload = _mapping(request["payload"], "backlog request payload")
    item: dict[str, object]
    if action == "add":
        item_id = f"BLG-{int(backlog['next_item_number']):06d}"
        if any(existing["id"] == item_id for existing in items):
            raise WorkflowError("collaborative backlog next item id already exists")
        if payload["source_id"] is not None and any(
            existing["source_id"] == payload["source_id"] for existing in items
        ):
            raise WorkflowError("backlog source_id already exists")
        missing = [dependency for dependency in payload["dependencies"] if dependency not in {row["id"] for row in items}]
        if missing:
            raise WorkflowError(f"backlog dependencies do not exist: {missing}")
        item = {
            "id": item_id,
            "title": payload["title"],
            "description": payload["description"],
            "priority": payload["priority"],
            "status": "open",
            "source_id": payload["source_id"],
            "dependencies": payload["dependencies"],
            "evidence_required": payload["evidence_required"],
            "evidence_refs": [],
            "creator": authenticated_actor.as_mapping(),
            "assignee": None,
            "blocked_reason": None,
            "created_at": committed_at,
            "updated_at": committed_at,
            "kind": "idea",
            "triage_owner": None,
            "requirements": [],
            "acceptance_criteria": [],
            "scope_paths": [],
            "lease": None,
            "pull_request": None,
            "accepted_commit": None,
        }
        items.append(item)
        next_number = next_available_item_number(items)
    else:
        item = next((row for row in items if row["id"] == request["item_id"]), None)  # type: ignore[assignment]
        if item is None:
            raise WorkflowError(f"Unknown backlog item: {request['item_id']}")
        if item["status"] in {"done", "cancelled"}:
            raise WorkflowError("completed backlog item cannot be changed")
        next_number = int(backlog["next_item_number"])
        if action == "triage":
            if item.get("kind") not in {None, "idea"} or item["status"] != "open":
                raise WorkflowError("only an open idea may be triaged")
            target_key = (str(payload["assignee_provider"]), str(payload["assignee_user_id"]))
            target = member_map.get(target_key)
            if target is None:
                raise WorkflowError("backlog assignee is not an active project member")
            if item["id"] in payload["dependencies"]:
                raise WorkflowError("backlog task cannot depend on itself")
            missing = [
                dependency
                for dependency in payload["dependencies"]
                if dependency not in {row["id"] for row in items}
            ]
            if missing:
                raise WorkflowError(f"backlog dependencies do not exist: {missing}")
            item["kind"] = "task"
            item["triage_owner"] = authenticated_actor.as_mapping()
            item["assignee"] = target.as_mapping()
            item["priority"] = payload["priority"]
            item["requirements"] = payload["requirements"]
            item["acceptance_criteria"] = payload["acceptance_criteria"]
            item["dependencies"] = payload["dependencies"]
            item["scope_paths"] = payload["scope_paths"]
            item["evidence_required"] = payload["evidence_required"]
            item["status"] = "assigned"
            item["blocked_reason"] = None
            item["lease"] = None
            item["pull_request"] = None
            item["accepted_commit"] = None
        elif action == "assign":
            if item.get("kind") == "idea":
                raise WorkflowError("backlog idea must be triaged before assignment")
            target_key = (str(payload["assignee_provider"]), str(payload["assignee_user_id"]))
            target = member_map.get(target_key)
            if target is None:
                raise WorkflowError("backlog assignee is not an active project member")
            item["assignee"] = target.as_mapping()
            item["status"] = "assigned"
            item["blocked_reason"] = None
        elif action == "claim":
            if item.get("kind") == "idea":
                raise WorkflowError("backlog idea must be triaged before work starts")
            if item["assignee"] is not None and not _same_identity(item["assignee"], authenticated_actor):
                raise WorkflowError("backlog item is assigned to another actor")
            priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
            rows = {str(row["id"]): row for row in items}
            higher_priority = []
            for other in items:
                if (
                    other["id"] == item["id"]
                    or other["status"] != "assigned"
                    or other.get("assignee") is None
                    or not _same_identity(other["assignee"], authenticated_actor)
                    or priority_order.get(str(other["priority"]), 99)
                    >= priority_order.get(str(item["priority"]), 99)
                    or any(rows[dependency]["status"] != "done" for dependency in other["dependencies"])
                ):
                    continue
                if any(
                    active["id"] != other["id"]
                    and active.get("lease") is not None
                    and scope_sets_overlap(other["scope_paths"], active["lease"]["scope_paths"])
                    for active in items
                ):
                    continue
                higher_priority.append(str(other["id"]))
            if higher_priority:
                raise WorkflowError(
                    f"higher-priority assigned backlog items must start first: {higher_priority}"
                )
            expected_branch = f"work/{authenticated_actor.actor.username_snapshot}"
            if payload["branch"] != expected_branch:
                raise WorkflowError(f"backlog task must start on {expected_branch}")
            incomplete = [
                dependency
                for dependency in item["dependencies"]
                if rows[dependency]["status"] != "done"
            ]
            if incomplete:
                raise WorkflowError(f"backlog dependencies are incomplete: {incomplete}")
            for other in items:
                if other["id"] == item["id"] or other.get("lease") is None:
                    continue
                if scope_sets_overlap(item["scope_paths"], other["lease"]["scope_paths"]):
                    raise WorkflowError(f"backlog file scope conflicts with active task {other['id']}")
            item["assignee"] = authenticated_actor.as_mapping()
            item["status"] = "in_progress"
            item["blocked_reason"] = None
            if item.get("lease") is None:
                item["lease"] = {
                    "holder": authenticated_actor.as_mapping(),
                    "branch": payload["branch"],
                    "scope_paths": item["scope_paths"],
                    "acquired_at": committed_at,
                }
        elif action == "block":
            if item["assignee"] is None or not _same_identity(item["assignee"], authenticated_actor):
                raise WorkflowError("only the assignee may block this backlog item")
            item["status"] = "blocked"
            item["blocked_reason"] = payload["reason"]
        elif action == "cancel":
            item["status"] = "cancelled"
            item["blocked_reason"] = None
            if set(item) == EXTENDED_ITEM_KEYS:
                item["lease"] = None
                item["pull_request"] = None
                item["accepted_commit"] = None
        elif action == "amend_scope":
            if set(item) != EXTENDED_ITEM_KEYS or item.get("kind") != "task":
                raise WorkflowError("only an upgraded task scope may be amended")
            if item["status"] in {"in_review", "done", "cancelled"}:
                raise WorkflowError("task scope cannot change after review")
            for other in items:
                if other["id"] == item["id"] or other.get("lease") is None:
                    continue
                if scope_sets_overlap(payload["scope_paths"], other["lease"]["scope_paths"]):
                    raise WorkflowError(f"backlog file scope conflicts with active task {other['id']}")
            item["scope_paths"] = payload["scope_paths"]
            if isinstance(item.get("lease"), dict):
                item["lease"] = {**item["lease"], "scope_paths": payload["scope_paths"]}
        elif action == "recover":
            if item["assignee"] is None:
                raise WorkflowError("legacy task recovery requires an existing assignee")
            target_status = payload["target_status"]
            branch = payload["branch"]
            expected_branch = f"work/{item['assignee']['username_snapshot']}"
            if target_status in {"in_progress", "blocked"} and branch != expected_branch:
                raise WorkflowError(f"recovered active task must use {expected_branch}")
            item.update(
                {
                    "kind": "task",
                    "triage_owner": authenticated_actor.as_mapping(),
                    "requirements": payload["requirements"],
                    "acceptance_criteria": payload["acceptance_criteria"],
                    "scope_paths": payload["scope_paths"],
                    "lease": None,
                    "pull_request": None,
                    "accepted_commit": None,
                }
            )
            if target_status in {"in_progress", "blocked"}:
                for other in items:
                    if other["id"] == item["id"] or other.get("lease") is None:
                        continue
                    if scope_sets_overlap(payload["scope_paths"], other["lease"]["scope_paths"]):
                        raise WorkflowError(
                            f"backlog file scope conflicts with active task {other['id']}"
                        )
                item["lease"] = {
                    "holder": item["assignee"],
                    "branch": branch,
                    "scope_paths": payload["scope_paths"],
                    "acquired_at": committed_at,
                }
            item["status"] = target_status
            item["blocked_reason"] = (
                item.get("blocked_reason") if target_status == "blocked" else None
            )
        elif action == "review":
            if not acceptance_verified:
                raise WorkflowError("backlog review requires verified PR head and scope")
            if item["assignee"] is None or not _same_identity(item["assignee"], authenticated_actor):
                raise WorkflowError("only the assignee PR may enter review")
            lease = item.get("lease")
            if not isinstance(lease, dict) or lease.get("branch") != payload["source_branch"]:
                raise WorkflowError("verified pull request branch does not match task lease")
            outside = [
                path for path in payload["changed_paths"] if not path_allowed(path, item["scope_paths"])
            ]
            if outside:
                raise WorkflowError(f"pull request changes paths outside task scope: {outside}")
            item["status"] = "in_review"
            item["pull_request"] = payload["pull_request"]
            item["accepted_commit"] = payload["head_commit"]
            item["blocked_reason"] = None
        elif action == "complete":
            if not acceptance_verified:
                raise WorkflowError("backlog completion requires verified PR checks and merge")
            if item["assignee"] is None or not _same_identity(item["assignee"], authenticated_actor):
                raise WorkflowError("only the assignee may complete this backlog item")
            if (
                item["status"] != "in_review"
                or item.get("pull_request") != payload["pull_request"]
                or item.get("accepted_commit") != payload["source_commit"]
            ):
                raise WorkflowError("merged pull request does not match the verified review head")
            lease = item.get("lease")
            if not isinstance(lease, dict) or lease.get("branch") != payload["source_branch"]:
                raise WorkflowError("verified pull request branch does not match task lease")
            outside = [
                path for path in payload["changed_paths"] if not path_allowed(path, item["scope_paths"])
            ]
            if outside:
                raise WorkflowError(f"pull request changes paths outside task scope: {outside}")
            if item["evidence_required"] and not payload["evidence_refs"]:
                raise WorkflowError("backlog completion requires evidence")
            item["status"] = "done"
            item["blocked_reason"] = None
            item["evidence_refs"] = payload["evidence_refs"]
            item["lease"] = None
            item["pull_request"] = payload["pull_request"]
            item["accepted_commit"] = payload["merge_commit"]
        item["updated_at"] = committed_at
    items.sort(key=lambda value: str(value["id"]))
    revision = int(backlog["revision"]) + 1
    previous = backlog["events"][-1]["event_sha256"] if backlog["events"] else None
    event = {
        "schema_version": 1,
        "sequence": revision,
        "project_id": backlog["project_id"],
        "request_id": request["request_id"],
        "correlation_id": request["correlation_id"],
        "request_fingerprint": fingerprint,
        "action": action,
        "item_ids": [item["id"]],
        "requested_at": request["requested_at"],
        "committed_at": committed_at,
        "requested_by": authenticated_actor.as_mapping(),
        "committed_by": coordinator.as_mapping(),
        "items_sha256": _sha256_json(items),
        "previous_event_sha256": previous,
    }
    event["event_sha256"] = _sha256_json(event)
    updated = {
        **backlog,
        "revision": revision,
        "next_item_number": next_number,
        "updated_at": committed_at,
        "items": items,
        "events": [*backlog["events"], event],
    }
    validate_collaborative_backlog(updated)
    return {"applied": True, "reason": None, "backlog": updated, "item": item}


def backlog_audit_view(backlog_value: object) -> list[dict[str, object]]:
    backlog = validate_collaborative_backlog(backlog_value)
    return [
        {
            "sequence": event["sequence"],
            "action": event["action"],
            "item_ids": event["item_ids"],
            "requested_by": event["requested_by"],
            "committed_by": event["committed_by"],
            "request_id": event["request_id"],
            "correlation_id": event["correlation_id"],
            "requested_at": event["requested_at"],
            "committed_at": event["committed_at"],
        }
        for event in backlog["events"]
    ]
