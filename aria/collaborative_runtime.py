from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from aria.collaboration import ControlContract, load_control_contract
from aria.collaborative_backlog import ProviderIdentity
from aria.collaborative_documents import validate_collaborative_access
from aria.collaborative_team import active_team_identities, load_collaborative_team
from aria.errors import ConfigurationError, WorkflowError
from aria.project import ProjectConfig
from aria.provider import ProviderInspection, validate_provider_inspection


class AuthenticatedCollaborativeAdapter(Protocol):
    provider_id: str

    def inspect_collaboration(
        self, *, repository_id: str, control_branch: str
    ) -> ProviderInspection: ...


@dataclass(frozen=True)
class CollaborativeAuthorization:
    contract: ControlContract
    actor: ProviderIdentity
    members: tuple[ProviderIdentity, ...]
    permissions: frozenset[str]


def machine_runtime_root(project: ProjectConfig) -> Path:
    expected = project.runtime_root.parent / project.project_id
    if expected != project.runtime_root or project.runtime_root.parent.name != "projects":
        raise ConfigurationError("project runtime layout is invalid")
    return project.runtime_root.parent.parent


def _yaml_mapping(path: Path, label: str) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot read {label}: {path}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a mapping")
    return value


def authorize_collaborative_actor(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    require_protection: bool,
) -> CollaborativeAuthorization:
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    if contract.project_id != project.project_id:
        raise WorkflowError("CONTROL.yaml belongs to another project")
    if adapter.provider_id != contract.provider:
        raise WorkflowError("authenticated adapter does not match CONTROL.yaml")
    inspection = adapter.inspect_collaboration(
        repository_id=contract.repository_id,
        control_branch=contract.control_branch,
    )
    validate_provider_inspection(
        inspection,
        expected_provider=contract.provider,
        expected_repository_id=contract.repository_id,
    )
    if not inspection.membership.active:
        raise WorkflowError("authenticated user is not an active project member")
    if require_protection and not inspection.protection.coordinator_only:
        raise WorkflowError("mutation requires coordinator-only control branch protection")
    team = load_collaborative_team(project.docs_root / "ARIA_TEAM.yaml")
    if (
        team["project_id"] != project.project_id
        or team["provider"] != contract.provider
        or team["repository_id"] != contract.repository_id
    ):
        raise WorkflowError("ARIA_TEAM.yaml belongs to another collaborative project")
    members = active_team_identities(team)
    actor = ProviderIdentity(inspection.provider, inspection.actor)
    if actor.key not in {member.key for member in members}:
        raise WorkflowError("authenticated user is absent from the active team projection")
    members = tuple(actor if member.key == actor.key else member for member in members)
    access = validate_collaborative_access(
        _yaml_mapping(project.docs_root / "ACCESS.yaml", "ACCESS.yaml"), contract
    )
    role_permissions = access["role_permissions"]
    if not isinstance(role_permissions, dict):
        raise ConfigurationError("ACCESS.yaml role permissions are invalid")
    permissions: set[str] = set()
    for role in inspection.membership.roles:
        granted = role_permissions.get(role)
        if not isinstance(granted, list) or not all(
            isinstance(permission, str) for permission in granted
        ):
            raise WorkflowError(f"provider role is absent from ACCESS.yaml: {role}")
        permissions.update(granted)
    return CollaborativeAuthorization(
        contract=contract,
        actor=actor,
        members=members,
        permissions=frozenset(permissions),
    )
