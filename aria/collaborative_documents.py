from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from aria import __version__
from aria.activity import activity_template, dump_activity
from aria.collaboration import ControlContract, dump_control_contract
from aria.collaborative_backlog import (
    collaborative_backlog_template,
    dump_collaborative_backlog,
)
from aria.collaborative_team import (
    collaborative_team_template,
    dump_collaborative_team,
)
from aria.collaborative_state import (
    collaborative_state_template,
    validate_collaborative_state,
)
from aria.errors import ConfigurationError
from aria.io import atomic_write_bytes
from aria.github_control import CONTROL_DOCUMENTS
from aria.project import PROJECT_ID_RE


ROLE_PERMISSIONS = {
    "admin": [
        "activity.write",
        "backlog.add",
        "backlog.assign",
        "backlog.block",
        "backlog.claim",
        "backlog.complete",
        "backlog.cancel",
        "backlog.amend_scope",
        "backlog.recover",
        "backlog.triage",
        "state.read",
        "state.write",
        "team.sync",
    ],
    "maintainer": [
        "activity.write",
        "backlog.add",
        "backlog.assign",
        "backlog.block",
        "backlog.claim",
        "backlog.complete",
        "state.read",
    ],
    "contributor": [
        "activity.write",
        "backlog.add",
        "backlog.block",
        "backlog.claim",
        "backlog.complete",
        "state.read",
    ],
    "viewer": ["state.read"],
}

TRIAGE_ROLE_PERMISSIONS = {
    role: [
        permission
        for permission in permissions
        if permission not in {"backlog.cancel", "backlog.amend_scope", "backlog.recover"}
    ]
    for role, permissions in ROLE_PERMISSIONS.items()
}

LEGACY_ROLE_PERMISSIONS = {
    role: [permission for permission in permissions if permission != "backlog.triage"]
    for role, permissions in TRIAGE_ROLE_PERMISSIONS.items()
}


@dataclass(frozen=True)
class CollaborativeDocumentSet:
    project_id: str
    documents: dict[str, str]

    def __post_init__(self) -> None:
        if PROJECT_ID_RE.fullmatch(self.project_id) is None:
            raise ConfigurationError("collaborative document project id is invalid")
        if set(self.documents) != CONTROL_DOCUMENTS:
            raise ConfigurationError("collaborative initial document set is incomplete")
        if any(not isinstance(value, str) or not value for value in self.documents.values()):
            raise ConfigurationError("collaborative initial document content is invalid")


def _yaml(value: object) -> str:
    content = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    if yaml.safe_load(content) != value:
        raise ConfigurationError("collaborative document round-trip failed")
    return content


def collaborative_access_template(contract: ControlContract) -> dict[str, object]:
    return {
        "schema_version": 2,
        "project_id": contract.project_id,
        "provider": contract.provider,
        "repository_id": contract.repository_id,
        "revision": 0,
        "role_permissions": ROLE_PERMISSIONS,
    }


def validate_collaborative_access(
    value: object, contract: ControlContract
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "project_id",
        "provider",
        "repository_id",
        "revision",
        "role_permissions",
    }:
        raise ConfigurationError("collaborative ACCESS.yaml schema is invalid")
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != 2
        or value.get("project_id") != contract.project_id
        or value.get("provider") != contract.provider
        or value.get("repository_id") != contract.repository_id
        or type(value.get("revision")) is not int
        or value["revision"] < 0
        or value.get("role_permissions")
        not in {
            "current": ROLE_PERMISSIONS,
            "triage": TRIAGE_ROLE_PERMISSIONS,
            "legacy": LEGACY_ROLE_PERMISSIONS,
        }.values()
    ):
        raise ConfigurationError("collaborative ACCESS.yaml policy is invalid")
    return value


def upgrade_legacy_collaborative_documents(
    root: Path,
    *,
    contract: ControlContract,
    coordinator_integration_id: int,
) -> dict[str, bool]:
    """Upgrade the two legacy policy documents during an owner recovery action."""
    documents, changed = plan_legacy_collaborative_upgrade(
        root,
        contract=contract,
        coordinator_integration_id=coordinator_integration_id,
    )
    for name in ("ACCESS.yaml", "PROJECT.yaml"):
        key = "access" if name == "ACCESS.yaml" else "project"
        if changed[key]:
            atomic_write_bytes(root / name, documents[name].encode("utf-8"))
    return changed


def plan_legacy_collaborative_upgrade(
    root: Path,
    *,
    contract: ControlContract,
    coordinator_integration_id: int,
) -> tuple[dict[str, str], dict[str, bool]]:
    """Build a complete target snapshot without mutating the control worktree."""
    try:
        documents = {
            name: (root / name).read_text(encoding="utf-8")
            for name in CONTROL_DOCUMENTS
        }
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError("legacy collaborative documents are unreadable") from error
    access_path = root / "ACCESS.yaml"
    project_path = root / "PROJECT.yaml"
    try:
        access = yaml.safe_load(access_path.read_text(encoding="utf-8"))
        project = yaml.safe_load(project_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError("legacy collaborative documents are unreadable") from error
    access = validate_collaborative_access(access, contract)
    if not isinstance(project, dict):
        raise ConfigurationError("legacy PROJECT.yaml is invalid")
    access_changed = access["role_permissions"] != ROLE_PERMISSIONS
    expected_repository = _project_template(
        contract,
        str(project.get("display_name") or contract.project_id),
        coordinator_integration_id=coordinator_integration_id,
    )["repository"]
    repository = project.get("repository")
    if repository is not None:
        if not isinstance(repository, dict) or any(
            repository.get(key) != value
            for key, value in expected_repository.items()
            if key != "required_checks"
        ):
            raise ConfigurationError("legacy PROJECT.yaml repository identity is invalid")
    project_changed = (
        repository is None
        or repository.get("required_checks") != expected_repository["required_checks"]
    )
    if access_changed:
        access = {**access, "role_permissions": ROLE_PERMISSIONS}
        validate_collaborative_access(access, contract)
        documents["ACCESS.yaml"] = _yaml(access)
    if project_changed:
        project = {
            **project,
            "repository": (
                expected_repository
                if repository is None
                else {**repository, "required_checks": expected_repository["required_checks"]}
            ),
        }
        documents["PROJECT.yaml"] = _yaml(project)
    return documents, {"access": access_changed, "project": project_changed}


def _project_template(
    contract: ControlContract,
    display_name: str,
    *,
    coordinator_integration_id: int | None = None,
) -> dict[str, object]:
    if not isinstance(display_name, str) or not display_name.strip():
        raise ConfigurationError("collaborative project display name is invalid")
    repository = {
        "provider": contract.provider,
        "repository_id": contract.repository_id,
        "remote": contract.remote,
        "main_branch": "main",
        "integration_branch": contract.integration_branch,
        "working_branch_template": "work/{github_username}",
        "control_branch": contract.control_branch,
    }
    if coordinator_integration_id is not None:
        if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
            raise ConfigurationError("coordinator integration id is invalid")
        repository["required_checks"] = [
            {"context": "ARIA integration", "app_id": coordinator_integration_id}
        ]
    return {
        "schema_version": 1,
        "project_id": contract.project_id,
        "display_name": display_name.strip(),
        "framework_version": __version__,
        "collaboration_mode": "collaborative",
        "repository": repository,
        "state_profile": "frontier",
        "documents": {
            "state": "STATE.yaml",
            "stack": "SYSTEM_MAP.yaml",
            "history": "HISTORY.jsonl",
            "system_map": "SYSTEM_MAP.yaml",
            "specs": "CONTROL.yaml",
            "adr": "CONTROL.yaml",
            "knowledge": "CONTROL.yaml",
            "team": "ARIA_TEAM.yaml",
            "trust": "ACCESS.yaml",
            "access": "ACCESS.yaml",
            "backlog": "BACKLOG.yaml",
        },
        "governance": {
            "status_authority": "BACKLOG.yaml",
            "require_active_run_for_writes": True,
        },
        "context": {
            "default_budget_bytes": 131072,
            "state_budget_bytes": 32768,
            "state_projection_budget_bytes": 32768,
            "stack_manifests": ["CONTROL.yaml"],
            "git_ignore_prefixes": [],
        },
    }


def build_initial_collaborative_documents(
    contract: ControlContract,
    *,
    display_name: str,
    coordinator_integration_id: int | None = None,
) -> CollaborativeDocumentSet:
    if not isinstance(contract, ControlContract):
        raise ConfigurationError("collaborative control contract is invalid")
    system_map = {
        "schema_version": 1,
        "project_id": contract.project_id,
        "generated_from": {
            "authority": "accepted-integration-state",
            "branch": contract.integration_branch,
            "git_head": None,
        },
        "dimensions": {
            "layers": [],
            "domains": [],
            "runtime_surfaces": [],
            "cross_cutting": [],
        },
        "components": [],
        "shared_primitives": [],
        "critical_flows": [],
        "unknowns": ["No integration commit has been accepted by coordinator"],
    }
    access = collaborative_access_template(contract)
    state = collaborative_state_template(contract)
    validate_collaborative_access(access, contract)
    validate_collaborative_state(state, contract)
    documents = {
        "CONTROL.yaml": dump_control_contract(contract),
        "PROJECT.yaml": _yaml(
            _project_template(
                contract,
                display_name,
                coordinator_integration_id=coordinator_integration_id,
            )
        ),
        "BACKLOG.yaml": dump_collaborative_backlog(
            collaborative_backlog_template(contract.project_id)
        ),
        "ACTIVITY.yaml": dump_activity(activity_template(contract.project_id)),
        "STATE.yaml": _yaml(state),
        "HISTORY.jsonl": "\n",
        "ARIA_TEAM.yaml": dump_collaborative_team(
            collaborative_team_template(
                contract.project_id,
                provider=contract.provider,
                repository_id=contract.repository_id,
            )
        ),
        "ACCESS.yaml": _yaml(access),
        "SYSTEM_MAP.yaml": _yaml(system_map),
    }
    result = CollaborativeDocumentSet(contract.project_id, documents)
    # Ensure no document accidentally contains a serialized secret-bearing object.
    json.dumps(result.documents, ensure_ascii=False)
    return result
