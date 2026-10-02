from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from aria.activity import EVENT_ID_RE
from aria.collaboration import ControlContract, load_control_contract
from aria.collaborative_backlog import ProviderIdentity
from aria.collaborative_state import (
    apply_state_acceptance,
    collaborative_state_template,
    dump_collaborative_state,
    load_collaborative_state,
    validate_collaborative_history,
    validate_collaborative_state,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.github_integration import GitHubIntegrationAcceptance
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE


@dataclass(frozen=True)
class CollaborativeStateCoordinatorPaths:
    state: Path
    history: Path
    transaction: Path
    lock: Path


def collaborative_state_coordinator_paths(
    *, control_root: Path, runtime_root: Path, project_id: str,
) -> CollaborativeStateCoordinatorPaths:
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    transaction_root = runtime_root / "collaborative-state" / project_id
    return CollaborativeStateCoordinatorPaths(
        state=control_root / "STATE.yaml",
        history=control_root / "HISTORY.jsonl",
        transaction=transaction_root / "transaction.json",
        lock=runtime_root / "locks" / f"collaborative-state-{project_id}.lock",
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _record(before: bytes, after: bytes) -> dict[str, object]:
    return {
        "before_sha256": _sha256(before),
        "after_sha256": _sha256(after),
        "after": after.decode("utf-8"),
    }


def _load_transaction(path: Path, *, project_id: str, event_id: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("collaborative state transaction is unreadable") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "project_id", "event_id", "state", "history"}
        or value.get("schema_version") != 1
        or value.get("project_id") != project_id
        or value.get("event_id") != event_id
    ):
        raise ConfigurationError("collaborative state transaction is invalid")
    for name in ("state", "history"):
        record = value.get(name)
        if not isinstance(record, dict) or set(record) != {
            "before_sha256", "after_sha256", "after"
        }:
            raise ConfigurationError("collaborative state transaction record is invalid")
        after = record.get("after")
        if (
            not isinstance(after, str)
            or not all(
                isinstance(record.get(key), str)
                and len(record[key]) == 64
                and all(character in "0123456789abcdef" for character in record[key])
                for key in ("before_sha256", "after_sha256")
            )
            or _sha256(after.encode("utf-8")) != record["after_sha256"]
        ):
            raise ConfigurationError("collaborative state transaction hash is invalid")
    return value


def _recover(paths: CollaborativeStateCoordinatorPaths, transaction: dict[str, object]) -> None:
    for name, path in (("state", paths.state), ("history", paths.history)):
        record = transaction[name]
        current = path.read_bytes()
        current_hash = _sha256(current)
        if current_hash == record["before_sha256"]:
            atomic_write_bytes(path, record["after"].encode("utf-8"))
        elif current_hash != record["after_sha256"]:
            raise WorkflowError("collaborative state recovery found unknown document state")
        if _sha256(path.read_bytes()) != record["after_sha256"]:
            raise WorkflowError("collaborative state recovery read-back failed")


def _validate_after_documents(
    transaction: dict[str, object], contract: ControlContract
) -> dict[str, object]:
    try:
        value = yaml.safe_load(transaction["state"]["after"])
    except yaml.YAMLError as error:
        raise ConfigurationError("collaborative state transaction YAML is invalid") from error
    state = validate_collaborative_state(value, contract)
    validate_collaborative_history(transaction["history"]["after"], state)
    return state


def submit_state_acceptance(
    *,
    control_root: Path,
    runtime_root: Path,
    project_id: str,
    backlog_item: dict[str, object],
    backlog_revision: int,
    acceptance: GitHubIntegrationAcceptance,
    coordinator: ProviderIdentity,
    event_id: str,
    expected_revision: int,
) -> dict[str, object]:
    if EVENT_ID_RE.fullmatch(event_id) is None:
        raise ConfigurationError("state acceptance event id is invalid")
    paths = collaborative_state_coordinator_paths(
        control_root=control_root, runtime_root=runtime_root, project_id=project_id
    )
    contract = load_control_contract(control_root / "CONTROL.yaml")
    if contract.project_id != project_id:
        raise WorkflowError("state control contract belongs to another project")
    with exclusive_lock(paths.lock):
        recovered = paths.transaction.exists()
        if recovered:
            transaction = _load_transaction(
                paths.transaction, project_id=project_id, event_id=event_id
            )
            _validate_after_documents(transaction, contract)
            _recover(paths, transaction)
            paths.transaction.unlink()
        state = (
            load_collaborative_state(paths.state, contract)
            if paths.state.exists()
            else collaborative_state_template(contract)
        )
        result = apply_state_acceptance(
            state,
            contract=contract,
            backlog_item=backlog_item,
            backlog_revision=backlog_revision,
            acceptance=acceptance,
            coordinator=coordinator,
            event_id=event_id,
            expected_revision=expected_revision,
        )
        if not result["applied"]:
            return {**result, "recovered": recovered, "revision": state["revision"]}
        updated = validate_collaborative_state(result["state"], contract)
        state_before = dump_collaborative_state(state, contract).encode("utf-8")
        state_after = dump_collaborative_state(updated, contract).encode("utf-8")
        history_before = paths.history.read_bytes()
        validate_collaborative_history(history_before.decode("utf-8"), state)
        history_after = (
            "\n".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for event in updated["events"]
            )
            + "\n"
        ).encode("utf-8")
        validate_collaborative_history(history_after.decode("utf-8"), updated)
        transaction = {
            "schema_version": 1,
            "project_id": project_id,
            "event_id": event_id,
            "state": _record(state_before, state_after),
            "history": _record(history_before, history_after),
        }
        atomic_write_bytes(paths.transaction, json_bytes(transaction))
        atomic_write_bytes(paths.state, state_after)
        atomic_write_bytes(paths.history, history_after)
        checked = _load_transaction(
            paths.transaction, project_id=project_id, event_id=event_id
        )
        _validate_after_documents(checked, contract)
        _recover(paths, checked)
        paths.transaction.unlink()
        return {
            **result,
            "recovered": recovered,
            "revision": updated["revision"],
        }
