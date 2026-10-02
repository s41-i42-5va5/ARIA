from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from aria.activity import EVENT_ID_RE, TASK_ID_RE
from aria.collaboration import ControlContract
from aria.collaborative_backlog import EVIDENCE_REF_RE, ProviderIdentity
from aria.errors import ConfigurationError, WorkflowError
from aria.github_integration import GIT_OID_RE, GitHubIntegrationAcceptance
from aria.provider import ProviderActor


STATE_SCHEMA_VERSION = 2


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} must be a string-keyed mapping")
    return value


def _exact(value: dict[str, object], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise ConfigurationError(f"{label} schema keys are invalid")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _stamp(value: object, label: str) -> str:
    from datetime import UTC, datetime

    if not isinstance(value, str) or not value.endswith("Z"):
        raise ConfigurationError(f"{label} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"{label} is invalid") from error
    if parsed.astimezone(UTC) != parsed:
        raise ConfigurationError(f"{label} must be UTC")
    return value


def _instant(value: object, label: str):
    from datetime import datetime

    text = _stamp(value, label)
    return datetime.fromisoformat(text[:-1] + "+00:00")


def _identity(value: object, label: str) -> dict[str, object]:
    raw = _mapping(value, label)
    _exact(
        raw,
        {"provider", "user_id", "username_snapshot", "display_name_snapshot"},
        label,
    )
    identity = ProviderIdentity(
        provider=str(raw.get("provider")),
        actor=ProviderActor(
            user_id=str(raw.get("user_id")),
            username_snapshot=str(raw.get("username_snapshot")),
            display_name_snapshot=raw.get("display_name_snapshot"),
        ),
    )
    if identity.as_mapping() != raw:
        raise ConfigurationError(f"{label} identity is invalid")
    return raw


def collaborative_state_template(contract: ControlContract) -> dict[str, object]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "project_id": contract.project_id,
        "revision": 0,
        "integration_branch": contract.integration_branch,
        "accepted_head": None,
        "accepted_at": None,
        "backlog_revision": 0,
        "components": [],
        "events": [],
    }


def _component(value: object, index: int) -> dict[str, object]:
    raw = _mapping(value, f"state component {index}")
    _exact(
        raw,
        {
            "id", "title", "status", "backlog_item_id", "pull_request_number",
            "accepted_commit", "accepted_at", "evidence_refs",
        },
        f"state component {index}",
    )
    if (
        not isinstance(raw.get("id"), str)
        or TASK_ID_RE.fullmatch(raw["id"]) is None
        or raw.get("backlog_item_id") != raw["id"]
        or not isinstance(raw.get("title"), str)
        or not raw["title"]
        or raw["title"] != raw["title"].strip()
        or len(raw["title"]) > 200
        or "\n" in raw["title"]
        or "\r" in raw["title"]
        or raw.get("status") != "accepted"
        or type(raw.get("pull_request_number")) is not int
        or raw["pull_request_number"] <= 0
        or not isinstance(raw.get("accepted_commit"), str)
        or GIT_OID_RE.fullmatch(raw["accepted_commit"]) is None
    ):
        raise ConfigurationError(f"state component {index} content is invalid")
    _stamp(raw.get("accepted_at"), f"state component {index} accepted_at")
    refs = raw.get("evidence_refs")
    if not isinstance(refs, list) or not refs or refs != sorted(set(refs)) or not all(
        isinstance(ref, str) and EVIDENCE_REF_RE.fullmatch(ref) is not None for ref in refs
    ):
        raise ConfigurationError(f"state component {index} evidence is invalid")
    return raw


def _event(value: object, index: int, project_id: str) -> dict[str, object]:
    raw = _mapping(value, f"state event {index}")
    _exact(
        raw,
        {
            "schema_version", "sequence", "project_id", "event_id", "item_id",
            "pull_request_number", "merge_commit", "accepted_at", "evidence_refs",
            "requested_by", "committed_by", "components_sha256",
            "acceptance_sha256", "previous_event_sha256", "event_sha256",
        },
        f"state event {index}",
    )
    if (
        type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != 1
        or type(raw.get("sequence")) is not int
        or raw.get("sequence") != index + 1
        or raw.get("project_id") != project_id
        or not isinstance(raw.get("event_id"), str)
        or EVENT_ID_RE.fullmatch(raw["event_id"]) is None
        or not isinstance(raw.get("item_id"), str)
        or TASK_ID_RE.fullmatch(raw["item_id"]) is None
        or type(raw.get("pull_request_number")) is not int
        or raw["pull_request_number"] <= 0
        or not isinstance(raw.get("merge_commit"), str)
        or GIT_OID_RE.fullmatch(raw["merge_commit"]) is None
        or not _is_sha256(raw.get("components_sha256"))
        or not _is_sha256(raw.get("acceptance_sha256"))
    ):
        raise ConfigurationError(f"state event {index} content is invalid")
    _stamp(raw.get("accepted_at"), f"state event {index} accepted_at")
    refs = raw.get("evidence_refs")
    if not isinstance(refs, list) or not refs or refs != sorted(set(refs)) or not all(
        isinstance(ref, str) and EVIDENCE_REF_RE.fullmatch(ref) is not None for ref in refs
    ):
        raise ConfigurationError(f"state event {index} evidence is invalid")
    _identity(raw.get("requested_by"), f"state event {index} requested_by")
    _identity(raw.get("committed_by"), f"state event {index} committed_by")
    previous = raw.get("previous_event_sha256")
    if previous is not None and not _is_sha256(previous):
        raise ConfigurationError(f"state event {index} previous hash is invalid")
    unhashed = {key: item for key, item in raw.items() if key != "event_sha256"}
    if raw.get("event_sha256") != _sha256_json(unhashed):
        raise ConfigurationError(f"state event {index} hash is invalid")
    return raw


def validate_collaborative_state(
    value: object, contract: ControlContract
) -> dict[str, object]:
    raw = _mapping(value, "collaborative STATE.yaml")
    _exact(
        raw,
        {
            "schema_version", "project_id", "revision", "integration_branch",
            "accepted_head", "accepted_at", "backlog_revision", "components", "events",
        },
        "collaborative STATE.yaml",
    )
    if (
        type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != STATE_SCHEMA_VERSION
        or raw.get("project_id") != contract.project_id
        or type(raw.get("revision")) is not int
        or raw["revision"] < 0
        or raw.get("integration_branch") != contract.integration_branch
        or type(raw.get("backlog_revision")) is not int
        or raw["backlog_revision"] < 0
        or not isinstance(raw.get("components"), list)
        or not isinstance(raw.get("events"), list)
    ):
        raise ConfigurationError("collaborative STATE.yaml content is invalid")
    if raw["revision"] == 0:
        if raw != collaborative_state_template(contract):
            raise ConfigurationError("collaborative STATE.yaml revision zero must be empty")
        return raw
    if (
        len(raw["events"]) != raw["revision"]
        or raw.get("accepted_head") is None
        or raw.get("accepted_at") is None
    ):
        raise ConfigurationError("collaborative STATE.yaml revision linkage is invalid")
    _stamp(raw["accepted_at"], "state accepted_at")
    if not isinstance(raw["accepted_head"], str) or GIT_OID_RE.fullmatch(raw["accepted_head"]) is None:
        raise ConfigurationError("state accepted_head is invalid")
    components = [_component(item, index) for index, item in enumerate(raw["components"])]
    if [item["id"] for item in components] != sorted({item["id"] for item in components}):
        raise ConfigurationError("state components are duplicated or unsorted")
    previous = None
    event_ids: set[str] = set()
    item_ids: set[str] = set()
    for index, value in enumerate(raw["events"]):
        event = _event(value, index, contract.project_id)
        if event["event_id"] in event_ids or event["item_id"] in item_ids:
            raise ConfigurationError("state events contain duplicate identities")
        event_ids.add(event["event_id"])
        item_ids.add(event["item_id"])
        if event["previous_event_sha256"] != previous:
            raise ConfigurationError("state event hash chain is invalid")
        previous = event["event_sha256"]
    latest = raw["events"][-1]
    if (
        latest["merge_commit"] != raw["accepted_head"]
        or latest["accepted_at"] != raw["accepted_at"]
        or latest["components_sha256"] != _sha256_json(components)
        or item_ids != {component["id"] for component in components}
    ):
        raise ConfigurationError("state latest checkpoint linkage is invalid")
    return raw


def dump_collaborative_state(value: object, contract: ControlContract) -> str:
    state = validate_collaborative_state(value, contract)
    content = yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
    if validate_collaborative_state(yaml.safe_load(content), contract) != state:
        raise ConfigurationError("collaborative STATE.yaml round-trip failed")
    return content


def load_collaborative_state(path: Path, contract: ControlContract) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot read collaborative STATE.yaml: {path}") from error
    return validate_collaborative_state(value, contract)


def validate_collaborative_history(
    content: str, state_value: dict[str, object]
) -> tuple[dict[str, object], ...]:
    if not isinstance(content, str):
        raise ConfigurationError("collaborative history is invalid")
    expected = state_value.get("events")
    if not isinstance(expected, list):
        raise ConfigurationError("collaborative state events are invalid")
    if not content.strip():
        if expected:
            raise WorkflowError("collaborative history does not match STATE.yaml")
        return ()
    events: list[dict[str, object]] = []
    for index, line in enumerate(content.splitlines()):
        if not line:
            raise ConfigurationError("collaborative history contains a blank event")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ConfigurationError("collaborative history event is invalid JSON") from error
        events.append(_mapping(value, f"collaborative history event {index}"))
    if events != expected:
        raise WorkflowError("collaborative history does not match STATE.yaml")
    return tuple(events)


def apply_state_acceptance(
    state_value: object,
    *,
    contract: ControlContract,
    backlog_item: dict[str, object],
    backlog_revision: int,
    acceptance: GitHubIntegrationAcceptance,
    coordinator: ProviderIdentity,
    event_id: str,
    expected_revision: int,
) -> dict[str, object]:
    state = validate_collaborative_state(state_value, contract)
    if EVENT_ID_RE.fullmatch(event_id) is None:
        raise ConfigurationError("state acceptance event id is invalid")
    if (
        acceptance.repository_id != contract.repository_id
        or acceptance.integration_branch != contract.integration_branch
    ):
        raise WorkflowError("integration acceptance does not match control contract")
    item_id = backlog_item.get("id")
    evidence = backlog_item.get("evidence_refs")
    assignee = backlog_item.get("assignee")
    fingerprint = _sha256_json(
        {
            "acceptance": acceptance.as_mapping(),
            "item_id": item_id,
            "evidence_refs": evidence,
            "backlog_revision": backlog_revision,
        }
    )
    existing = next(
        (event for event in state["events"] if event["event_id"] == event_id), None
    )
    if existing is not None:
        if existing["acceptance_sha256"] != fingerprint:
            raise WorkflowError("state acceptance event id was reused with different content")
        return {"applied": False, "reason": "duplicate_event", "state": state}
    if state["revision"] != expected_revision:
        raise WorkflowError(
            f"Stale collaborative state revision: expected {expected_revision}, actual {state['revision']}"
        )
    if state["accepted_at"] is not None and _instant(
        acceptance.merged_at, "state new accepted_at"
    ) <= _instant(state["accepted_at"], "state previous accepted_at"):
        raise WorkflowError("state acceptance time must move forward")
    if (
        not isinstance(item_id, str)
        or TASK_ID_RE.fullmatch(item_id) is None
        or acceptance.backlog_item_id != item_id
        or backlog_item.get("status") != "done"
        or not isinstance(evidence, list)
        or not evidence
        or not isinstance(assignee, dict)
        or assignee.get("provider") != "github"
        or assignee.get("user_id") != acceptance.pull_request_author.user_id
        or type(backlog_revision) is not int
        or backlog_revision <= state["backlog_revision"]
    ):
        raise WorkflowError("accepted backlog item is not eligible for state publication")
    if any(component["id"] == item_id for component in state["components"]):
        raise WorkflowError("backlog item already exists in canonical state")
    component = {
        "id": item_id,
        "title": backlog_item["title"],
        "status": "accepted",
        "backlog_item_id": item_id,
        "pull_request_number": acceptance.pull_request_number,
        "accepted_commit": acceptance.merge_commit,
        "accepted_at": acceptance.merged_at,
        "evidence_refs": evidence,
    }
    components = sorted([*state["components"], component], key=lambda value: value["id"])
    event = {
        "schema_version": 1,
        "sequence": state["revision"] + 1,
        "project_id": contract.project_id,
        "event_id": event_id,
        "item_id": item_id,
        "pull_request_number": acceptance.pull_request_number,
        "merge_commit": acceptance.merge_commit,
        "accepted_at": acceptance.merged_at,
        "evidence_refs": evidence,
        "requested_by": ProviderIdentity("github", acceptance.pull_request_author).as_mapping(),
        "committed_by": coordinator.as_mapping(),
        "components_sha256": _sha256_json(components),
        "acceptance_sha256": fingerprint,
        "previous_event_sha256": state["events"][-1]["event_sha256"] if state["events"] else None,
    }
    event["event_sha256"] = _sha256_json(event)
    updated = {
        **state,
        "revision": state["revision"] + 1,
        "accepted_head": acceptance.merge_commit,
        "accepted_at": acceptance.merged_at,
        "backlog_revision": backlog_revision,
        "components": components,
        "events": [*state["events"], event],
    }
    validate_collaborative_state(updated, contract)
    return {"applied": True, "reason": None, "state": updated, "component": component}
