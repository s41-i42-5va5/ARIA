from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aria import __version__
from aria.backlog import load_backlog
from aria.collaboration import (
    build_control_contract,
    collaboration_plan,
)
from aria.collaboration_apply import ControlWriter, apply_collaboration_transaction
from aria.collaborative_backlog import (
    ProviderIdentity,
    dump_collaborative_backlog,
    next_available_item_number,
    validate_collaborative_backlog,
)
from aria.collaborative_documents import (
    CollaborativeDocumentSet,
    build_initial_collaborative_documents,
)
from aria.collaborative_team import (
    collaborative_team_template,
    dump_collaborative_team,
    sync_collaborative_team,
)
from aria.coordinator_scheduler import coordinator_schedule_status
from aria.errors import ConfigurationError, ProviderAdapterError, WorkflowError
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import (
    PROJECT_ID_RE,
    default_runtime_root,
    load_project,
    run_project_doctor,
    verify_history,
)
from aria.provider import ProviderActor, ProviderTeamMember
from aria.registry import read_registry, register_project, registry_path


MIGRATION_SCHEMA_VERSION = 1
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_TREE_BYTES = 256 * 1024 * 1024
PRIORITY_MAP = {"critical": "P0", "high": "P1", "normal": "P2", "low": "P3"}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ACTOR_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
USER_ID_RE = re.compile(r"[1-9][0-9]{0,31}")
REPARSE_POINT = 0x400


@dataclass(frozen=True)
class MigrationPaths:
    root: Path
    journal: Path
    receipt: Path
    backup: Path
    lock: Path


def migration_paths(
    *, runtime_root: Path, project_id: str, plan_sha256: str
) -> MigrationPaths:
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError("collaborative migration project id is invalid")
    if SHA256_RE.fullmatch(plan_sha256) is None:
        raise ConfigurationError("collaborative migration plan SHA-256 is invalid")
    root = runtime_root / "collaboration-migrations" / project_id
    return MigrationPaths(
        root=root,
        journal=root / "pending.json",
        receipt=root / "receipt.json",
        backup=root / "backups" / plan_sha256,
        lock=runtime_root / "locks" / f"collaboration-migration-{project_id}.lock",
    )


def _canonical_sha(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_actor_mappings(values: list[str] | tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    used_user_ids: set[str] = set()
    for value in values:
        if not isinstance(value, str) or value.count("=") != 1:
            raise ConfigurationError("actor mapping must use LEGACY_ACTOR=GITHUB_USER_ID")
        actor_id, user_id = value.split("=", 1)
        if ACTOR_ID_RE.fullmatch(actor_id) is None or USER_ID_RE.fullmatch(user_id) is None:
            raise ConfigurationError("actor mapping identity is invalid")
        if actor_id in result or user_id in used_user_ids:
            raise ConfigurationError("actor mappings must be one-to-one and unique")
        result[actor_id] = user_id
        used_user_ids.add(user_id)
    return result


def _is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as error:
        raise ConfigurationError(f"migration source cannot be inspected: {path}") from error
    return path.is_symlink() or bool(attributes & REPARSE_POINT)


def snapshot_tree(root: Path) -> dict[str, object]:
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(f"migration source is unreadable: {root}") from error
    if not resolved.is_dir() or _is_reparse(resolved):
        raise ConfigurationError("migration source must be a real directory")
    files: list[dict[str, object]] = []
    directories: list[str] = []
    total = 0
    for current, dir_names, file_names in os.walk(resolved, followlinks=False):
        current_path = Path(current)
        for name in sorted(dir_names):
            path = current_path / name
            if _is_reparse(path):
                raise ConfigurationError(f"migration source contains a reparse point: {path}")
            relative = path.relative_to(resolved).as_posix()
            directories.append(relative)
        for name in sorted(file_names):
            path = current_path / name
            if _is_reparse(path) or not path.is_file():
                raise ConfigurationError(f"migration source contains a special file: {path}")
            try:
                content = path.read_bytes()
            except OSError as error:
                raise ConfigurationError(f"migration source file is unreadable: {path}") from error
            if len(content) > MAX_SOURCE_FILE_BYTES:
                raise WorkflowError(f"migration source file exceeds safety limit: {path}")
            total += len(content)
            if total > MAX_SOURCE_TREE_BYTES:
                raise WorkflowError("migration source tree exceeds safety limit")
            files.append(
                {
                    "path": path.relative_to(resolved).as_posix(),
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    directories.sort()
    files.sort(key=lambda item: str(item["path"]))
    body = {"directories": directories, "files": files, "total_bytes": total}
    return {**body, "tree_sha256": _canonical_sha(body)}


def _copy_verified_backup(
    source: Path, destination: Path, expected_snapshot: dict[str, object]
) -> dict[str, object]:
    current = snapshot_tree(source)
    if current != expected_snapshot:
        raise WorkflowError("offline project changed after the migration plan was approved")
    if destination.exists():
        manifest_path = destination / "BACKUP_MANIFEST.json"
        if not manifest_path.is_file():
            raise WorkflowError("migration backup destination is occupied")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowError("migration backup manifest is unreadable") from error
        if manifest.get("source_snapshot") != expected_snapshot:
            raise WorkflowError("existing migration backup belongs to another source snapshot")
        if snapshot_tree(destination / "source") != expected_snapshot:
            raise WorkflowError("existing migration backup content is invalid")
        return manifest
    destination.mkdir(parents=True)
    copy_root = destination / "source"
    copy_root.mkdir()
    for relative in expected_snapshot["directories"]:
        (copy_root / str(relative)).mkdir(parents=True, exist_ok=True)
    for row in expected_snapshot["files"]:
        relative = str(row["path"])
        target = copy_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    copied = snapshot_tree(copy_root)
    if copied != expected_snapshot:
        raise WorkflowError("migration backup read-back does not match the offline source")
    manifest = {
        "schema_version": 1,
        "source_root": str(source.resolve(strict=True)),
        "source_snapshot": expected_snapshot,
    }
    atomic_write_bytes(destination / "BACKUP_MANIFEST.json", json_bytes(manifest))
    return manifest


def _identity(member: ProviderTeamMember) -> dict[str, object]:
    return ProviderIdentity(member.provider, member.actor).as_mapping()


def _legacy_description(item: dict[str, object]) -> str:
    value = {
        "acceptance": item["acceptance"],
        "legacy_type": item["type"],
        "refs": item["refs"],
        "requirements": item["requirements"],
        "source": item["source"],
        "target_versions": item["target_versions"],
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_migrated_backlog(
    *,
    project_id: str,
    legacy_backlog: dict[str, object],
    actor_mappings: dict[str, str],
    provider_members: tuple[ProviderTeamMember, ...],
    coordinator: ProviderIdentity,
    migrated_at: str,
    source_tree_sha256: str,
) -> dict[str, object]:
    members = {member.actor.user_id: member for member in provider_members}

    def mapped(actor_id: object) -> dict[str, object] | None:
        if actor_id is None:
            return None
        user_id = actor_mappings.get(str(actor_id))
        member = members.get(user_id or "")
        if member is None:
            raise WorkflowError(f"legacy actor has no active provider mapping: {actor_id}")
        return _identity(member)

    items: list[dict[str, object]] = []
    for legacy in legacy_backlog["items"]:
        item = dict(legacy)
        converted = {
            "id": item["id"],
            "title": item["title"],
            "description": _legacy_description(item),
            "priority": PRIORITY_MAP[str(item["priority"])],
            "status": item["status"],
            "source_id": item["source"]["id"],
            "dependencies": sorted(item["dependencies"]),
            "evidence_required": bool(item["completion_evidence"]) or item["status"] == "done",
            "evidence_refs": sorted(item["completion_evidence"]),
            "creator": mapped(item["creator"]),
            "assignee": mapped(item["assignee"]),
            "blocked_reason": item["blocked_reason"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
        }
        items.append(converted)
    items.sort(key=lambda item: str(item["id"]))
    events: list[dict[str, object]] = []
    prefix: list[dict[str, object]] = []
    previous: str | None = None
    for sequence, item in enumerate(items, start=1):
        prefix.append(item)
        request_id = f"migration-{source_tree_sha256[:20]}-{sequence}"
        event = {
            "schema_version": 1,
            "sequence": sequence,
            "project_id": project_id,
            "request_id": request_id,
            "correlation_id": f"migration-{source_tree_sha256[:32]}",
            "request_fingerprint": _canonical_sha(
                {"operation": "offline-import", "item": item, "source": source_tree_sha256}
            ),
            "action": "add",
            "item_ids": [item["id"]],
            "requested_at": item["created_at"],
            "committed_at": migrated_at,
            "requested_by": item["creator"],
            "committed_by": coordinator.as_mapping(),
            "items_sha256": _canonical_sha(prefix),
            "previous_event_sha256": previous,
        }
        event["event_sha256"] = _canonical_sha(event)
        previous = str(event["event_sha256"])
        events.append(event)
    result = {
        "schema_version": 2,
        "project_id": project_id,
        "revision": len(events),
        "next_item_number": next_available_item_number(items),
        "updated_at": migrated_at if events else None,
        "items": items,
        "events": events,
    }
    return validate_collaborative_backlog(result)


def build_migrated_documents(
    *,
    project_id: str,
    display_name: str,
    repository_id: str,
    integration_branch: str,
    control_branch: str,
    remote: str,
    legacy_backlog: dict[str, object],
    actor_mappings: dict[str, str],
    provider_members: tuple[ProviderTeamMember, ...],
    coordinator_integration_id: int,
    migrated_at: str,
    source_snapshot: dict[str, object],
) -> CollaborativeDocumentSet:
    contract = build_control_contract(
        project_id=project_id,
        provider="github",
        repository_id=repository_id,
        remote=remote,
        integration_branch=integration_branch,
        control_branch=control_branch,
    )
    base = build_initial_collaborative_documents(
        contract,
        display_name=display_name,
        coordinator_integration_id=coordinator_integration_id,
    )
    coordinator = ProviderIdentity(
        "github-app",
        ProviderActor(
            str(coordinator_integration_id),
            "aria-coordinator",
            "ARIA Coordinator",
        ),
    )
    backlog = build_migrated_backlog(
        project_id=project_id,
        legacy_backlog=legacy_backlog,
        actor_mappings=actor_mappings,
        provider_members=provider_members,
        coordinator=coordinator,
        migrated_at=migrated_at,
        source_tree_sha256=str(source_snapshot["tree_sha256"]),
    )
    team_result = sync_collaborative_team(
        collaborative_team_template(
            project_id, provider="github", repository_id=repository_id
        ),
        provider_members,
        sync_id=f"migration-team-{str(source_snapshot['tree_sha256'])[:20]}",
        coordinator=coordinator,
        expected_revision=0,
        checked_at=migrated_at,
    )
    system_map = yaml.safe_load(base.documents["SYSTEM_MAP.yaml"])
    system_map["unknowns"] = [
        *system_map["unknowns"],
        f"Legacy offline snapshot SHA-256: {source_snapshot['tree_sha256']}",
    ]
    documents = dict(base.documents)
    documents["BACKLOG.yaml"] = dump_collaborative_backlog(backlog)
    documents["ARIA_TEAM.yaml"] = dump_collaborative_team(team_result["team"])
    documents["SYSTEM_MAP.yaml"] = yaml.safe_dump(
        system_map, allow_unicode=True, sort_keys=False
    )
    return CollaborativeDocumentSet(project_id, documents)


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _validate_actor_mapping_dict(value: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(actor_id, str)
        or ACTOR_ID_RE.fullmatch(actor_id) is None
        or not isinstance(user_id, str)
        or USER_ID_RE.fullmatch(user_id) is None
        for actor_id, user_id in value.items()
    ):
        raise ConfigurationError("actor mappings are invalid")
    if len(set(value.values())) != len(value):
        raise ConfigurationError("actor mappings must be one-to-one")
    return dict(sorted(value.items()))


def _member_snapshot(members: tuple[ProviderTeamMember, ...]) -> list[dict[str, object]]:
    return [member.as_mapping() for member in sorted(members, key=lambda row: row.actor.user_id)]


def _members_from_snapshot(values: list[dict[str, object]]) -> tuple[ProviderTeamMember, ...]:
    from aria.provider import ProviderMembership

    members: list[ProviderTeamMember] = []
    for value in values:
        actor = value["actor"]
        membership = value["membership"]
        members.append(
            ProviderTeamMember(
                provider=str(value["provider"]),
                actor=ProviderActor(
                    str(actor["user_id"]),
                    str(actor["username_snapshot"]),
                    actor.get("display_name_snapshot"),
                ),
                membership=ProviderMembership(
                    active=membership["active"], roles=tuple(membership["roles"])
                ),
            )
        )
    return tuple(members)


def collaborative_migration_plan(
    *,
    project_id: str,
    provider: str,
    repository_id: str,
    actor_mappings: dict[str, str],
    coordinator_integration_id: int,
    provider_adapter: object,
    framework_root: Path | None = None,
    runtime_root: Path | None = None,
    docs_root: Path | None = None,
    remote: str = "origin",
    integration_branch: str = "dev",
    control_branch: str = "aria-control",
) -> dict[str, object]:
    if provider != "github":
        raise ConfigurationError("collaborative migration currently supports GitHub only")
    if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
        raise ConfigurationError("coordinator integration id must be positive")
    actor_mappings = _validate_actor_mapping_dict(actor_mappings)
    project = load_project(
        project_id, framework_root=framework_root, runtime_root=runtime_root
    )
    blockers: list[dict[str, str]] = []
    if project.collaboration_mode != "offline":
        blockers.append(_blocker("NOT_OFFLINE", "project is already collaborative"))
    if project.framework_version != "1.5.5" or __version__ != "1.5.5":
        blockers.append(
            _blocker("VERSION_MISMATCH", "migration requires project and ARIA 1.5.5")
        )
    doctor = run_project_doctor(project)
    if doctor.get("ok") is not True:
        blockers.append(_blocker("OFFLINE_DOCTOR_FAILED", "offline project doctor failed"))
    history = verify_history(project)
    if history.get("ok") is not True:
        blockers.append(_blocker("HISTORY_INVALID", "offline HISTORY.jsonl is invalid"))
    legacy_backlog = load_backlog(project)
    source_snapshot = snapshot_tree(project.docs_root)
    open_runs: list[str] = []
    runs_root = project.runtime_root / "runs"
    if runs_root.is_dir():
        for path in sorted(runs_root.glob("*/manifest.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ConfigurationError(f"run manifest is unreadable: {path}") from error
            if isinstance(manifest, dict) and manifest.get("status") in {
                "started",
                "awaiting_user_approval",
            }:
                open_runs.append(path.parent.name)
    if open_runs:
        blockers.append(_blocker("OPEN_RUNS", f"offline project has open runs: {open_runs}"))
    base_plan = collaboration_plan(
        project_id=project_id,
        code_root=project.code_root,
        docs_root=docs_root,
        provider=provider,
        repository_id=repository_id,
        remote=remote,
        integration_branch=integration_branch,
        control_branch=control_branch,
        provider_adapter=provider_adapter,
    )
    blockers.extend(base_plan["blockers"])
    members: tuple[ProviderTeamMember, ...] = ()
    listing = getattr(provider_adapter, "list_collaborators", None)
    if not callable(listing):
        blockers.append(_blocker("TEAM_READBACK_UNAVAILABLE", "provider cannot list collaborators"))
    else:
        try:
            members = listing(repository_id=repository_id)
        except ProviderAdapterError as error:
            blockers.append(_blocker("TEAM_READBACK_UNAVAILABLE", str(error)))
    if not isinstance(members, tuple) or any(
        not isinstance(member, ProviderTeamMember) for member in members
    ):
        raise ConfigurationError("provider collaborator snapshot is invalid")
    member_ids = {member.actor.user_id for member in members}
    readback = base_plan.get("provider_readback")
    readback_actor = readback.get("actor") if isinstance(readback, dict) else None
    authenticated_user_id = (
        str(readback_actor.get("user_id")) if isinstance(readback_actor, dict) else None
    )
    if authenticated_user_id is not None and authenticated_user_id not in member_ids:
        blockers.append(
            _blocker(
                "AUTHENTICATED_MEMBER_ABSENT",
                "authenticated provider user is absent from collaborator read-back",
            )
        )
    legacy_actors = {
        str(actor)
        for item in legacy_backlog["items"]
        for actor in (item["creator"], item["assignee"])
        if actor is not None
    }
    missing = sorted(legacy_actors - set(actor_mappings))
    unknown = sorted(set(actor_mappings.values()) - member_ids)
    extra = sorted(set(actor_mappings) - legacy_actors)
    if missing:
        blockers.append(_blocker("ACTOR_MAPPING_MISSING", f"legacy actors are unmapped: {missing}"))
    if unknown:
        blockers.append(_blocker("ACTOR_MAPPING_INACTIVE", f"mapped GitHub ids are not active: {unknown}"))
    if extra:
        blockers.append(_blocker("ACTOR_MAPPING_UNUSED", f"actor mappings are unused: {extra}"))
    if not missing and not unknown and not extra:
        try:
            build_migrated_documents(
                project_id=project_id,
                display_name=project.display_name,
                repository_id=repository_id,
                integration_branch=integration_branch,
                control_branch=control_branch,
                remote=remote,
                legacy_backlog=legacy_backlog,
                actor_mappings=actor_mappings,
                provider_members=members,
                coordinator_integration_id=coordinator_integration_id,
                migrated_at="2000-01-01T00:00:00Z",
                source_snapshot=source_snapshot,
            )
        except (ConfigurationError, WorkflowError) as error:
            blockers.append(_blocker("BACKLOG_INCOMPATIBLE", str(error)))
    registry = read_registry(project.registry_path)
    source_entry = registry["projects"][project_id]
    body: dict[str, object] = {
        "operation": "collaboration.migrate",
        "project_id": project_id,
        "provider": provider,
        "repository_id": repository_id,
        "coordinator_integration_id": coordinator_integration_id,
        "actor_mappings": dict(sorted(actor_mappings.items())),
        "offline": {
            "docs_root": str(project.docs_root.resolve(strict=True)),
            "code_root": str(project.code_root.resolve(strict=True)),
            "registry_entry": source_entry,
            "snapshot": source_snapshot,
            "history": history,
            "backlog_revision": legacy_backlog["revision"],
            "backlog_items": len(legacy_backlog["items"]),
            "open_runs": open_runs,
        },
        "provider_members": _member_snapshot(members),
        "collaboration_plan": {
            key: value for key, value in base_plan.items() if key != "plan_sha256"
        },
        "blockers": blockers,
    }
    return {
        "ok": True,
        "ready": not blockers,
        "read_only": True,
        "plan_sha256": _canonical_sha(body),
        **body,
    }


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"{label} is unreadable") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} is invalid")
    return value


def migrate_offline_project(
    *,
    project_id: str,
    provider: str,
    repository_id: str,
    actor_mappings: dict[str, str],
    coordinator_integration_id: int,
    expected_plan_sha256: str,
    confirm: bool,
    provider_adapter: object,
    control_writer: ControlWriter,
    framework_root: Path | None = None,
    runtime_root: Path | None = None,
    docs_root: Path | None = None,
    remote: str = "origin",
    integration_branch: str = "dev",
    control_branch: str = "aria-control",
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if not confirm:
        raise WorkflowError("collaborative migration requires explicit confirmation")
    if SHA256_RE.fullmatch(expected_plan_sha256) is None:
        raise ConfigurationError("expected migration plan SHA-256 is invalid")
    actor_mappings = _validate_actor_mapping_dict(actor_mappings)
    runtime = (runtime_root or default_runtime_root()).resolve(strict=False)
    paths = migration_paths(
        runtime_root=runtime,
        project_id=project_id,
        plan_sha256=expected_plan_sha256,
    )
    with exclusive_lock(paths.lock, timeout_seconds=120):
        rollback_path = paths.root / "rollback.json"
        if paths.receipt.is_file() and rollback_path.is_file() and not paths.journal.exists():
            receipt = _load_json(paths.receipt, "collaborative migration receipt")
            expected_arguments = {
                "plan_sha256": expected_plan_sha256,
                "repository_id": repository_id,
                "coordinator_integration_id": coordinator_integration_id,
                "actor_mappings": dict(sorted(actor_mappings.items())),
                "remote": remote,
                "integration_branch": integration_branch,
                "control_branch": control_branch,
            }
            if any(receipt.get(key) != value for key, value in expected_arguments.items()):
                raise WorkflowError("migration reactivation arguments do not match its receipt")
            source_root = Path(str(receipt["source_docs_root"]))
            if snapshot_tree(source_root) != receipt["source_snapshot"]:
                raise WorkflowError("offline source changed after rollback; build a new plan")
            target = Path(str(receipt["target_docs_root"]))
            commit = str(receipt["control_commit"])
            if (
                control_writer.read_head() != commit
                or _git_value(target, "rev-parse", "HEAD") != commit
                or _git_value(target, "status", "--porcelain=v1", "--untracked-files=all")
            ):
                raise WorkflowError("aria-control changed after rollback; reactivation is unsafe")
            inspection = provider_adapter.inspect_collaboration(
                repository_id=repository_id, control_branch=control_branch
            )
            if not inspection.protection.coordinator_only:
                raise WorkflowError("aria-control protection is no longer coordinator-only")
            registered = register_project(
                project_id,
                docs_root=target,
                code_root=Path(str(receipt["source_registry_entry"]["code_root"])),
                runtime_root=runtime,
            )
            reactivated = load_project(
                project_id, framework_root=framework_root, runtime_root=runtime
            )
            doctor = run_project_doctor(reactivated)
            if doctor.get("ok") is not True:
                register_project(
                    project_id,
                    docs_root=source_root,
                    code_root=Path(str(receipt["source_registry_entry"]["code_root"])),
                    runtime_root=runtime,
                )
                raise WorkflowError("reactivated collaborative project failed doctor")
            atomic_write_bytes(
                paths.root / "reactivation.json",
                json_bytes(
                    {
                        "schema_version": 1,
                        "project_id": project_id,
                        "reactivated_at": _utc_now(),
                        "plan_sha256": expected_plan_sha256,
                        "control_commit": commit,
                    }
                ),
            )
            rollback_path.unlink()
            return {
                **registered,
                "migration": "reactivated",
                "control_commit": commit,
                "backup_path": receipt["backup_path"],
                "recovered": False,
            }
        if paths.journal.is_file():
            journal = _load_json(paths.journal, "collaborative migration journal")
            if (
                journal.get("schema_version") != MIGRATION_SCHEMA_VERSION
                or journal.get("project_id") != project_id
                or journal.get("plan_sha256") != expected_plan_sha256
                or journal.get("repository_id") != repository_id
                or journal.get("coordinator_integration_id")
                != coordinator_integration_id
                or journal.get("actor_mappings") != actor_mappings
                or journal.get("remote") != remote
                or journal.get("integration_branch") != integration_branch
                or journal.get("control_branch") != control_branch
            ):
                raise WorkflowError("another collaborative migration is pending")
            recovered = True
        else:
            plan = collaborative_migration_plan(
                project_id=project_id,
                provider=provider,
                repository_id=repository_id,
                actor_mappings=actor_mappings,
                coordinator_integration_id=coordinator_integration_id,
                provider_adapter=provider_adapter,
                framework_root=framework_root,
                runtime_root=runtime,
                docs_root=docs_root,
                remote=remote,
                integration_branch=integration_branch,
                control_branch=control_branch,
            )
            if not hmac.compare_digest(str(plan["plan_sha256"]), expected_plan_sha256):
                raise WorkflowError("collaborative migration plan is stale")
            if plan["blockers"]:
                summary = "; ".join(
                    f"{item['code']}: {item['message']}" for item in plan["blockers"]
                )
                raise WorkflowError(f"collaborative migration blocked: {summary}")
            migrated_at = _utc_now()
            legacy_root = Path(str(plan["offline"]["docs_root"]))
            legacy_project = load_project(
                project_id, framework_root=framework_root, runtime_root=runtime
            )
            documents = build_migrated_documents(
                project_id=project_id,
                display_name=legacy_project.display_name,
                repository_id=repository_id,
                integration_branch=integration_branch,
                control_branch=control_branch,
                remote=remote,
                legacy_backlog=load_backlog(legacy_project),
                actor_mappings=actor_mappings,
                provider_members=_members_from_snapshot(plan["provider_members"]),
                coordinator_integration_id=coordinator_integration_id,
                migrated_at=migrated_at,
                source_snapshot=plan["offline"]["snapshot"],
            )
            backup_manifest = _copy_verified_backup(
                legacy_root, paths.backup, plan["offline"]["snapshot"]
            )
            journal = {
                "schema_version": MIGRATION_SCHEMA_VERSION,
                "project_id": project_id,
                "plan_sha256": expected_plan_sha256,
                "repository_id": repository_id,
                "coordinator_integration_id": coordinator_integration_id,
                "actor_mappings": dict(sorted(actor_mappings.items())),
                "remote": remote,
                "integration_branch": integration_branch,
                "control_branch": control_branch,
                "code_root": plan["offline"]["code_root"],
                "source_docs_root": plan["offline"]["docs_root"],
                "source_registry_entry": plan["offline"]["registry_entry"],
                "source_snapshot": plan["offline"]["snapshot"],
                "backup_path": str(paths.backup),
                "backup_manifest_sha256": _canonical_sha(backup_manifest),
                "target_docs_root": plan["collaboration_plan"]["docs_root"],
                "migrated_at": migrated_at,
                "documents": documents.documents,
            }
            atomic_write_bytes(paths.journal, json_bytes(journal))
            recovered = False
        source_root = Path(str(journal["source_docs_root"]))
        if snapshot_tree(source_root) != journal["source_snapshot"]:
            raise WorkflowError("offline source changed while migration recovery is pending")
        inspection = provider_adapter.inspect_collaboration(
            repository_id=repository_id, control_branch=control_branch
        )
        if not inspection.protection.coordinator_only:
            ensure = getattr(provider_adapter, "ensure_control_protection", None)
            if not callable(ensure):
                raise WorkflowError("provider cannot configure aria-control protection")
            ensure(repository_id=repository_id, control_branch=control_branch)
            inspection = provider_adapter.inspect_collaboration(
                repository_id=repository_id, control_branch=control_branch
            )
            if not inspection.protection.coordinator_only:
                raise WorkflowError("aria-control protection read-back failed")
        result = apply_collaboration_transaction(
            project_id=project_id,
            repository_id=repository_id,
            plan_sha256=expected_plan_sha256,
            code_root=Path(str(journal["code_root"])),
            docs_root=Path(str(journal["target_docs_root"])),
            remote=remote,
            control_branch=control_branch,
            document_set=CollaborativeDocumentSet(project_id, journal["documents"]),
            writer=control_writer,
            runtime_root=runtime,
            git_environment=git_environment,
        )
        receipt = {
            key: journal[key]
            for key in (
                "schema_version",
                "project_id",
                "plan_sha256",
                "repository_id",
                "coordinator_integration_id",
                "actor_mappings",
                "remote",
                "integration_branch",
                "control_branch",
                "source_docs_root",
                "source_registry_entry",
                "source_snapshot",
                "backup_path",
                "backup_manifest_sha256",
                "target_docs_root",
                "migrated_at",
            )
        }
        receipt["control_commit"] = result["control_commit"]
        atomic_write_bytes(paths.receipt, json_bytes(receipt))
        paths.journal.unlink()
        return {**result, "migration": "completed", "backup_path": str(paths.backup), "recovered": recovered or result["recovered"]}


def collaborative_migration_status(
    *, project_id: str, runtime_root: Path | None = None
) -> dict[str, object]:
    runtime = (runtime_root or default_runtime_root()).resolve(strict=False)
    root = runtime / "collaboration-migrations" / project_id
    journal = root / "pending.json"
    receipt = root / "receipt.json"
    registry = read_registry(registry_path(runtime))
    entry = registry.get("projects", {}).get(project_id)
    return {
        "ok": True,
        "project": project_id,
        "pending": journal.is_file(),
        "completed": receipt.is_file(),
        "registered_docs_root": entry.get("docs_root") if isinstance(entry, dict) else None,
        "receipt": _load_json(receipt, "collaborative migration receipt") if receipt.is_file() else None,
    }


def _git_value(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise WorkflowError("migration rollback Git inspection failed") from error
    if result.returncode != 0:
        raise WorkflowError("migration rollback Git inspection failed")
    return result.stdout.strip()


def rollback_collaborative_migration(
    *,
    project_id: str,
    expected_control_commit: str,
    confirm: bool,
    control_writer: ControlWriter,
    runtime_root: Path | None = None,
    scheduler: object | None = None,
) -> dict[str, object]:
    if not confirm:
        raise WorkflowError("collaborative migration rollback requires confirmation")
    if re.fullmatch(r"[0-9a-f]{40}", expected_control_commit) is None:
        raise ConfigurationError("expected control commit is invalid")
    runtime = (runtime_root or default_runtime_root()).resolve(strict=False)
    root = runtime / "collaboration-migrations" / project_id
    receipt_path = root / "receipt.json"
    if not receipt_path.is_file():
        raise WorkflowError("completed collaborative migration receipt was not found")
    receipt = _load_json(receipt_path, "collaborative migration receipt")
    if receipt.get("control_commit") != expected_control_commit:
        raise WorkflowError("rollback control commit confirmation does not match receipt")
    if control_writer.read_head() != expected_control_commit:
        raise WorkflowError("aria-control has changed since migration; rollback is unsafe")
    target = Path(str(receipt["target_docs_root"]))
    if _git_value(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise WorkflowError("aria-control worktree is dirty; rollback is unsafe")
    if _git_value(target, "rev-parse", "HEAD") != expected_control_commit:
        raise WorkflowError("local aria-control worktree is not at the migration commit")
    schedule = coordinator_schedule_status(
        project_id=project_id, runtime_root=runtime, scheduler=scheduler
    )
    if schedule.get("configured") or schedule.get("installed"):
        raise WorkflowError("remove the coordinator schedule before migration rollback")
    source = Path(str(receipt["source_docs_root"]))
    if snapshot_tree(source) != receipt["source_snapshot"]:
        raise WorkflowError("offline source no longer matches the migration snapshot")
    backup = Path(str(receipt["backup_path"]))
    manifest = _load_json(backup / "BACKUP_MANIFEST.json", "migration backup manifest")
    if (
        _canonical_sha(manifest) != receipt["backup_manifest_sha256"]
        or manifest.get("source_snapshot") != receipt["source_snapshot"]
        or snapshot_tree(backup / "source") != receipt["source_snapshot"]
    ):
        raise WorkflowError("migration backup proof is invalid")
    source_entry = receipt["source_registry_entry"]
    restored = register_project(
        project_id,
        docs_root=source,
        code_root=Path(str(source_entry["code_root"])),
        runtime_root=runtime,
    )
    rollback_receipt = {
        "schema_version": 1,
        "project_id": project_id,
        "rolled_back_at": _utc_now(),
        "control_commit": expected_control_commit,
        "restored_docs_root": str(source.resolve(strict=True)),
        "control_branch_preserved": True,
    }
    atomic_write_bytes(root / "rollback.json", json_bytes(rollback_receipt))
    return {
        **restored,
        "migration": "rolled_back",
        "control_branch_preserved": True,
        "reapply_plan_sha256": receipt["plan_sha256"],
    }
