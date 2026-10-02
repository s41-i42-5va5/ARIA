from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from aria.access import access_template, load_access_policy
from aria.backlog import backlog_template, load_backlog
from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes, atomic_write_json, exclusive_lock
from aria.migration_1_4 import _open_runs
from aria.project import safe_relative_path
from aria.team import load_team
from aria.trust import load_trust_policy


ARIA_1_5_CURRENT = "1.5.5"


def _migration_journal(project: object) -> Path:
    return (
        Path(project.runtime_root)
        / "migrations"
        / f"upgrade-1.5-{project.project_id}.json"
    )


def _recover_pending_migration(project: object, journal_path: Path) -> bool:
    if not journal_path.is_file():
        return False
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError("ARIA 1.5 migration recovery journal is unreadable") from error
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != 1
        or journal.get("project_id") != project.project_id
        or journal.get("phase") not in {"prepared", "committed"}
        or not isinstance(journal.get("targets"), list)
    ):
        raise WorkflowError("ARIA 1.5 migration recovery journal is invalid")
    if journal["phase"] == "committed":
        journal_path.unlink()
        return False
    docs_root = Path(project.docs_root).resolve(strict=True)
    project_path = Path(project.project_path).resolve(strict=False)
    restored: list[tuple[Path, bytes | None]] = []
    seen: set[Path] = set()
    try:
        for row in journal["targets"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("path"), str)
                or not isinstance(row.get("existed"), bool)
                or (
                    row["existed"]
                    and not isinstance(row.get("content_base64"), str)
                )
            ):
                raise WorkflowError("ARIA 1.5 migration recovery target is invalid")
            path = Path(row["path"])
            if not path.is_absolute():
                raise WorkflowError("ARIA 1.5 migration recovery path is not absolute")
            resolved = path.resolve(strict=False)
            if (
                resolved in seen
                or (
                    resolved != project_path
                    and not resolved.is_relative_to(docs_root)
                )
            ):
                raise WorkflowError("ARIA 1.5 migration recovery path escapes project docs")
            seen.add(resolved)
            content = (
                base64.b64decode(row["content_base64"], validate=True)
                if row["existed"]
                else None
            )
            restored.append((resolved, content))
    except (ValueError, TypeError) as error:
        raise WorkflowError("ARIA 1.5 migration recovery preimage is invalid") from error
    for path, content in restored:
        if content is None:
            if path.exists():
                path.unlink()
        else:
            atomic_write_bytes(path, content)
    for path, content in restored:
        if content is None:
            if path.exists():
                raise WorkflowError("ARIA 1.5 migration recovery delete read-back failed")
        elif not path.is_file() or path.read_bytes() != content:
            raise WorkflowError("ARIA 1.5 migration recovery write read-back failed")
    journal_path.unlink()
    return True


def team_template_1_5(project_id: str) -> bytes:
    return yaml.safe_dump(
        {
            "schema_version": 2,
            "project_id": project_id,
            "actors": [
                {
                    "id": "local-owner",
                    "display_name": "Local project owner",
                    "type": "human",
                    "roles": [
                        "contributor",
                        "reviewer",
                        "maintainer",
                        "release-manager",
                    ],
                },
                {
                    "id": "github-actions",
                    "display_name": "GitHub Actions",
                    "type": "service",
                    "roles": ["ci"],
                },
            ],
        },
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def trust_template_1_5() -> bytes:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "keys": [],
            "policies": {
                "access": {
                    "minimum_trust_level": "signed",
                    "trusted_keys": [],
                    "required_assurance_classes": [],
                    "allowed_actor_roles": ["maintainer", "release-manager"],
                    "required_approvals": 0,
                },
                "contributor": {
                    "minimum_trust_level": "signed",
                    "trusted_keys": [],
                    "allowed_actor_roles": ["contributor", "maintainer"],
                },
                "job": {
                    "minimum_trust_level": "signed",
                    "trusted_keys": [],
                },
                "ci": {
                    "minimum_trust_level": "ci-signed",
                    "trusted_keys": [],
                    "allowed_actor_roles": ["ci"],
                },
                "reviewer": {
                    "minimum_trust_level": "signed",
                    "trusted_keys": [],
                    "allowed_actor_roles": ["reviewer", "maintainer"],
                },
                "release": {
                    "minimum_trust_level": "ci-signed",
                    "trusted_keys": [],
                    "allowed_actor_roles": ["ci", "release-manager"],
                    "required_approvals": 1,
                },
            },
        },
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def _read_yaml(path: Path, label: str) -> dict[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"{label} is unreadable: {path}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must be a mapping")
    return raw


def _team_v2(
    project_id: str, raw: dict[str, object]
) -> dict[str, object]:
    if raw.get("schema_version") == 2:
        if raw.get("project_id") != project_id:
            raise ConfigurationError("ARIA_TEAM.yaml project identity mismatch")
        return raw
    if raw.get("schema_version") != 1 or not isinstance(raw.get("actors"), list):
        raise ConfigurationError("ARIA 1.4 team document cannot be migrated")
    unknown = set(raw) - {"schema_version", "actors"}
    if unknown:
        raise ConfigurationError(
            f"ARIA_TEAM.yaml contains unknown fields: {sorted(unknown)}"
        )
    return {
        "schema_version": 2,
        "project_id": project_id,
        "actors": raw["actors"],
    }


def _trust_v1_5(raw: dict[str, object]) -> dict[str, object]:
    if (
        raw.get("schema_version") != 1
        or not isinstance(raw.get("keys"), list)
        or not isinstance(raw.get("policies"), dict)
    ):
        raise ConfigurationError("ARIA 1.4 trust document cannot be migrated")
    policies = raw["policies"]
    if "access" not in policies:
        policies["access"] = {
            "minimum_trust_level": "signed",
            "trusted_keys": [],
            "required_assurance_classes": [],
            "allowed_actor_roles": ["maintainer", "release-manager"],
            "required_approvals": 0,
        }
    return raw


def _proxy(project: object, *, access: str, backlog: str) -> object:
    values = dict(vars(project))
    current_files = getattr(project, "files", None)
    values["files"] = SimpleNamespace(
        team=getattr(current_files, "team", "ARIA_TEAM.yaml"),
        trust=getattr(current_files, "trust", "TRUST.yaml"),
        access=access,
        backlog=backlog,
    )
    return SimpleNamespace(**values)


def _governance_contract(project: object, *, backlog: str) -> dict[str, object]:
    contract: dict[str, object] = {
        "status_authority": backlog,
        "require_active_run_for_writes": True,
    }
    code_root = getattr(project, "code_root", None)
    if isinstance(code_root, Path) and (
        code_root / "docs" / "decisions" / "DECISIONS.md"
    ).is_file():
        contract["decision_registry"] = "docs/decisions/DECISIONS.md"
    return contract


def upgrade_project_to_1_5(project: object) -> dict[str, object]:
    lock_path = Path(project.runtime_root) / "locks" / "migration-1.5.lock"
    with exclusive_lock(lock_path):
        journal_path = _migration_journal(project)
        _recover_pending_migration(project, journal_path)
        project_path = Path(project.project_path)
        raw = _read_yaml(project_path, "PROJECT.yaml")
        if (
            raw.get("schema_version") != 1
            or raw.get("project_id") != project.project_id
        ):
            raise ConfigurationError("PROJECT.yaml identity or schema is invalid")
        version = raw.get("framework_version")
        if version not in {
            "1.4.0",
            "1.5.0",
            "1.5.1",
            "1.5.2",
            "1.5.3",
            ARIA_1_5_CURRENT,
        }:
            raise WorkflowError(
                f"Automatic 1.5 migration supports ARIA 1.4/1.5 projects, got {version!r}"
            )
        documents = raw.get("documents")
        if not isinstance(documents, dict):
            raise ConfigurationError("PROJECT.yaml documents must be a mapping")
        for key in ("team", "trust"):
            if not isinstance(documents.get(key), str):
                raise ConfigurationError(
                    f"ARIA project must preserve the {key} document pointer"
                )
        access_relative = safe_relative_path(
            str(documents.get("access", "ACCESS.yaml"))
        )
        backlog_relative = safe_relative_path(
            str(documents.get("backlog", "BACKLOG.yaml"))
        )
        access_path = Path(project.docs_root) / access_relative
        backlog_path = Path(project.docs_root) / backlog_relative
        team_path = Path(project.docs_root) / safe_relative_path(str(documents["team"]))
        trust_path = Path(project.docs_root) / safe_relative_path(str(documents["trust"]))
        migration_targets = {
            project_path.resolve(strict=False),
            team_path.resolve(strict=False),
            trust_path.resolve(strict=False),
            access_path.resolve(strict=False),
            backlog_path.resolve(strict=False),
        }
        if len(migration_targets) != 5:
            raise ConfigurationError(
                "ARIA 1.5 migration document pointers must be distinct"
            )
        proxy = _proxy(project, access=access_relative, backlog=backlog_relative)
        if version in {
            "1.5.0",
            "1.5.1",
            "1.5.2",
            "1.5.3",
            ARIA_1_5_CURRENT,
        }:
            load_team(proxy)
            load_trust_policy(trust_path)
            load_access_policy(proxy)
            load_backlog(proxy)
            governance = _governance_contract(project, backlog=backlog_relative)
            if (
                version == ARIA_1_5_CURRENT
                and raw.get("governance") == governance
            ):
                return {
                    "ok": True,
                    "project_id": project.project_id,
                    "from_version": version,
                    "to_version": version,
                    "idempotent": True,
                    "access_path": str(access_path.resolve()),
                    "backlog_path": str(backlog_path.resolve()),
                    "next_action": "Project already has executable ARIA 1.5 governance.",
                }
            open_runs = _open_runs(project)
            if open_runs:
                raise WorkflowError(
                    "Close or reconcile open ARIA runs before enabling governance: "
                    f"{open_runs}"
                )
            before_project = project_path.read_bytes()
            raw["governance"] = governance
            raw["framework_version"] = ARIA_1_5_CURRENT
            project_after = yaml.safe_dump(
                raw, allow_unicode=True, sort_keys=False
            ).encode("utf-8")
            atomic_write_json(
                journal_path,
                {
                    "schema_version": 1,
                    "project_id": project.project_id,
                    "phase": "prepared",
                    "targets": [
                        {
                            "path": str(project_path.resolve(strict=False)),
                            "existed": True,
                            "content_base64": base64.b64encode(before_project).decode(
                                "ascii"
                            ),
                        }
                    ],
                },
            )
            try:
                atomic_write_bytes(project_path, project_after)
                written = _read_yaml(project_path, "PROJECT.yaml")
                if (
                    written.get("governance") != governance
                    or written.get("framework_version") != ARIA_1_5_CURRENT
                ):
                    raise WorkflowError("ARIA governance migration read-back failed")
            except BaseException:
                atomic_write_bytes(project_path, before_project)
                if journal_path.exists():
                    journal_path.unlink()
                raise
            atomic_write_json(
                journal_path,
                {
                    "schema_version": 1,
                    "project_id": project.project_id,
                    "phase": "committed",
                    "targets": [],
                },
            )
            journal_path.unlink()
            return {
                "ok": True,
                "project_id": project.project_id,
                "from_version": version,
                "to_version": ARIA_1_5_CURRENT,
                "idempotent": False,
                "access_path": str(access_path.resolve()),
                "backlog_path": str(backlog_path.resolve()),
                "next_action": (
                    "Run governance check and reconcile any unmanaged worktree or "
                    "accepted-decision drift before the next build."
                ),
            }
        open_runs = _open_runs(project)
        if open_runs:
            raise WorkflowError(
                f"Close or discard open ARIA 1.4 runs before migration: {open_runs}"
            )
        load_team(project)
        load_trust_policy(trust_path)
        team_after = yaml.safe_dump(
            _team_v2(project.project_id, _read_yaml(team_path, "ARIA_TEAM.yaml")),
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        trust_after = yaml.safe_dump(
            _trust_v1_5(_read_yaml(trust_path, "TRUST.yaml")),
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        if access_path.exists():
            access_after = access_path.read_bytes()
        else:
            access_after = access_template(project.project_id)
        if backlog_path.exists():
            backlog_after = backlog_path.read_bytes()
        else:
            backlog_after = backlog_template(project.project_id)
        documents["access"] = access_relative
        documents["backlog"] = backlog_relative
        raw["governance"] = _governance_contract(
            project, backlog=backlog_relative
        )
        raw["framework_version"] = ARIA_1_5_CURRENT
        project_after = yaml.safe_dump(
            raw, allow_unicode=True, sort_keys=False
        ).encode("utf-8")
        targets = {
            team_path: team_after,
            trust_path: trust_after,
            access_path: access_after,
            backlog_path: backlog_after,
            project_path: project_after,
        }
        before = {
            path: path.read_bytes() if path.is_file() else None for path in targets
        }
        atomic_write_json(
            journal_path,
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "phase": "prepared",
                "targets": [
                    {
                        "path": str(path.resolve(strict=False)),
                        "existed": content is not None,
                        "content_base64": (
                            base64.b64encode(content).decode("ascii")
                            if content is not None
                            else None
                        ),
                    }
                    for path, content in before.items()
                ],
            },
        )
        try:
            for path, content in targets.items():
                atomic_write_bytes(path, content)
            load_team(proxy)
            load_trust_policy(trust_path)
            load_access_policy(proxy)
            load_backlog(proxy)
            written_project = _read_yaml(project_path, "PROJECT.yaml")
            if written_project.get("framework_version") != ARIA_1_5_CURRENT:
                raise WorkflowError("ARIA 1.5 migration read-back failed")
        except BaseException:
            for path, content in before.items():
                if content is None:
                    if path.exists():
                        path.unlink()
                else:
                    atomic_write_bytes(path, content)
            if journal_path.exists():
                journal_path.unlink()
            raise
        atomic_write_json(
            journal_path,
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "phase": "committed",
                "targets": [],
            },
        )
        journal_path.unlink()
        return {
            "ok": True,
            "project_id": project.project_id,
            "from_version": version,
            "to_version": ARIA_1_5_CURRENT,
            "idempotent": False,
            "access_path": str(access_path.resolve()),
            "backlog_path": str(backlog_path.resolve()),
            "next_action": (
                "Enroll the local owner identity, bootstrap ACCESS.yaml, then run "
                "project doctor and canary."
            ),
        }
