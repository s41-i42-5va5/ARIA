from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes, exclusive_lock
from aria.project import safe_relative_path
from aria.team import load_team
from aria.trust import load_trust_policy


def _team_template() -> bytes:
    return yaml.safe_dump(
        {
            "schema_version": 1,
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


def _trust_template() -> bytes:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "keys": [],
            "policies": {
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


def _open_runs(project: object) -> list[str]:
    runs_root = Path(project.runtime_root) / "runs"
    open_ids: list[str] = []
    if not runs_root.is_dir():
        return open_ids
    for path in runs_root.glob("*/manifest.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowError(
                f"Cannot classify pre-migration run manifest: {path}"
            ) from error
        if isinstance(raw, dict) and raw.get("status") in {
            "started",
            "awaiting_user_approval",
        }:
            open_ids.append(path.parent.name)
    return sorted(open_ids)


def upgrade_project_to_1_4(project: object) -> dict[str, object]:
    lock_path = Path(project.runtime_root) / "locks" / "migration-1.4.lock"
    with exclusive_lock(lock_path):
        path = Path(project.project_path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
            raise ConfigurationError(f"PROJECT.yaml is unreadable: {path}") from error
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != 1
            or raw.get("project_id") != project.project_id
        ):
            raise ConfigurationError("PROJECT.yaml identity or schema is invalid")
        version = raw.get("framework_version")
        if version not in {"1.3.0", "1.4.0"}:
            raise WorkflowError(
                f"Automatic 1.4 migration supports only ARIA 1.3 projects, got {version!r}"
            )
        if version == "1.3.0":
            open_runs = _open_runs(project)
            if open_runs:
                raise WorkflowError(
                    f"Close or discard open pre-1.4 runs before migration: {open_runs}"
                )
        documents = raw.get("documents")
        if not isinstance(documents, dict):
            raise ConfigurationError("PROJECT.yaml documents must be a mapping")
        if version == "1.4.0":
            team_relative = documents.get("team")
            trust_relative = documents.get("trust")
            if not isinstance(team_relative, str) or not isinstance(
                trust_relative, str
            ):
                raise ConfigurationError(
                    "ARIA 1.4 project must preserve team and trust document pointers"
                )
            team_path = Path(project.docs_root) / safe_relative_path(
                team_relative
            )
            trust_path = Path(project.docs_root) / safe_relative_path(
                trust_relative
            )
            proxy_values = dict(vars(project))
            proxy_values["files"] = SimpleNamespace(
                team=safe_relative_path(team_relative)
            )
            proxy = SimpleNamespace(**proxy_values)
            load_team(proxy)
            load_trust_policy(trust_path)
            return {
                "ok": True,
                "project_id": project.project_id,
                "from_version": version,
                "to_version": "1.4.0",
                "team_path": str(team_path.resolve()),
                "trust_path": str(trust_path.resolve()),
                "idempotent": True,
                "next_action": (
                    "Project is already on ARIA 1.4; configured team and trust "
                    "pointers were preserved."
                ),
            }
        team_path = Path(project.docs_root) / "ARIA_TEAM.yaml"
        trust_path = Path(project.docs_root) / "TRUST.yaml"
        if not team_path.exists():
            atomic_write_bytes(team_path, _team_template())
        if not trust_path.exists():
            atomic_write_bytes(trust_path, _trust_template())
        load_team(project)
        load_trust_policy(trust_path)
        documents["team"] = "ARIA_TEAM.yaml"
        documents["trust"] = "TRUST.yaml"
        raw["framework_version"] = "1.4.0"
        atomic_write_bytes(
            path,
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8"),
        )
        return {
            "ok": True,
            "project_id": project.project_id,
            "from_version": version,
            "to_version": "1.4.0",
            "team_path": str(team_path.resolve()),
            "trust_path": str(trust_path.resolve()),
            "idempotent": version == "1.4.0",
            "next_action": (
                "Replace placeholder actors, add trusted public keys, then run project "
                "doctor and canary before active work."
            ),
        }
