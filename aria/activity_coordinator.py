from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from aria.activity import (
    EVENT_ID_RE,
    SOURCE_STAGES,
    activity_template,
    apply_activity_event,
    dump_activity,
    load_activity,
    parse_activity_event,
    validate_activity_snapshot,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE
from aria.provider import PROVIDER_ID_RE, ProviderActor


ACTIVITY_RECEIPTS_SCHEMA_VERSION = 1
ACTIVITY_TRANSACTION_SCHEMA_VERSION = 1
ACTIVITY_ARCHIVE_SCHEMA_VERSION = 1
MAX_ACTIVE_RECEIPTS = 1024
TARGET_ACTIVE_RECEIPTS = 512


@dataclass(frozen=True)
class ActivityCoordinatorPaths:
    activity: Path
    receipts: Path
    archive: Path
    transaction: Path
    lock: Path


def activity_coordinator_paths(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
) -> ActivityCoordinatorPaths:
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    runtime_activity = runtime_root / "activity" / project_id
    return ActivityCoordinatorPaths(
        activity=control_root / "ACTIVITY.yaml",
        receipts=runtime_activity / "receipts.json",
        archive=runtime_activity / "receipt-archive.json",
        transaction=runtime_activity / "transaction.json",
        lock=runtime_root / "locks" / f"activity-{project_id}.lock",
    )


def _exact(mapping: dict[str, object], expected: set[str], label: str) -> None:
    if set(mapping) != expected:
        raise ConfigurationError(f"{label} schema keys are invalid")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} must be a string-keyed mapping")
    return value


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _request_id(value: object, label: str) -> str:
    if not isinstance(value, str) or EVENT_ID_RE.fullmatch(value) is None:
        raise ConfigurationError(f"{label} is invalid")
    return value


def _receipts_template(project_id: str) -> dict[str, object]:
    return {
        "schema_version": ACTIVITY_RECEIPTS_SCHEMA_VERSION,
        "project_id": project_id,
        "receipts": [],
    }


def _validate_receipts(value: object, project_id: str) -> dict[str, object]:
    raw = _mapping(value, "activity receipts")
    _exact(raw, {"schema_version", "project_id", "receipts"}, "activity receipts")
    if (
        type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != ACTIVITY_RECEIPTS_SCHEMA_VERSION
    ):
        raise ConfigurationError("activity receipts schema_version must be 1")
    if raw.get("project_id") != project_id:
        raise ConfigurationError("activity receipts belong to another project")
    receipts = raw.get("receipts")
    if not isinstance(receipts, list):
        raise ConfigurationError("activity receipts must be a list")
    seen: set[str] = set()
    for index, receipt_value in enumerate(receipts):
        receipt = _mapping(receipt_value, f"activity receipt {index}")
        legacy_keys = {
            "request_id",
            "correlation_id",
            "event_id",
            "event_fingerprint",
            "applied",
            "reason",
            "revision",
        }
        if frozenset(receipt) not in {
            frozenset(legacy_keys),
            frozenset({*legacy_keys, "request_fingerprint"}),
        }:
            raise ConfigurationError(
                f"activity receipt {index} schema keys are invalid"
            )
        request_id = _request_id(receipt.get("request_id"), "activity request_id")
        _request_id(receipt.get("correlation_id"), "activity correlation_id")
        _request_id(receipt.get("event_id"), "activity receipt event_id")
        fingerprint = receipt.get("event_fingerprint")
        if not _is_sha256(fingerprint):
            raise ConfigurationError("activity receipt fingerprint is invalid")
        request_fingerprint = receipt.get("request_fingerprint")
        if "request_fingerprint" in receipt and not _is_sha256(request_fingerprint):
            raise ConfigurationError("activity receipt request fingerprint is invalid")
        if type(receipt.get("applied")) is not bool:
            raise ConfigurationError("activity receipt applied flag is invalid")
        reason = receipt.get("reason")
        if reason is not None and reason not in {"duplicate_event", "late_event"}:
            raise ConfigurationError("activity receipt reason is invalid")
        revision = receipt.get("revision")
        if type(revision) is not int or revision < 0:
            raise ConfigurationError("activity receipt revision is invalid")
        if request_id in seen:
            raise ConfigurationError("activity receipts contain duplicate request ids")
        seen.add(request_id)
    return raw


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Cannot read {label}: {path}") from error


def _load_receipts(path: Path, project_id: str) -> dict[str, object]:
    if not path.exists():
        return _receipts_template(project_id)
    return _validate_receipts(_load_json(path, "activity receipts"), project_id)


def _activity_bytes(snapshot: dict[str, object]) -> bytes:
    return dump_activity(snapshot).encode("utf-8")


def _receipts_bytes(receipts: dict[str, object], project_id: str) -> bytes:
    return json_bytes(_validate_receipts(receipts, project_id))


def activity_event_fingerprint(
    event_value: object,
    *,
    authorized_source: str,
    authorized_provider: str,
    authorized_actor: ProviderActor,
) -> str:
    parsed_event = parse_activity_event(event_value)
    event = {
        key: value for key, value in parsed_event.items() if key != "observed_at"
    }
    trusted_request = {
        "event": event,
        "authorized_source": authorized_source,
        "authorized_provider": authorized_provider,
        "authorized_actor": {
            "user_id": authorized_actor.user_id,
        },
    }
    encoded = json.dumps(
        trusted_request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(encoded)


def activity_request_fingerprint(
    event_value: object,
    *,
    request_id: str,
    correlation_id: str,
    expected_revision: int,
    authorized_source: str,
    authorized_provider: str,
    authorized_actor: ProviderActor,
    bind_observed_at: bool = True,
) -> str:
    """Bind a durable receipt to the complete request and stable actor id."""
    event = parse_activity_event(event_value)
    actor = _mapping(event["actor"], "activity actor")
    canonical_event = {
        **event,
        "actor": {
            "provider": actor["provider"],
            "user_id": actor["user_id"],
        },
    }
    if type(bind_observed_at) is not bool:
        raise ConfigurationError("activity request fingerprint binding is invalid")
    if not bind_observed_at:
        canonical_event = {
            key: value
            for key, value in canonical_event.items()
            if key != "observed_at"
        }
    _request_id(request_id, "activity request_id")
    _request_id(correlation_id, "activity correlation_id")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ConfigurationError("activity expected_revision is invalid")
    trusted_request = {
        "event": canonical_event,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "expected_revision": expected_revision,
        "authorized_source": authorized_source,
        "authorized_provider": authorized_provider,
        "authorized_actor": {"user_id": authorized_actor.user_id},
    }
    return _sha256(
        json.dumps(
            trusted_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def find_activity_receipt(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
    request_id: str,
) -> dict[str, object] | None:
    """Read one validated active or archived receipt without mutating storage."""
    paths = activity_coordinator_paths(
        control_root=control_root,
        runtime_root=runtime_root,
        project_id=project_id,
    )
    request_id = _request_id(request_id, "activity request_id")
    receipts = _load_receipts(paths.receipts, project_id)
    archive = _load_archive(paths.archive, project_id)
    matches = [
        receipt
        for receipt in [
            *receipts["receipts"],
            *(event["receipt"] for event in archive["events"]),
        ]
        if receipt["request_id"] == request_id
    ]
    if not matches:
        return None
    if any(receipt != matches[0] for receipt in matches[1:]):
        raise WorkflowError("activity receipt ledgers conflict")
    return dict(matches[0])


def _document_record(
    *,
    before_exists: bool,
    before: object,
    before_bytes: bytes,
    after: object,
    after_bytes: bytes,
) -> dict[str, object]:
    return {
        "before_exists": before_exists,
        "before": before,
        "before_sha256": _sha256(before_bytes) if before_exists else None,
        "after": after,
        "after_sha256": _sha256(after_bytes),
    }


def _transaction_bytes(transaction: dict[str, object]) -> bytes:
    return json_bytes(transaction)


def _validate_document_record(value: object, label: str) -> dict[str, object]:
    record = _mapping(value, label)
    _exact(
        record,
        {"before_exists", "before", "before_sha256", "after", "after_sha256"},
        label,
    )
    if type(record.get("before_exists")) is not bool:
        raise ConfigurationError(f"{label}.before_exists is invalid")
    before_hash = record.get("before_sha256")
    if record["before_exists"]:
        if not _is_sha256(before_hash):
            raise ConfigurationError(f"{label}.before_sha256 is invalid")
    elif before_hash is not None:
        raise ConfigurationError(f"{label}.before_sha256 must be null")
    after_hash = record.get("after_sha256")
    if not _is_sha256(after_hash):
        raise ConfigurationError(f"{label}.after_sha256 is invalid")
    return record


def _archive_template(project_id: str) -> dict[str, object]:
    return {
        "schema_version": ACTIVITY_ARCHIVE_SCHEMA_VERSION,
        "project_id": project_id,
        "events": [],
    }


def _validate_archive(value: object, project_id: str) -> dict[str, object]:
    raw = _mapping(value, "activity receipt archive")
    _exact(
        raw,
        {"schema_version", "project_id", "events"},
        "activity receipt archive",
    )
    if (
        raw.get("schema_version") != ACTIVITY_ARCHIVE_SCHEMA_VERSION
        or raw.get("project_id") != project_id
        or not isinstance(raw.get("events"), list)
    ):
        raise ConfigurationError("activity receipt archive is invalid")
    previous: str | None = None
    request_ids: set[str] = set()
    for sequence, event in enumerate(raw["events"], start=1):
        if not isinstance(event, dict) or set(event) != {
            "sequence",
            "receipt",
            "previous_archive_sha256",
            "archive_sha256",
        }:
            raise ConfigurationError("activity receipt archive event is invalid")
        receipt = _validate_receipts(
            {
                "schema_version": ACTIVITY_RECEIPTS_SCHEMA_VERSION,
                "project_id": project_id,
                "receipts": [event["receipt"]],
            },
            project_id,
        )["receipts"][0]
        if (
            event.get("sequence") != sequence
            or receipt["request_id"] in request_ids
            or event.get("previous_archive_sha256") != previous
        ):
            raise WorkflowError("activity receipt archive chain is invalid")
        actual = event.get("archive_sha256")
        expected = _sha256(
            json.dumps(
                {key: item for key, item in event.items() if key != "archive_sha256"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if not _is_sha256(actual) or actual != expected:
            raise WorkflowError("activity receipt archive hash is invalid")
        request_ids.add(str(receipt["request_id"]))
        previous = str(actual)
    return raw


def _load_archive(path: Path, project_id: str) -> dict[str, object]:
    return (
        _validate_archive(_load_json(path, "activity receipt archive"), project_id)
        if path.exists()
        else _archive_template(project_id)
    )


def _archive_receipts(
    paths: ActivityCoordinatorPaths,
    *,
    receipts: dict[str, object],
    archive: dict[str, object],
    project_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    archived_by_id = {
        event["receipt"]["request_id"]: event["receipt"] for event in archive["events"]
    }
    active: list[dict[str, object]] = []
    normalized = False
    for receipt in receipts["receipts"]:
        archived = archived_by_id.get(receipt["request_id"])
        if archived is None:
            active.append(receipt)
            continue
        if archived != receipt:
            raise WorkflowError("activity receipt archive conflicts with active ledger")
        normalized = True
    if normalized:
        receipts = {**receipts, "receipts": active}
        _install(paths.receipts, _receipts_bytes(receipts, project_id))
    if len(receipts["receipts"]) <= MAX_ACTIVE_RECEIPTS:
        return receipts, archive
    move_count = len(receipts["receipts"]) - TARGET_ACTIVE_RECEIPTS
    moving = receipts["receipts"][:move_count]
    events = list(archive["events"])
    previous = events[-1]["archive_sha256"] if events else None
    for receipt in moving:
        event = {
            "sequence": len(events) + 1,
            "receipt": receipt,
            "previous_archive_sha256": previous,
        }
        event["archive_sha256"] = _sha256(
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        events.append(event)
        previous = event["archive_sha256"]
    updated_archive = _validate_archive(
        {**archive, "events": events}, project_id
    )
    updated_receipts = _validate_receipts(
        {**receipts, "receipts": receipts["receipts"][move_count:]}, project_id
    )
    _install(paths.archive, json_bytes(updated_archive))
    _install(paths.receipts, _receipts_bytes(updated_receipts, project_id))
    if _load_archive(paths.archive, project_id) != updated_archive:
        raise WorkflowError("activity receipt archive read-back failed")
    if _load_receipts(paths.receipts, project_id) != updated_receipts:
        raise WorkflowError("compacted activity receipt ledger read-back failed")
    return updated_receipts, updated_archive


def _validate_transaction(value: object, project_id: str) -> dict[str, object]:
    raw = _mapping(value, "activity transaction")
    _exact(
        raw,
        {"schema_version", "project_id", "request_id", "activity", "receipts"},
        "activity transaction",
    )
    if (
        type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != ACTIVITY_TRANSACTION_SCHEMA_VERSION
    ):
        raise ConfigurationError("activity transaction schema_version must be 1")
    if raw.get("project_id") != project_id:
        raise ConfigurationError("activity transaction belongs to another project")
    _request_id(raw.get("request_id"), "activity transaction request_id")
    activity = _validate_document_record(raw.get("activity"), "transaction activity")
    receipts = _validate_document_record(raw.get("receipts"), "transaction receipts")
    before_activity = validate_activity_snapshot(activity["before"])
    after_activity = validate_activity_snapshot(activity["after"])
    if (
        before_activity["project_id"] != project_id
        or after_activity["project_id"] != project_id
    ):
        raise ConfigurationError("transaction activity belongs to another project")
    before_receipts = _validate_receipts(receipts["before"], project_id)
    after_receipts = _validate_receipts(receipts["after"], project_id)
    before_activity_bytes = _activity_bytes(before_activity)
    after_activity_bytes = _activity_bytes(after_activity)
    before_receipts_bytes = _receipts_bytes(before_receipts, project_id)
    after_receipts_bytes = _receipts_bytes(after_receipts, project_id)
    if activity["before_exists"] and activity["before_sha256"] != _sha256(
        before_activity_bytes
    ):
        raise ConfigurationError("transaction activity before hash is invalid")
    if activity["after_sha256"] != _sha256(after_activity_bytes):
        raise ConfigurationError("transaction activity after hash is invalid")
    if receipts["before_exists"] and receipts["before_sha256"] != _sha256(
        before_receipts_bytes
    ):
        raise ConfigurationError("transaction receipts before hash is invalid")
    if receipts["after_sha256"] != _sha256(after_receipts_bytes):
        raise ConfigurationError("transaction receipts after hash is invalid")
    return raw


def _current_document_state(path: Path, record: dict[str, object]) -> str:
    if not path.exists():
        if not record["before_exists"]:
            return "before"
        return "unknown"
    current_hash = _sha256(path.read_bytes())
    if current_hash == record["after_sha256"]:
        return "after"
    if record["before_exists"] and current_hash == record["before_sha256"]:
        return "before"
    return "unknown"


def _install(path: Path, content: bytes) -> None:
    atomic_write_bytes(path, content)


def _recover_transaction(paths: ActivityCoordinatorPaths, project_id: str) -> bool:
    if not paths.transaction.exists():
        return False
    transaction = _validate_transaction(
        _load_json(paths.transaction, "activity transaction"), project_id
    )
    activity = _mapping(transaction["activity"], "transaction activity")
    receipts = _mapping(transaction["receipts"], "transaction receipts")
    activity_state = _current_document_state(paths.activity, activity)
    receipts_state = _current_document_state(paths.receipts, receipts)
    if "unknown" in {activity_state, receipts_state}:
        raise WorkflowError(
            "Activity recovery found state outside the prepared transaction"
        )
    activity_after = validate_activity_snapshot(activity["after"])
    receipts_after = _validate_receipts(receipts["after"], project_id)
    activity_content = _activity_bytes(activity_after)
    receipts_content = _receipts_bytes(receipts_after, project_id)
    if activity_state == "before":
        _install(paths.activity, activity_content)
    if receipts_state == "before":
        _install(paths.receipts, receipts_content)
    if paths.activity.read_bytes() != activity_content:
        raise WorkflowError("Activity recovery read-back failed")
    if paths.receipts.read_bytes() != receipts_content:
        raise WorkflowError("Activity receipts recovery read-back failed")
    paths.transaction.unlink()
    return True


def submit_activity_event(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
    event_value: object,
    request_id: str,
    correlation_id: str,
    expected_revision: int,
    authorized_source: str,
    authorized_provider: str,
    authorized_actor: ProviderActor,
    received_at: str,
    bind_observed_at: bool = True,
) -> dict[str, object]:
    paths = activity_coordinator_paths(
        control_root=control_root,
        runtime_root=runtime_root,
        project_id=project_id,
    )
    request_id = _request_id(request_id, "activity request_id")
    correlation_id = _request_id(correlation_id, "activity correlation_id")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ConfigurationError("activity expected_revision is invalid")
    if authorized_source not in SOURCE_STAGES:
        raise ConfigurationError("activity authorized_source is invalid")
    if (
        not isinstance(authorized_provider, str)
        or PROVIDER_ID_RE.fullmatch(authorized_provider) is None
    ):
        raise ConfigurationError("activity authorized_provider is invalid")
    fingerprint = activity_event_fingerprint(
        event_value,
        authorized_source=authorized_source,
        authorized_provider=authorized_provider,
        authorized_actor=authorized_actor,
    )
    request_fingerprint = activity_request_fingerprint(
        event_value,
        request_id=request_id,
        correlation_id=correlation_id,
        expected_revision=expected_revision,
        authorized_source=authorized_source,
        authorized_provider=authorized_provider,
        authorized_actor=authorized_actor,
        bind_observed_at=bind_observed_at,
    )
    event = parse_activity_event(event_value)
    if event["project_id"] != project_id:
        raise WorkflowError("Activity request belongs to another project")

    with exclusive_lock(paths.lock):
        recovered = _recover_transaction(paths, project_id)
        snapshot = (
            load_activity(paths.activity)
            if paths.activity.exists()
            else activity_template(project_id)
        )
        receipts = _load_receipts(paths.receipts, project_id)
        archive = _load_archive(paths.archive, project_id)
        receipts, archive = _archive_receipts(
            paths,
            receipts=receipts,
            archive=archive,
            project_id=project_id,
        )
        archived_receipts = [event["receipt"] for event in archive["events"]]
        existing = next(
            (
                receipt
                for receipt in [*receipts["receipts"], *archived_receipts]
                if receipt["request_id"] == request_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing["event_fingerprint"] != fingerprint
                or existing["correlation_id"] != correlation_id
                or existing["event_id"] != event["event_id"]
                or (
                    existing.get("request_fingerprint") is not None
                    and existing["request_fingerprint"] != request_fingerprint
                )
            ):
                raise WorkflowError(
                    "Activity request id was reused with different content"
                )
            return {
                "applied": False,
                "reason": "duplicate_request",
                "recovered": recovered,
                "revision": snapshot["revision"],
                "snapshot": snapshot,
            }
        if snapshot["revision"] != expected_revision:
            raise WorkflowError(
                f"Stale activity revision: expected {expected_revision}, "
                f"actual {snapshot['revision']}"
            )
        result = apply_activity_event(
            snapshot,
            event,
            authorized_source=authorized_source,
            authorized_provider=authorized_provider,
            authorized_actor=authorized_actor,
            received_at=received_at,
        )
        updated_snapshot = validate_activity_snapshot(result["snapshot"])
        updated_receipts = {
            **receipts,
            "receipts": [
                *receipts["receipts"],
                {
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "event_id": event["event_id"],
                    "event_fingerprint": fingerprint,
                    "request_fingerprint": request_fingerprint,
                    "applied": result["applied"],
                    "reason": result["reason"],
                    "revision": updated_snapshot["revision"],
                },
            ],
        }
        _validate_receipts(updated_receipts, project_id)

        activity_before_exists = paths.activity.exists()
        receipts_before_exists = paths.receipts.exists()
        activity_before_bytes = _activity_bytes(snapshot)
        receipts_before_bytes = _receipts_bytes(receipts, project_id)
        activity_after_bytes = _activity_bytes(updated_snapshot)
        receipts_after_bytes = _receipts_bytes(updated_receipts, project_id)
        transaction = {
            "schema_version": ACTIVITY_TRANSACTION_SCHEMA_VERSION,
            "project_id": project_id,
            "request_id": request_id,
            "activity": _document_record(
                before_exists=activity_before_exists,
                before=snapshot,
                before_bytes=activity_before_bytes,
                after=updated_snapshot,
                after_bytes=activity_after_bytes,
            ),
            "receipts": _document_record(
                before_exists=receipts_before_exists,
                before=receipts,
                before_bytes=receipts_before_bytes,
                after=updated_receipts,
                after_bytes=receipts_after_bytes,
            ),
        }
        _validate_transaction(transaction, project_id)
        _install(paths.transaction, _transaction_bytes(transaction))
        _install(paths.activity, activity_after_bytes)
        _install(paths.receipts, receipts_after_bytes)
        if paths.activity.read_bytes() != activity_after_bytes:
            raise WorkflowError("Activity coordinator read-back failed")
        if paths.receipts.read_bytes() != receipts_after_bytes:
            raise WorkflowError("Activity coordinator receipt read-back failed")
        paths.transaction.unlink()
        _archive_receipts(
            paths,
            receipts=updated_receipts,
            archive=archive,
            project_id=project_id,
        )
        return {
            "applied": result["applied"],
            "reason": result["reason"],
            "recovered": recovered,
            "revision": updated_snapshot["revision"],
            "snapshot": updated_snapshot,
        }
