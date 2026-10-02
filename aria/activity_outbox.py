from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aria.activity import load_activity, parse_activity_event, validate_activity_snapshot
from aria.activity_coordinator import (
    activity_event_fingerprint,
    activity_request_fingerprint,
    find_activity_receipt,
)
from aria.collaboration import load_control_contract
from aria.collaborative_backlog import (
    ProviderIdentity,
    backlog_request_fingerprint,
    load_collaborative_backlog,
    parse_backlog_request,
    validate_collaborative_backlog,
)
from aria.collaborative_documents import validate_collaborative_access
from aria.collaborative_runtime import (
    AuthenticatedCollaborativeAdapter,
    authorize_collaborative_actor,
    machine_runtime_root,
)
from aria.collaborative_team import active_team_identities, load_collaborative_team
from aria.errors import ConfigurationError, WorkflowError
from aria.github_request_queue import (
    GitHubRequestQueue,
    load_queue_request,
    validate_queue_request,
)
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE, ProjectConfig
from aria.provider import ProviderActor


OUTBOX_SCHEMA_VERSION = 1
MAX_OUTBOX_REQUESTS = 1024


@dataclass(frozen=True)
class CachedActivityAuthorization:
    actor: ProviderIdentity
    permissions: frozenset[str]


@dataclass(frozen=True)
class ActivityOutboxPaths:
    root: Path
    identity: Path
    outbox: Path
    terminal: Path
    lock: Path


def activity_outbox_paths(project: ProjectConfig) -> ActivityOutboxPaths:
    if PROJECT_ID_RE.fullmatch(project.project_id) is None:
        raise ConfigurationError("activity outbox project id is invalid")
    root = machine_runtime_root(project) / "activity-outbox" / project.project_id
    return ActivityOutboxPaths(
        root=root,
        identity=root / "identity.json",
        outbox=root / "outbox.json",
        terminal=root / "terminal.json",
        lock=machine_runtime_root(project)
        / "locks"
        / f"activity-outbox-{project.project_id}.lock",
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ConfigurationError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"{label} is invalid") from error
    if parsed.astimezone(UTC) != parsed:
        raise ConfigurationError(f"{label} is invalid")
    return value


def _load_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"{label} is unreadable") from error


def _yaml_mapping(path: Path, label: str) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"{label} is unreadable") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} is invalid")
    return value


def cache_activity_identity(
    project: ProjectConfig, *, actor: ProviderIdentity, observed_at: str | None = None
) -> dict[str, object]:
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    if actor.provider != contract.provider:
        raise WorkflowError("activity identity cache provider mismatch")
    value = {
        "schema_version": 1,
        "project_id": project.project_id,
        "provider": contract.provider,
        "repository_id": contract.repository_id,
        "actor": actor.as_mapping(),
        "observed_at": observed_at or _utc_now(),
    }
    paths = activity_outbox_paths(project)
    with exclusive_lock(paths.lock):
        atomic_write_bytes(paths.identity, json_bytes(value))
    return value


def cached_activity_authorization(project: ProjectConfig) -> CachedActivityAuthorization:
    paths = activity_outbox_paths(project)
    if not paths.identity.is_file():
        raise WorkflowError(
            "offline activity queue has no previously verified GitHub identity"
        )
    value = _load_json(paths.identity, "activity identity cache")
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "project_id",
        "provider",
        "repository_id",
        "actor",
        "observed_at",
    }:
        raise ConfigurationError("activity identity cache is invalid")
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    if (
        value.get("schema_version") != 1
        or value.get("project_id") != project.project_id
        or value.get("provider") != contract.provider
        or value.get("repository_id") != contract.repository_id
        or not isinstance(value.get("actor"), dict)
    ):
        raise WorkflowError("activity identity cache belongs to another authority")
    cached = value["actor"]
    team = load_collaborative_team(project.docs_root / "ARIA_TEAM.yaml")
    identities = active_team_identities(team)
    actor = next(
        (
            identity
            for identity in identities
            if identity.provider == cached.get("provider")
            and identity.actor.user_id == cached.get("user_id")
        ),
        None,
    )
    if actor is None:
        raise WorkflowError("cached activity actor is not active in the local team projection")
    team_member = next(
        (
            member
            for member in team["members"]
            if member["provider"] == actor.provider
            and member["user_id"] == actor.actor.user_id
            and member["active"] is True
        ),
        None,
    )
    if team_member is None:
        raise WorkflowError("cached activity actor has no active team role")
    access = validate_collaborative_access(
        _yaml_mapping(project.docs_root / "ACCESS.yaml", "ACCESS.yaml"), contract
    )
    permissions: set[str] = set()
    for role in team_member["roles"]:
        grants = access["role_permissions"].get(role)
        if not isinstance(grants, list):
            raise WorkflowError("cached activity actor role is absent from ACCESS.yaml")
        permissions.update(str(permission) for permission in grants)
    if "activity.write" not in permissions:
        raise WorkflowError("cached activity actor cannot write activity")
    return CachedActivityAuthorization(actor, frozenset(permissions))


def _template(project_id: str) -> dict[str, object]:
    return {
        "schema_version": OUTBOX_SCHEMA_VERSION,
        "project_id": project_id,
        "requests": [],
    }


def _validate_outbox(value: object, project_id: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "project_id", "requests"}
        or value.get("schema_version") != OUTBOX_SCHEMA_VERSION
        or value.get("project_id") != project_id
        or not isinstance(value.get("requests"), list)
        or len(value["requests"]) > MAX_OUTBOX_REQUESTS
    ):
        raise ConfigurationError("activity outbox is invalid")
    request_ids: set[str] = set()
    for entry in value["requests"]:
        legacy_keys = {
            "request",
            "actor_user_id",
            "actor_username_snapshot",
            "request_sha256",
            "queued_at",
        }
        if not isinstance(entry, dict) or frozenset(entry) not in {
            frozenset(legacy_keys),
            frozenset({*legacy_keys, "issue_number"}),
        }:
            raise ConfigurationError("activity outbox entry is invalid")
        if "issue_number" not in entry:
            entry["issue_number"] = None
        request = validate_queue_request(entry["request"])
        if request["project_id"] != project_id:
            raise ConfigurationError("activity outbox request identity is invalid")
        request_id = str(request["request_id"])
        actor = ProviderActor(
            str(entry["actor_user_id"]), str(entry["actor_username_snapshot"])
        )
        operation = request["operation"]
        if request["kind"] == "activity":
            if not isinstance(operation, dict) or set(operation) != {
                "task_id", "stage", "branch", "note",
            }:
                raise ConfigurationError("activity outbox operation is invalid")
            parse_activity_event(
                {
                    "schema_version": 1,
                    "event_id": request_id,
                    "project_id": project_id,
                    "task_id": operation["task_id"],
                    "actor": {
                        "provider": "github",
                        "user_id": actor.user_id,
                        "username_snapshot": actor.username_snapshot,
                    },
                    "source": "local_aria",
                    "stage": operation["stage"],
                    "branch": operation["branch"],
                    "pr_number": None,
                    "note": operation["note"],
                    "observed_at": request["submitted_at"],
                }
            )
        elif request["kind"] == "backlog":
            if not isinstance(operation, dict) or set(operation) != {
                "action", "item_id", "payload",
            }:
                raise ConfigurationError("backlog outbox operation is invalid")
            parse_backlog_request(
                {
                    "schema_version": 1,
                    "request_id": request_id,
                    "correlation_id": f"correlation-{request_id}",
                    "project_id": project_id,
                    **operation,
                    "requested_at": request["submitted_at"],
                }
            )
        _stamp(entry["queued_at"], "activity outbox queued_at")
        encoded = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if (
            request_id in request_ids
            or entry["request_sha256"] != hashlib.sha256(encoded).hexdigest()
            or not isinstance(entry["actor_user_id"], str)
            or not isinstance(entry["actor_username_snapshot"], str)
            or not isinstance(entry["queued_at"], str)
            or (
                entry["issue_number"] is not None
                and (type(entry["issue_number"]) is not int or entry["issue_number"] <= 0)
            )
        ):
            raise ConfigurationError("activity outbox entry proof is invalid")
        request_ids.add(request_id)
    return value


def _load_outbox(path: Path, project_id: str) -> dict[str, object]:
    return (
        _validate_outbox(_load_json(path, "activity outbox"), project_id)
        if path.is_file()
        else _template(project_id)
    )


def _load_terminal(path: Path, project_id: str) -> dict[str, object]:
    if not path.is_file():
        return {"schema_version": 1, "project_id": project_id, "requests": []}
    value = _load_json(path, "activity outbox terminal store")
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "project_id", "requests"}
        or value.get("schema_version") != 1
        or value.get("project_id") != project_id
        or not isinstance(value.get("requests"), list)
        or len(value["requests"]) > MAX_OUTBOX_REQUESTS
    ):
        raise ConfigurationError("activity outbox terminal store is invalid")
    ids: set[str] = set()
    for entry in value["requests"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {
                "request_id", "request_sha256", "issue_number",
                "issue_id", "body_sha256", "recorded_at",
            }
            or not isinstance(entry.get("request_id"), str)
            or not isinstance(entry.get("request_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["request_sha256"]) is None
            or type(entry.get("issue_number")) is not int
            or entry["issue_number"] <= 0
            or type(entry.get("issue_id")) is not int
            or entry["issue_id"] <= 0
            or not isinstance(entry.get("body_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", entry["body_sha256"]) is None
            or entry["request_id"] in ids
        ):
            raise ConfigurationError("activity outbox terminal receipt is invalid")
        _stamp(entry.get("recorded_at"), "activity outbox terminal recorded_at")
        ids.add(entry["request_id"])
    return value


def _coordinator_integration_id(project: ProjectConfig) -> int:
    project_document = _yaml_mapping(project.docs_root / "PROJECT.yaml", "PROJECT.yaml")
    repository = project_document.get("repository")
    checks = repository.get("required_checks") if isinstance(repository, dict) else None
    matches = [
        row.get("app_id")
        for row in checks
        if isinstance(row, dict) and row.get("context") == "ARIA integration"
    ] if isinstance(checks, list) else []
    if len(matches) != 1 or type(matches[0]) is not int or matches[0] <= 0:
        raise ConfigurationError("PROJECT.yaml coordinator integration is invalid")
    return matches[0]


def _record_terminal_delivery(
    paths: ActivityOutboxPaths,
    project_id: str,
    entry: dict[str, object],
    issue,
) -> None:
    terminal = _load_terminal(paths.terminal, project_id)
    request_id = str(entry["request"]["request_id"])
    existing = next(
        (row for row in terminal["requests"] if row["request_id"] == request_id),
        None,
    )
    receipt = {
        "request_id": request_id,
        "request_sha256": entry["request_sha256"],
        "issue_number": issue.number,
        "issue_id": issue.issue_id,
        "body_sha256": issue.body_sha256,
        "recorded_at": _utc_now(),
    }
    if existing is not None:
        comparable = {key: value for key, value in existing.items() if key != "recorded_at"}
        if comparable != {key: value for key, value in receipt.items() if key != "recorded_at"}:
            raise WorkflowError("activity outbox terminal receipt identity changed")
        return
    requests = [*terminal["requests"], receipt]
    if len(requests) > MAX_OUTBOX_REQUESTS:
        raise WorkflowError("activity outbox terminal store reached its safety limit")
    atomic_write_bytes(
        paths.terminal,
        json_bytes({**terminal, "requests": requests}),
    )


def queue_offline_activity_request(
    project: ProjectConfig,
    *,
    request: dict[str, object],
    actor: ProviderIdentity,
    queued_at: str | None = None,
) -> dict[str, object]:
    validated = validate_queue_request(request)
    if validated["project_id"] != project.project_id:
        raise WorkflowError("offline request belongs to another project")
    encoded = json.dumps(
        validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    entry = {
        "request": validated,
        "actor_user_id": actor.actor.user_id,
        "actor_username_snapshot": actor.actor.username_snapshot,
        "request_sha256": hashlib.sha256(encoded).hexdigest(),
        "queued_at": queued_at or _utc_now(),
        "issue_number": None,
    }
    paths = activity_outbox_paths(project)
    with exclusive_lock(paths.lock):
        outbox = _load_outbox(paths.outbox, project.project_id)
        existing = next(
            (
                row
                for row in outbox["requests"]
                if row["request"]["request_id"] == validated["request_id"]
            ),
            None,
        )
        if existing is not None:
            if (
                existing["request_sha256"] != entry["request_sha256"]
                or existing["actor_user_id"] != entry["actor_user_id"]
                or existing["actor_username_snapshot"]
                != entry["actor_username_snapshot"]
            ):
                raise WorkflowError("offline activity request id is already in use")
            return {"queued": False, "reason": "already_queued", "pending": len(outbox["requests"])}
        if len(outbox["requests"]) >= MAX_OUTBOX_REQUESTS:
            raise WorkflowError("offline activity outbox reached its safety limit")
        updated = {**outbox, "requests": [*outbox["requests"], entry]}
        atomic_write_bytes(paths.outbox, json_bytes(_validate_outbox(updated, project.project_id)))
        return {"queued": True, "reason": None, "pending": len(updated["requests"])}


def flush_activity_outbox(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    queue: GitHubRequestQueue,
    maximum: int = 20,
) -> dict[str, object]:
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ConfigurationError("activity outbox flush limit is invalid")
    authorization = authorize_collaborative_actor(
        project, adapter=adapter, require_protection=False
    )
    cache_activity_identity(project, actor=authorization.actor)
    paths = activity_outbox_paths(project)
    delivered: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    with exclusive_lock(paths.lock):
        outbox = _load_outbox(paths.outbox, project.project_id)
        selected = list(outbox["requests"][:maximum])
        remaining = list(outbox["requests"])
        for entry in selected:
            if entry["actor_user_id"] != authorization.actor.actor.user_id:
                raise WorkflowError(
                    "offline activity request belongs to another GitHub session"
                )
            issue_number = entry["issue_number"]
            if issue_number is None:
                issue = queue.submit(
                    entry["request"], expected_actor=authorization.actor.actor
                )
                remaining[0] = {**entry, "issue_number": issue.number}
            else:
                issue = queue.read(issue_number)
                queue.verify_immutable(issue)
                if (
                    load_queue_request(issue.body) != entry["request"]
                    or issue.author.user_id != authorization.actor.actor.user_id
                ):
                    raise WorkflowError("offline activity delivery read-back mismatch")
            delivered.append(
                {
                    "request_id": entry["request"]["request_id"],
                    "issue_number": issue.number,
                    "issue_id": issue.issue_id,
                    "body_sha256": issue.body_sha256,
                }
            )
            integration_id = _coordinator_integration_id(project)
            receipt_commit = queue.acceptance_receipt(
                issue,
                entry["request"],
                coordinator_integration_id=integration_id,
            )
            applied = receipt_commit is not None
            rejected_receipt = (
                queue.rejection_receipt(
                    issue,
                    entry["request"],
                    coordinator_integration_id=integration_id,
                )
                if issue.state == "closed" and issue.state_reason == "not_planned"
                else None
            )
            if applied:
                remaining.pop(0)
            elif rejected_receipt is not None:
                _record_terminal_delivery(
                    paths, project.project_id, entry, issue
                )
                rejected.append(
                    {
                        "request_id": entry["request"]["request_id"],
                        "issue_number": issue.number,
                    }
                )
                remaining.pop(0)
            atomic_write_bytes(
                paths.outbox,
                json_bytes(
                    _validate_outbox(
                        {**outbox, "requests": remaining}, project.project_id
                    )
                ),
            )
            outbox = {**outbox, "requests": remaining}
            if not applied and rejected_receipt is None:
                break
        return {
            "ok": True,
            "project": project.project_id,
            "selected": len(selected),
            "delivered": delivered,
            "rejected": rejected,
            "pending": len(remaining),
            "sync_status": (
                "pending_sync" if remaining else "rejected" if rejected else "accepted"
            ),
        }


def activity_outbox_status(
    project: ProjectConfig,
) -> dict[str, object]:
    paths = activity_outbox_paths(project)
    with exclusive_lock(paths.lock):
        outbox = _load_outbox(paths.outbox, project.project_id)
        terminal = _load_terminal(paths.terminal, project.project_id)
    return {
        "ok": True,
        "project": project.project_id,
        "identity_cached": paths.identity.is_file(),
        "pending": len(outbox["requests"]),
        "terminal": len(terminal["requests"]),
        "sync_status": (
            "pending_sync" if outbox["requests"] else
            "rejected" if terminal["requests"] else "accepted"
        ),
        "request_ids": [entry["request"]["request_id"] for entry in outbox["requests"]],
    }


def _queue_backlog_request(request: dict[str, object]) -> dict[str, object]:
    operation = request["operation"]
    if not isinstance(operation, dict) or set(operation) != {"action", "item_id", "payload"}:
        raise ConfigurationError("queued backlog operation is invalid")
    return parse_backlog_request(
        {
            "schema_version": 1,
            "request_id": request["request_id"],
            "correlation_id": f"correlation-{request['request_id']}",
            "project_id": request["project_id"],
            "action": operation["action"],
            "item_id": operation["item_id"],
            "payload": operation["payload"],
            "requested_at": request["submitted_at"],
        }
    )


def _queue_activity_event(
    request: dict[str, object], expected_actor: ProviderActor
) -> dict[str, object]:
    operation = request["operation"]
    if not isinstance(operation, dict) or set(operation) != {"task_id", "stage", "branch", "note"}:
        raise ConfigurationError("queued activity operation is invalid")
    return parse_activity_event(
        {
            "schema_version": 1,
            "event_id": request["request_id"],
            "project_id": request["project_id"],
            "task_id": operation["task_id"],
            "actor": {
                "provider": "github",
                "user_id": expected_actor.user_id,
                "username_snapshot": expected_actor.username_snapshot,
            },
            "source": "local_aria",
            "stage": operation["stage"],
            "branch": operation["branch"],
            "pr_number": None,
            "note": operation["note"],
            "observed_at": request["submitted_at"],
        }
    )


def queue_request_matches_projection(
    project: ProjectConfig,
    request_value: object,
    *,
    expected_actor: ProviderActor,
    projection_value: object,
    allow_legacy_activity_receipt: bool = False,
) -> bool:
    """Match a queue request to its complete durable control proof."""
    request = validate_queue_request(request_value)
    actor = ProviderIdentity("github", expected_actor)
    if request["kind"] == "backlog":
        backlog = validate_collaborative_backlog(projection_value)
        internal = _queue_backlog_request(request)
        event = next(
            (
                row
                for row in backlog["events"]
                if row["request_id"] == request["request_id"]
            ),
            None,
        )
        if event is None:
            return False
        requested_by = event["requested_by"]
        return (
            event["project_id"] == request["project_id"]
            and event["sequence"] == int(request["expected_revision"]) + 1
            and event["correlation_id"] == internal["correlation_id"]
            and event["action"] == internal["action"]
            and event["requested_at"] == internal["requested_at"]
            and requested_by["provider"] == actor.provider
            and requested_by["user_id"] == actor.actor.user_id
            and event["request_fingerprint"]
            == backlog_request_fingerprint(internal, actor)
        )
    activity = validate_activity_snapshot(projection_value)
    event = _queue_activity_event(request, expected_actor)
    receipt = find_activity_receipt(
        control_root=project.docs_root,
        runtime_root=machine_runtime_root(project),
        project_id=project.project_id,
        request_id=str(request["request_id"]),
    )
    if receipt is None:
        return False
    expected_fingerprint = activity_request_fingerprint(
        event,
        request_id=str(request["request_id"]),
        correlation_id=f"correlation-{request['request_id']}",
        expected_revision=int(request["expected_revision"]),
        authorized_source="local_aria",
        authorized_provider="github",
        authorized_actor=expected_actor,
    )
    common_proof = (
        request["request_id"] in activity["recent_event_ids"]
        and receipt["correlation_id"] == f"correlation-{request['request_id']}"
        and receipt["event_id"] == request["request_id"]
        and receipt["applied"] is True
        and receipt["reason"] is None
        and receipt["revision"] == int(request["expected_revision"]) + 1
        and receipt["revision"] <= activity["revision"]
    )
    if not common_proof:
        return False
    if receipt.get("request_fingerprint") is not None:
        return receipt["request_fingerprint"] == expected_fingerprint
    if not allow_legacy_activity_receipt:
        return False
    active_entry = next(
        (
            entry
            for entry in activity["active_work"]
            if entry["last_event_id"] == request["request_id"]
        ),
        None,
    )
    if active_entry is None:
        return False
    actor = active_entry["actor"]
    if not (
        active_entry["task_id"] == event["task_id"]
        and actor["provider"] == "github"
        and actor["user_id"] == expected_actor.user_id
        and active_entry["source"] == "local_aria"
        and active_entry["stage"] == event["stage"]
        and active_entry["branch"] == event["branch"]
        and active_entry["pr_number"] is None
        and active_entry["note"] == event["note"]
        and active_entry["observed_at"] == event["observed_at"]
    ):
        return False
    legacy_event = {
        **event,
        "actor": {
            "provider": actor["provider"],
            "user_id": actor["user_id"],
            "username_snapshot": actor["username_snapshot"],
        },
    }
    return receipt["event_fingerprint"] == activity_event_fingerprint(
        legacy_event,
        authorized_source="local_aria",
        authorized_provider="github",
        authorized_actor=expected_actor,
    )


def queue_request_applied(
    project: ProjectConfig,
    request_value: object,
    *,
    expected_actor: ProviderActor | None = None,
) -> bool:
    """Return proof present in a clean, remote-verified control projection."""
    request = validate_queue_request(request_value)
    try:
        contract = load_control_contract(project.docs_root / "CONTROL.yaml")
        if any(
            path.exists()
            for path in (
                project.runtime_root / "control-sync" / "pending.json",
                project.runtime_root / "control-sync" / "transaction.json",
            )
        ):
            return False
        status = subprocess.run(
            ["git", "-C", str(project.docs_root), "status", "--porcelain=v1"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        local = subprocess.run(
            ["git", "-C", str(project.docs_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        remote = subprocess.run(
            [
                "git", "-C", str(project.docs_root), "rev-parse",
                f"refs/remotes/{contract.remote}/{contract.control_branch}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return False
    if (
        status.returncode != 0
        or status.stdout.strip()
        or local.returncode != 0
        or remote.returncode != 0
        or local.stdout.strip() != remote.stdout.strip()
    ):
        return False
    if expected_actor is None:
        return False
    try:
        projection = (
            load_collaborative_backlog(project.docs_root / "BACKLOG.yaml")
            if request["kind"] == "backlog"
            else load_activity(project.docs_root / "ACTIVITY.yaml")
        )
        return queue_request_matches_projection(
            project,
            request,
            expected_actor=expected_actor,
            projection_value=projection,
        )
    except (ConfigurationError, WorkflowError):
        return False
