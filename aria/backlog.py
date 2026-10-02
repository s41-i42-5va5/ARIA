from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aria.access import authorize_access, load_access_policy
from aria.errors import ConfigurationError, WorkflowError
from aria.identity import sign_with_identity
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import (
    canonical_sha,
    git_is_ancestor,
    git_resolve_commit,
    git_snapshot,
)
from aria.signing import verify_bytes
from aria.team import load_team

BACKLOG_TYPES = {
    "feature",
    "bug",
    "debt",
    "risk",
    "clarification",
    "coverage-gap",
    "review-finding",
    "verification-failure",
    "blocker",
}
BACKLOG_PRIORITIES = {"critical", "high", "normal", "low"}
BACKLOG_STATUSES = {"open", "assigned", "in_progress", "blocked", "done"}


def _stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _relative(project: object) -> str:
    files = getattr(project, "files", None)
    relative = getattr(files, "backlog", None) if files is not None else None
    return relative if isinstance(relative, str) and relative else "BACKLOG.yaml"


def _path(project: object) -> Path:
    return Path(project.docs_root) / _relative(project)


def backlog_template(project_id: str) -> bytes:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "project_id": project_id,
            "revision": 0,
            "updated_at": None,
            "items": [],
            "events": [],
        },
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def _source_id(kind: str, reference: str) -> str:
    content = f"{kind}\0{reference}".encode("utf-8")
    return f"src:{hashlib.sha256(content).hexdigest()[:24]}"


def _items_sha(items: list[object]) -> str:
    return hashlib.sha256(json_bytes(items)).hexdigest()


def _event_core(event: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in event.items()
        if key not in {"event_sha256", "signature"}
    }


def _event_hash(event: dict[str, object]) -> str:
    return hashlib.sha256(json_bytes(_event_core(event))).hexdigest()


def _read(path: Path) -> dict[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"BACKLOG.yaml is unreadable: {path}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError("BACKLOG.yaml must be a mapping")
    return raw


def _string_list(value: object, label: str, *, allow_empty: bool = True) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise ConfigurationError(f"{label} must be a unique string list")
    return [item.strip() for item in value]


def _verified_completion_evidence(
    project: object, evidence: list[str]
) -> list[str]:
    verified: list[str] = []
    for reference in evidence:
        if reference.startswith("run:"):
            run_id = reference[4:]
            if (
                not run_id
                or Path(run_id).name != run_id
                or run_id in {".", ".."}
            ):
                raise WorkflowError(
                    f"Completion evidence has an invalid run id: {reference}"
                )
            try:
                from aria.simple_run import verify_completed_project_run

                verify_completed_project_run(project, run_id)
            except (ConfigurationError, WorkflowError) as error:
                raise WorkflowError(
                    f"Completion evidence run failed full closure read-back: {run_id}: {error}"
                ) from error
            verified.append(reference)
            continue
        if reference.startswith("git:"):
            supplied = reference[4:]
            if (
                len(supplied) != 40
                or any(character not in "0123456789abcdefABCDEF" for character in supplied)
            ):
                raise WorkflowError(
                    "Git completion evidence requires a full 40-character commit SHA"
                )
            try:
                resolved = git_resolve_commit(Path(project.code_root), supplied)
                snapshot = git_snapshot(
                    Path(project.code_root),
                    tuple(getattr(project, "git_ignore_prefixes", ())),
                )
                current = str(snapshot["head"])
                reachable = git_is_ancestor(Path(project.code_root), resolved, current)
            except ConfigurationError as error:
                raise WorkflowError(
                    f"Git completion evidence cannot be resolved: {supplied}"
                ) from error
            if (
                resolved.lower() != supplied.lower()
                or not reachable
                or resolved.lower() != current.lower()
                or snapshot.get("dirty") is not False
            ):
                raise WorkflowError(
                    "Git completion evidence requires the current reachable HEAD "
                    f"and a clean worktree: {supplied}"
                )
            verified.append(f"git:{resolved}")
            continue
        raise WorkflowError(
            "Completion evidence must reference a verified completed run "
            "('run:<run-id>') or reachable Git commit ('git:<full-sha>')"
        )
    return verified


def _validate_item(
    item: object,
    *,
    actors: dict[str, dict[str, object]],
    index: int,
) -> dict[str, object]:
    expected = {
        "id",
        "title",
        "type",
        "status",
        "priority",
        "creator",
        "assignee",
        "target_versions",
        "requirements",
        "refs",
        "dependencies",
        "acceptance",
        "blocked_reason",
        "completion_evidence",
        "source",
        "created_at",
        "updated_at",
    }
    if not isinstance(item, dict) or set(item) != expected:
        raise ConfigurationError(f"Backlog item {index} schema is invalid")
    if (
        not isinstance(item.get("id"), str)
        or not str(item["id"]).startswith("BLG-")
        or not isinstance(item.get("title"), str)
        or not str(item["title"]).strip()
        or item.get("type") not in BACKLOG_TYPES
        or item.get("status") not in BACKLOG_STATUSES
        or item.get("priority") not in BACKLOG_PRIORITIES
        or item.get("creator") not in actors
        or (
            item.get("assignee") is not None
            and item.get("assignee") not in actors
        )
        or not isinstance(item.get("acceptance"), str)
        or not str(item["acceptance"]).strip()
        or not isinstance(item.get("source"), dict)
        or set(item["source"]) != {"id", "kind", "ref"}
        or not all(
            isinstance(item["source"].get(key), str)
            and str(item["source"][key]).strip()
            for key in ("id", "kind", "ref")
        )
    ):
        raise ConfigurationError(f"Backlog item {index} identity is invalid")
    if item["source"]["id"] != _source_id(
        str(item["source"]["kind"]), str(item["source"]["ref"])
    ):
        raise ConfigurationError(f"Backlog item {index} source identity is invalid")
    _string_list(
        item.get("target_versions"),
        f"Backlog item {index}.target_versions",
        allow_empty=False,
    )
    for key in ("requirements", "refs", "dependencies", "completion_evidence"):
        _string_list(item.get(key), f"Backlog item {index}.{key}")
    if item["status"] == "done" and not item["completion_evidence"]:
        raise ConfigurationError(
            f"Backlog item {index} done status requires completion evidence"
        )
    if item["status"] == "blocked":
        if not isinstance(item.get("blocked_reason"), str) or not str(
            item["blocked_reason"]
        ).strip():
            raise ConfigurationError(
                f"Backlog item {index} blocked status requires a reason"
            )
    elif item.get("blocked_reason") is not None:
        raise ConfigurationError(
            f"Backlog item {index} has a reason outside blocked status"
        )
    return item


def _validate_backlog(project: object, raw: dict[str, object]) -> dict[str, object]:
    if (
        set(raw)
        != {
            "schema_version",
            "project_id",
            "revision",
            "updated_at",
            "items",
            "events",
        }
        or raw.get("schema_version") != 1
        or raw.get("project_id") != project.project_id
        or isinstance(raw.get("revision"), bool)
        or not isinstance(raw.get("revision"), int)
        or int(raw["revision"]) < 0
        or not isinstance(raw.get("items"), list)
        or not isinstance(raw.get("events"), list)
    ):
        raise ConfigurationError("BACKLOG.yaml schema or project identity is invalid")
    if raw["revision"] == 0:
        if raw["updated_at"] is not None or raw["items"] or raw["events"]:
            raise ConfigurationError("Initial BACKLOG.yaml must be empty")
        return raw
    if (
        not isinstance(raw.get("updated_at"), str)
        or len(raw["events"]) != raw["revision"]
    ):
        raise ConfigurationError("BACKLOG.yaml revision metadata is invalid")
    actors = load_team(project)
    items: list[dict[str, object]] = []
    ids: set[str] = set()
    sources: set[str] = set()
    for index, item in enumerate(raw["items"]):
        valid = _validate_item(item, actors=actors, index=index)
        if valid["id"] in ids or valid["source"]["id"] in sources:
            raise ConfigurationError("BACKLOG.yaml contains duplicate item/source identity")
        ids.add(str(valid["id"]))
        sources.add(str(valid["source"]["id"]))
        items.append(valid)
    for item in items:
        unknown = sorted(set(item["dependencies"]) - ids)
        if unknown:
            raise ConfigurationError(
                f"Backlog item {item['id']} has unknown dependencies: {unknown}"
            )
        if item["id"] in item["dependencies"]:
            raise ConfigurationError(f"Backlog item {item['id']} depends on itself")

    access = load_access_policy(project)
    devices = access["_devices"]
    previous = None
    for index, event in enumerate(raw["events"], start=1):
        if (
            not isinstance(event, dict)
            or set(event)
            != {
                "schema_version",
                "sequence",
                "timestamp",
                "type",
                "project_id",
                "actor_id",
                "device_id",
                "key_id",
                "backlog_revision",
                "items_sha256",
                "item_ids",
                "previous_event_sha256",
                "event_sha256",
                "signature",
            }
            or event.get("schema_version") != 1
            or event.get("sequence") != index
            or event.get("project_id") != project.project_id
            or event.get("backlog_revision") != index
            or event.get("previous_event_sha256") != previous
            or not isinstance(event.get("item_ids"), list)
            or not isinstance(event.get("signature"), dict)
        ):
            raise WorkflowError(f"Backlog event {index} identity is invalid")
        actual = _event_hash(event)
        if actual != event.get("event_sha256"):
            raise WorkflowError(f"Backlog event {index} hash is invalid")
        device = devices.get(str(event.get("key_id")))
        if (
            not isinstance(device, dict)
            or device.get("actor_id") != event.get("actor_id")
            or device.get("device_id") != event.get("device_id")
        ):
            raise WorkflowError(f"Backlog event {index} device is unknown")
        signature = event["signature"]
        verify_bytes(
            json_bytes(
                {key: value for key, value in event.items() if key != "signature"}
            ),
            public_key_b64=device.get("public_key"),
            signature_b64=signature.get("signature"),
            key_id=signature.get("key_id"),
        )
        previous = actual
    if raw["events"][-1]["items_sha256"] != _items_sha(raw["items"]):
        raise WorkflowError("BACKLOG.yaml state does not match its signed audit head")
    return raw


def load_backlog(project: object) -> dict[str, object]:
    return _validate_backlog(project, _read(_path(project)))


def _write_transition(
    project: object,
    *,
    current: dict[str, object],
    items: list[dict[str, object]],
    event_type: str,
    item_ids: list[str],
    identity: dict[str, object],
) -> dict[str, object]:
    revision = int(current["revision"]) + 1
    timestamp = _stamp()
    previous = (
        current["events"][-1]["event_sha256"] if current["events"] else None
    )
    event: dict[str, object] = {
        "schema_version": 1,
        "sequence": revision,
        "timestamp": timestamp,
        "type": event_type,
        "project_id": project.project_id,
        "actor_id": identity["actor_id"],
        "device_id": identity["device_id"],
        "key_id": identity["key_id"],
        "backlog_revision": revision,
        "items_sha256": _items_sha(items),
        "item_ids": item_ids,
        "previous_event_sha256": previous,
    }
    event["event_sha256"] = _event_hash(event)
    event["signature"] = sign_with_identity(
        identity,
        json_bytes({key: value for key, value in event.items() if key != "signature"}),
    )
    updated = {
        "schema_version": 1,
        "project_id": project.project_id,
        "revision": revision,
        "updated_at": timestamp,
        "items": items,
        "events": [*current["events"], event],
    }
    content = yaml.safe_dump(
        updated, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    target = _path(project)
    before = target.read_bytes()
    try:
        atomic_write_bytes(target, content)
        read_back = load_backlog(project)
        if (
            read_back["revision"] != revision
            or read_back["events"][-1]["event_sha256"] != event["event_sha256"]
        ):
            raise WorkflowError("BACKLOG.yaml atomic transition read-back failed")
    except BaseException as error:
        try:
            atomic_write_bytes(target, before)
            if target.read_bytes() != before:
                raise WorkflowError("BACKLOG.yaml rollback read-back failed")
        except BaseException as rollback_error:
            raise WorkflowError(
                "BACKLOG.yaml transition failed and rollback could not restore the preimage"
            ) from rollback_error
        raise error
    return read_back


def _authorize(
    project: object,
    *,
    permission: str,
    actor_id: str | None,
    device_id: str | None,
    version: str | None,
    branch: str | None,
) -> dict[str, object]:
    code_root = getattr(project, "code_root", None)
    if not isinstance(code_root, Path):
        raise ConfigurationError("Backlog authorization requires the registered code root")
    snapshot = git_snapshot(
        code_root, getattr(project, "git_ignore_prefixes", ())
    )
    actual = snapshot.get("branch")
    actual_branch = str(actual) if isinstance(actual, str) else None
    if branch is not None and branch != actual_branch:
        raise WorkflowError(
            "Backlog branch context does not match the registered Git checkout: "
            f"requested={branch!r}, actual={actual_branch!r}"
        )
    return authorize_access(
        project,
        permission=permission,
        actor_id=actor_id,
        device_id=device_id,
        version=version,
        branch=actual_branch,
    )


def add_backlog_item(
    project: object,
    *,
    title: str,
    item_type: str,
    priority: str,
    target_versions: list[str],
    acceptance: str,
    source_kind: str,
    source_ref: str | None,
    requirements: list[str] | None = None,
    refs: list[str] | None = None,
    dependencies: list[str] | None = None,
    assignee: str | None = None,
    expected_revision: int,
    actor_id: str | None = None,
    device_id: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    if not title.strip() or not acceptance.strip():
        raise ConfigurationError("Backlog title and acceptance are required")
    if item_type not in BACKLOG_TYPES or priority not in BACKLOG_PRIORITIES:
        raise ConfigurationError("Backlog type or priority is invalid")
    target_versions = _string_list(
        target_versions, "target_versions", allow_empty=False
    )
    requirements = _string_list(requirements or [], "requirements")
    refs = _string_list(refs or [], "refs")
    dependencies = _string_list(dependencies or [], "dependencies")
    version = target_versions[0] if len(target_versions) == 1 else None
    authorization = _authorize(
        project,
        permission="backlog.write",
        actor_id=actor_id,
        device_id=device_id,
        version=version,
        branch=branch,
    )
    source_ref = source_ref or f"manual:{uuid.uuid4().hex}"
    source = {
        "id": _source_id(source_kind, source_ref),
        "kind": source_kind,
        "ref": source_ref,
    }
    lock = Path(project.runtime_root) / "backlog" / "backlog.lock"
    with exclusive_lock(lock):
        current = load_backlog(project)
        existing = next(
            (
                item
                for item in current["items"]
                if item["source"]["id"] == source["id"]
            ),
            None,
        )
        if existing is not None:
            if (
                existing["title"] != title.strip()
                or existing["type"] != item_type
                or existing["source"] != source
            ):
                raise WorkflowError("Backlog source identity collides with another item")
            return {
                "ok": True,
                "project_id": project.project_id,
                "revision": current["revision"],
                "item": existing,
                "idempotent": True,
            }
        if current["revision"] != expected_revision:
            raise WorkflowError(
                f"Stale backlog revision: expected {expected_revision}, "
                f"actual {current['revision']}"
            )
        actors = load_team(project)
        if assignee is not None and assignee not in actors:
            raise WorkflowError(f"Unknown backlog assignee: {assignee}")
        unknown_dependencies = sorted(
            set(dependencies) - {str(item["id"]) for item in current["items"]}
        )
        if unknown_dependencies:
            raise WorkflowError(
                f"Unknown backlog dependencies: {unknown_dependencies}"
            )
        timestamp = _stamp()
        item = {
            "id": f"BLG-{source['id'].split(':', 1)[1][:12].upper()}",
            "title": title.strip(),
            "type": item_type,
            "status": "assigned" if assignee else "open",
            "priority": priority,
            "creator": authorization["actor_id"],
            "assignee": assignee,
            "target_versions": target_versions,
            "requirements": requirements,
            "refs": refs,
            "dependencies": dependencies,
            "acceptance": acceptance.strip(),
            "blocked_reason": None,
            "completion_evidence": [],
            "source": source,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        updated = _write_transition(
            project,
            current=current,
            items=[*current["items"], item],
            event_type="backlog_item_added",
            item_ids=[item["id"]],
            identity=authorization["identity"],
        )
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": updated["revision"],
            "item": item,
            "idempotent": False,
        }


def list_backlog_items(
    project: object,
    *,
    status: str | None = None,
    assignee: str | None = None,
    version: str | None = None,
    actor_id: str | None = None,
    device_id: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    _authorize(
        project,
        permission="backlog.read",
        actor_id=actor_id,
        device_id=device_id,
        version=version,
        branch=branch,
    )
    current = load_backlog(project)
    items = [
        item
        for item in current["items"]
        if (status is None or item["status"] == status)
        and (assignee is None or item["assignee"] == assignee)
        and (
            version is None
            or any(
                __import__("fnmatch").fnmatchcase(version, pattern)
                for pattern in item["target_versions"]
            )
        )
    ]
    return {
        "ok": True,
        "project_id": project.project_id,
        "revision": current["revision"],
        "items": items,
        "count": len(items),
    }


def show_backlog_item(
    project: object,
    *,
    item_id: str,
    actor_id: str | None = None,
    device_id: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    current = load_backlog(project)
    item = next((row for row in current["items"] if row["id"] == item_id), None)
    if item is None:
        raise WorkflowError(f"Unknown backlog item: {item_id}")
    versions = item.get("target_versions", [])
    scoped_version = (
        str(versions[0])
        if isinstance(versions, list) and len(versions) == 1
        else getattr(project, "framework_version", None)
    )
    _authorize(
        project,
        permission="backlog.read",
        actor_id=actor_id,
        device_id=device_id,
        version=scoped_version,
        branch=branch,
    )
    return {
        "ok": True,
        "project_id": project.project_id,
        "revision": current["revision"],
        "item": item,
    }


def _transition_item(
    project: object,
    *,
    item_id: str,
    expected_revision: int,
    permission: str,
    event_type: str,
    transform,
    actor_id: str | None,
    device_id: str | None,
    version: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    lock = Path(project.runtime_root) / "backlog" / "backlog.lock"
    with exclusive_lock(lock):
        current = load_backlog(project)
        if current["revision"] != expected_revision:
            raise WorkflowError(
                f"Stale backlog revision: expected {expected_revision}, "
                f"actual {current['revision']}"
            )
        target = next(
            (row for row in current["items"] if row["id"] == item_id), None
        )
        if target is None:
            raise WorkflowError(f"Unknown backlog item: {item_id}")
        target_versions = target.get("target_versions", [])
        scoped_version = version
        if (
            scoped_version is None
            and isinstance(target_versions, list)
            and len(target_versions) == 1
        ):
            scoped_version = str(target_versions[0])
        authorization = _authorize(
            project,
            permission=permission,
            actor_id=actor_id,
            device_id=device_id,
            version=scoped_version,
            branch=branch,
        )
        found = False
        items: list[dict[str, object]] = []
        updated_item: dict[str, object] | None = None
        for row in current["items"]:
            if row["id"] == item_id:
                found = True
                updated_item = transform(dict(row), authorization, current)
                items.append(updated_item)
            else:
                items.append(row)
        if not found or updated_item is None:
            raise WorkflowError(f"Unknown backlog item: {item_id}")
        updated = _write_transition(
            project,
            current=current,
            items=items,
            event_type=event_type,
            item_ids=[item_id],
            identity=authorization["identity"],
        )
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": updated["revision"],
            "item": updated_item,
        }


def assign_backlog_item(
    project: object,
    *,
    item_id: str,
    assignee: str,
    expected_revision: int,
    actor_id: str | None = None,
    device_id: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    actors = load_team(project)
    access = load_access_policy(project)
    if assignee not in actors or assignee not in access["_grants"]:
        raise WorkflowError(f"Backlog assignee has no project access: {assignee}")

    def transform(item, _authorization, _current):
        if item["status"] == "done":
            raise WorkflowError("Completed backlog item cannot be reassigned")
        item["assignee"] = assignee
        item["status"] = "assigned"
        item["blocked_reason"] = None
        item["updated_at"] = _stamp()
        return item

    return _transition_item(
        project,
        item_id=item_id,
        expected_revision=expected_revision,
        permission="backlog.assign",
        event_type="backlog_item_assigned",
        transform=transform,
        actor_id=actor_id,
        device_id=device_id,
        branch=branch,
    )


def claim_backlog_item(
    project: object,
    *,
    item_id: str,
    expected_revision: int,
    actor_id: str | None = None,
    device_id: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    def transform(item, authorization, current):
        if item["status"] not in {"open", "assigned"}:
            raise WorkflowError(
                f"Backlog item {item_id} cannot be claimed from {item['status']}"
            )
        statuses = {
            str(row["id"]): str(row["status"])
            for row in current["items"]
            if isinstance(row, dict)
        }
        incomplete = sorted(
            dependency
            for dependency in item["dependencies"]
            if statuses.get(dependency) != "done"
        )
        if incomplete:
            raise WorkflowError(
                f"Backlog item {item_id} has incomplete dependencies: {incomplete}"
            )
        if item["assignee"] not in {None, authorization["actor_id"]}:
            raise WorkflowError(
                f"Backlog item {item_id} is assigned to {item['assignee']!r}"
            )
        item["assignee"] = authorization["actor_id"]
        item["status"] = "in_progress"
        item["updated_at"] = _stamp()
        return item

    return _transition_item(
        project,
        item_id=item_id,
        expected_revision=expected_revision,
        permission="backlog.claim",
        event_type="backlog_item_claimed",
        transform=transform,
        actor_id=actor_id,
        device_id=device_id,
        branch=branch,
    )


def block_backlog_item(
    project: object,
    *,
    item_id: str,
    reason: str,
    expected_revision: int,
    actor_id: str | None = None,
    device_id: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    if not reason.strip():
        raise ConfigurationError("Blocked backlog item requires a reason")

    def transform(item, authorization, _current):
        if item["status"] == "done":
            raise WorkflowError("Completed backlog item cannot be blocked")
        if item["assignee"] not in {None, authorization["actor_id"]}:
            raise WorkflowError("Only the assignee may block this backlog item")
        item["assignee"] = authorization["actor_id"]
        item["status"] = "blocked"
        item["blocked_reason"] = reason.strip()
        item["updated_at"] = _stamp()
        return item

    return _transition_item(
        project,
        item_id=item_id,
        expected_revision=expected_revision,
        permission="backlog.write",
        event_type="backlog_item_blocked",
        transform=transform,
        actor_id=actor_id,
        device_id=device_id,
        branch=branch,
    )


def complete_backlog_item(
    project: object,
    *,
    item_id: str,
    evidence: list[str],
    expected_revision: int,
    actor_id: str | None = None,
    device_id: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    evidence = _string_list(evidence, "completion evidence", allow_empty=False)

    def transform(item, authorization, _current):
        if item["status"] != "in_progress":
            raise WorkflowError(
                f"Backlog item {item_id} must be in_progress before completion"
            )
        if item["assignee"] != authorization["actor_id"]:
            raise WorkflowError("Only the assignee may complete this backlog item")
        verified_evidence = _verified_completion_evidence(project, evidence)
        item["status"] = "done"
        item["completion_evidence"] = verified_evidence
        item["blocked_reason"] = None
        item["updated_at"] = _stamp()
        return item

    return _transition_item(
        project,
        item_id=item_id,
        expected_revision=expected_revision,
        permission="backlog.close",
        event_type="backlog_item_completed",
        transform=transform,
        actor_id=actor_id,
        device_id=device_id,
        branch=branch,
    )


def reconcile_accepted_decisions(
    project: object,
    *,
    item_ids: list[str],
    expected_revision: int,
    confirm_acceptance: bool = False,
    actor_id: str | None = None,
    device_id: str | None = None,
    version: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    if confirm_acceptance is not True:
        raise WorkflowError(
            "Decision reconciliation requires explicit acceptance confirmation"
        )
    selected = _string_list(item_ids, "reconciliation item ids", allow_empty=False)
    from aria.governance import decision_reconciliation_plan

    plan = decision_reconciliation_plan(project)
    planned = {
        str(row["item_id"]): row
        for row in plan["actions"]
        if isinstance(row, dict) and isinstance(row.get("item_id"), str)
    }
    unknown = sorted(set(selected) - set(planned))
    if unknown:
        raise WorkflowError(
            "Reconciliation items are not supported by accepted decision evidence: "
            f"{unknown}"
        )
    authorization = _authorize(
        project,
        permission="backlog.close",
        actor_id=actor_id,
        device_id=device_id,
        version=version,
        branch=branch,
    )
    lock = Path(project.runtime_root) / "backlog" / "backlog.lock"
    with exclusive_lock(lock):
        current = load_backlog(project)
        if current["revision"] != expected_revision:
            raise WorkflowError(
                f"Stale backlog revision: expected {expected_revision}, "
                f"actual {current['revision']}"
            )
        timestamp = _stamp()
        changed: list[str] = []
        items: list[dict[str, object]] = []
        for original in current["items"]:
            item = dict(original)
            item_id = str(item["id"])
            if item_id not in selected:
                items.append(item)
                continue
            evidence = list(planned[item_id]["evidence"])
            if item.get("status") == "done" and item.get("completion_evidence") == evidence:
                items.append(item)
                continue
            if item.get("type") != "clarification":
                raise WorkflowError(
                    f"Decision reconciliation can close only clarification items: {item_id}"
                )
            if item.get("assignee") not in {None, authorization["actor_id"]}:
                raise WorkflowError(
                    f"Clarification {item_id} is assigned to another actor"
                )
            item["status"] = "done"
            item["assignee"] = item.get("assignee") or authorization["actor_id"]
            item["blocked_reason"] = None
            item["completion_evidence"] = evidence
            item["updated_at"] = timestamp
            items.append(item)
            changed.append(item_id)
        if not changed:
            return {
                "ok": True,
                "project_id": project.project_id,
                "revision": current["revision"],
                "reconciled": [],
                "idempotent": True,
            }
        updated = _write_transition(
            project,
            current=current,
            items=items,
            event_type="accepted_decisions_reconciled",
            item_ids=changed,
            identity=authorization["identity"],
        )
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": updated["revision"],
            "reconciled": changed,
            "idempotent": False,
        }


def backlog_audit(
    project: object,
    *,
    actor_id: str | None = None,
    device_id: str | None = None,
    version: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    _authorize(
        project,
        permission="backlog.read",
        actor_id=actor_id,
        device_id=device_id,
        version=version,
        branch=branch,
    )
    current = load_backlog(project)
    return {
        "ok": True,
        "project_id": project.project_id,
        "revision": current["revision"],
        "events": len(current["events"]),
        "items": len(current["items"]),
        "head_sha256": (
            current["events"][-1]["event_sha256"] if current["events"] else None
        ),
        "items_sha256": _items_sha(current["items"]),
    }


def _automatic_candidates(project: object) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    runs = Path(project.runtime_root) / "runs"
    if not runs.is_dir():
        return candidates
    for manifest_path in sorted(runs.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        run_id = str(manifest.get("run_id") or manifest_path.parent.name)
        task = manifest.get("task")
        governance = manifest.get("governance")
        bound_item = (
            governance.get("backlog_item_id")
            if isinstance(governance, dict)
            else None
        )
        if isinstance(task, str) and task.strip() and not isinstance(bound_item, str):
            candidates.append(
                {
                    "title": task.strip(),
                    "type": "feature",
                    "priority": "normal",
                    "target_versions": ["*"],
                    "acceptance": "The linked ARIA run converges with required evidence",
                    "source_kind": "aria-run",
                    "source_ref": run_id,
                    "refs": [f"run:{run_id}"],
                }
            )
        if manifest.get("status") == "blocked":
            candidates.append(
                {
                    "title": f"Unblock ARIA run: {task or run_id}",
                    "type": "blocker",
                    "priority": "high",
                    "target_versions": ["*"],
                    "acceptance": "The linked ARIA run is unblocked and can continue",
                    "source_kind": "run-blocker",
                    "source_ref": run_id,
                    "refs": [f"run:{run_id}"],
                }
            )
        for receipt_path in sorted(
            (manifest_path.parent / "execution" / "receipts").glob("*.json")
        ):
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(receipt, dict)
                or receipt.get("status") == "passed"
                and receipt.get("exit_code") == 0
            ):
                continue
            command_id = str(receipt.get("command_id") or receipt_path.stem)
            candidates.append(
                {
                    "title": f"Fix failed verification: {command_id}",
                    "type": "verification-failure",
                    "priority": "high",
                    "target_versions": ["*"],
                    "acceptance": "The same verification command passes with fresh evidence",
                    "source_kind": "verification-failure",
                    "source_ref": f"{run_id}:{receipt.get('execution_id') or receipt_path.stem}",
                    "refs": [f"run:{run_id}", str(receipt_path)],
                }
            )
        for role_path in sorted((manifest_path.parent / "outputs" / "roles").glob("*.json")):
            try:
                role = json.loads(role_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            findings = role.get("findings", []) if isinstance(role, dict) else []
            if not isinstance(findings, list):
                continue
            for index, finding in enumerate(findings):
                if not isinstance(finding, dict):
                    continue
                summary = finding.get("summary") or finding.get("title")
                if isinstance(summary, str) and summary.strip():
                    candidates.append(
                        {
                            "title": summary.strip(),
                            "type": "review-finding",
                            "priority": (
                                str(finding.get("severity", "normal")).lower()
                                if str(finding.get("severity", "normal")).lower()
                                in BACKLOG_PRIORITIES
                                else "normal"
                            ),
                            "target_versions": ["*"],
                            "acceptance": "The finding is fixed and independently verified",
                            "source_kind": "review-finding",
                            "source_ref": f"{run_id}:{role_path.name}:{index}",
                            "refs": [f"run:{run_id}", str(role_path)],
                        }
                    )
    return candidates


def sync_backlog(
    project: object,
    *,
    expected_revision: int,
    actor_id: str | None = None,
    device_id: str | None = None,
    version: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    authorization = _authorize(
        project,
        permission="backlog.write",
        actor_id=actor_id,
        device_id=device_id,
        version=version,
        branch=branch,
    )
    lock = Path(project.runtime_root) / "backlog" / "backlog.lock"
    with exclusive_lock(lock):
        current = load_backlog(project)
        if current["revision"] != expected_revision:
            raise WorkflowError(
                f"Stale backlog revision: expected {expected_revision}, "
                f"actual {current['revision']}"
            )
        existing_sources = {
            str(item["source"]["id"]) for item in current["items"]
        }
        run_states: dict[str, str] = {}
        bound_runs: dict[str, tuple[str, str]] = {}
        runs_root = Path(project.runtime_root) / "runs"
        for manifest_path in sorted(runs_root.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and isinstance(manifest.get("status"), str):
                run_id = str(manifest.get("run_id") or manifest_path.parent.name)
                run_status = str(manifest["status"])
                run_states[run_id] = run_status
                governance = manifest.get("governance")
                item_id = (
                    governance.get("backlog_item_id")
                    if isinstance(governance, dict)
                    else None
                )
                if isinstance(item_id, str):
                    if item_id in bound_runs and bound_runs[item_id][0] != run_id:
                        raise WorkflowError(
                            f"Backlog item {item_id} is bound to multiple runs"
                        )
                    bound_runs[item_id] = (run_id, run_status)
        items: list[dict[str, object]] = []
        reconciled: list[str] = []
        timestamp = _stamp()
        for original in current["items"]:
            item = dict(original)
            bound = bound_runs.get(str(item.get("id")))
            if bound is not None:
                run_id, run_status = bound
                if run_status == "completed" and item.get("status") != "done":
                    completion_authorization = _authorize(
                        project,
                        permission="backlog.close",
                        actor_id=actor_id,
                        device_id=device_id,
                        version=version,
                        branch=branch,
                    )
                    verified_evidence = _verified_completion_evidence(
                        project, [f"run:{run_id}"]
                    )
                    if item.get("assignee") not in {
                        None,
                        completion_authorization["actor_id"],
                    }:
                        raise WorkflowError(
                            f"Bound backlog item {item['id']} is assigned to another actor"
                        )
                    item["status"] = "done"
                    item["assignee"] = completion_authorization["actor_id"]
                    item["blocked_reason"] = None
                    item["completion_evidence"] = verified_evidence
                    item["updated_at"] = timestamp
                    reconciled.append(str(item["id"]))
                elif run_status == "blocked" and item.get("status") not in {
                    "blocked",
                    "done",
                }:
                    item["status"] = "blocked"
                    item["blocked_reason"] = f"ARIA run {run_id} is blocked"
                    item["updated_at"] = timestamp
                    reconciled.append(str(item["id"]))
            source = item.get("source")
            if bound is None and isinstance(source, dict) and source.get("kind") == "aria-run":
                run_id = str(source.get("ref"))
                run_status = run_states.get(run_id)
                if run_status == "completed" and item.get("status") != "done":
                    completion_authorization = _authorize(
                        project,
                        permission="backlog.close",
                        actor_id=actor_id,
                        device_id=device_id,
                        version=version,
                        branch=branch,
                    )
                    verified_evidence = _verified_completion_evidence(
                        project, [f"run:{run_id}"]
                    )
                    item["status"] = "done"
                    item["assignee"] = (
                        item.get("assignee")
                        or completion_authorization["actor_id"]
                    )
                    item["blocked_reason"] = None
                    item["completion_evidence"] = verified_evidence
                    item["updated_at"] = timestamp
                    reconciled.append(str(item["id"]))
                elif run_status == "blocked" and item.get("status") not in {
                    "blocked",
                    "done",
                }:
                    item["status"] = "blocked"
                    item["blocked_reason"] = f"ARIA run {run_id} is blocked"
                    item["updated_at"] = timestamp
                    reconciled.append(str(item["id"]))
            items.append(item)
        added: list[str] = []
        for candidate in _automatic_candidates(project):
            source = {
                "id": _source_id(
                    str(candidate["source_kind"]), str(candidate["source_ref"])
                ),
                "kind": candidate["source_kind"],
                "ref": candidate["source_ref"],
            }
            if source["id"] in existing_sources:
                continue
            item = {
                "id": f"BLG-{source['id'].split(':', 1)[1][:12].upper()}",
                "title": candidate["title"],
                "type": candidate["type"],
                "status": "open",
                "priority": candidate["priority"],
                "creator": authorization["actor_id"],
                "assignee": None,
                "target_versions": candidate["target_versions"],
                "requirements": [],
                "refs": candidate["refs"],
                "dependencies": [],
                "acceptance": candidate["acceptance"],
                "blocked_reason": None,
                "completion_evidence": [],
                "source": source,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            items.append(item)
            added.append(str(item["id"]))
            existing_sources.add(str(source["id"]))
        changed = [*reconciled, *added]
        if not changed:
            return {
                "ok": True,
                "project_id": project.project_id,
                "revision": current["revision"],
                "added": [],
                "idempotent": True,
            }
        updated = _write_transition(
            project,
            current=current,
            items=items,
            event_type="backlog_synchronized",
            item_ids=changed,
            identity=authorization["identity"],
        )
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": updated["revision"],
            "added": added,
            "reconciled": reconciled,
            "idempotent": False,
        }
