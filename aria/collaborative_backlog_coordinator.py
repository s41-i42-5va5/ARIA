from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from aria.activity import EVENT_ID_RE
from aria.collaborative_backlog import (
    ProviderIdentity,
    apply_backlog_request,
    collaborative_backlog_template,
    dump_collaborative_backlog,
    load_collaborative_backlog,
    parse_backlog_request,
    validate_collaborative_backlog,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE


BACKLOG_TRANSACTION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CollaborativeBacklogCoordinatorPaths:
    backlog: Path
    transaction: Path
    lock: Path


def collaborative_backlog_coordinator_paths(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
) -> CollaborativeBacklogCoordinatorPaths:
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    runtime_backlog = runtime_root / "collaborative-backlog" / project_id
    return CollaborativeBacklogCoordinatorPaths(
        backlog=control_root / "BACKLOG.yaml",
        transaction=runtime_backlog / "transaction.json",
        lock=runtime_root / "locks" / f"collaborative-backlog-{project_id}.lock",
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


def _backlog_bytes(backlog: object) -> bytes:
    return dump_collaborative_backlog(backlog).encode("utf-8")


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Cannot read {label}: {path}") from error


def _validate_transaction(value: object, project_id: str) -> dict[str, object]:
    raw = _mapping(value, "collaborative backlog transaction")
    _exact(
        raw,
        {
            "schema_version",
            "project_id",
            "request_id",
            "before_exists",
            "before",
            "before_sha256",
            "after",
            "after_sha256",
        },
        "collaborative backlog transaction",
    )
    if (
        type(raw.get("schema_version")) is not int
        or raw["schema_version"] != BACKLOG_TRANSACTION_SCHEMA_VERSION
    ):
        raise ConfigurationError("collaborative backlog transaction schema is invalid")
    if raw.get("project_id") != project_id:
        raise ConfigurationError("collaborative backlog transaction belongs to another project")
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or EVENT_ID_RE.fullmatch(request_id) is None:
        raise ConfigurationError("collaborative backlog transaction request id is invalid")
    if type(raw.get("before_exists")) is not bool:
        raise ConfigurationError("collaborative backlog transaction before_exists is invalid")
    before = validate_collaborative_backlog(raw.get("before"))
    after = validate_collaborative_backlog(raw.get("after"))
    if before["project_id"] != project_id or after["project_id"] != project_id:
        raise ConfigurationError("collaborative backlog transaction document project mismatch")
    before_hash = raw.get("before_sha256")
    if raw["before_exists"]:
        if not _is_sha256(before_hash) or before_hash != _sha256(_backlog_bytes(before)):
            raise ConfigurationError("collaborative backlog transaction before hash is invalid")
    elif before_hash is not None:
        raise ConfigurationError("missing backlog transaction before hash must be null")
    after_hash = raw.get("after_sha256")
    if not _is_sha256(after_hash) or after_hash != _sha256(_backlog_bytes(after)):
        raise ConfigurationError("collaborative backlog transaction after hash is invalid")
    if int(after["revision"]) != int(before["revision"]) + 1:
        raise ConfigurationError("collaborative backlog transaction revision is invalid")
    if not after["events"] or after["events"][-1]["request_id"] != request_id:
        raise ConfigurationError("collaborative backlog transaction request does not match audit")
    return raw


def _install(path: Path, content: bytes) -> None:
    atomic_write_bytes(path, content)


def _recover_transaction(
    paths: CollaborativeBacklogCoordinatorPaths,
    project_id: str,
) -> bool:
    if not paths.transaction.exists():
        return False
    transaction = _validate_transaction(
        _load_json(paths.transaction, "collaborative backlog transaction"),
        project_id,
    )
    after = validate_collaborative_backlog(transaction["after"])
    after_content = _backlog_bytes(after)
    if not paths.backlog.exists():
        if transaction["before_exists"]:
            raise WorkflowError("collaborative backlog recovery found a missing preimage")
        state = "before"
    else:
        current_hash = _sha256(paths.backlog.read_bytes())
        if current_hash == transaction["after_sha256"]:
            state = "after"
        elif transaction["before_exists"] and current_hash == transaction["before_sha256"]:
            state = "before"
        else:
            raise WorkflowError(
                "collaborative backlog recovery found state outside the prepared transaction"
            )
    if state == "before":
        _install(paths.backlog, after_content)
    if paths.backlog.read_bytes() != after_content:
        raise WorkflowError("collaborative backlog recovery read-back failed")
    paths.transaction.unlink()
    return True


def submit_collaborative_backlog_request(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
    request_value: object,
    authenticated_actor: ProviderIdentity,
    active_members: tuple[ProviderIdentity, ...],
    permissions: frozenset[str],
    coordinator: ProviderIdentity,
    expected_revision: int,
    committed_at: str,
    acceptance_verified: bool = False,
) -> dict[str, object]:
    paths = collaborative_backlog_coordinator_paths(
        control_root=control_root,
        runtime_root=runtime_root,
        project_id=project_id,
    )
    request = parse_backlog_request(request_value)
    if request["project_id"] != project_id:
        raise WorkflowError("collaborative backlog request belongs to another project")
    with exclusive_lock(paths.lock):
        recovered = _recover_transaction(paths, project_id)
        backlog = (
            load_collaborative_backlog(paths.backlog)
            if paths.backlog.exists()
            else collaborative_backlog_template(project_id)
        )
        result = apply_backlog_request(
            backlog,
            request,
            authenticated_actor=authenticated_actor,
            active_members=active_members,
            permissions=permissions,
            coordinator=coordinator,
            expected_revision=expected_revision,
            committed_at=committed_at,
            acceptance_verified=acceptance_verified,
        )
        if not result["applied"]:
            return {**result, "recovered": recovered, "revision": backlog["revision"]}
        updated = validate_collaborative_backlog(result["backlog"])
        before_exists = paths.backlog.exists()
        before_content = _backlog_bytes(backlog)
        after_content = _backlog_bytes(updated)
        transaction = {
            "schema_version": BACKLOG_TRANSACTION_SCHEMA_VERSION,
            "project_id": project_id,
            "request_id": request["request_id"],
            "before_exists": before_exists,
            "before": backlog,
            "before_sha256": _sha256(before_content) if before_exists else None,
            "after": updated,
            "after_sha256": _sha256(after_content),
        }
        _validate_transaction(transaction, project_id)
        _install(paths.transaction, json_bytes(transaction))
        _install(paths.backlog, after_content)
        if paths.backlog.read_bytes() != after_content:
            raise WorkflowError("collaborative backlog coordinator read-back failed")
        paths.transaction.unlink()
        return {**result, "recovered": recovered, "revision": updated["revision"]}
