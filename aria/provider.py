from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from aria.errors import ConfigurationError


PROVIDER_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
USERNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9])?")
ROLE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def _bounded_text(value: str, label: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ConfigurationError(f"{label} must be a safe non-empty trimmed string")
    return value


@dataclass(frozen=True)
class ProviderActor:
    user_id: str
    username_snapshot: str
    display_name_snapshot: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.user_id, "provider actor user_id")
        if (
            not isinstance(self.username_snapshot, str)
            or USERNAME_RE.fullmatch(self.username_snapshot) is None
        ):
            raise ConfigurationError("provider username snapshot is invalid")
        if self.display_name_snapshot is not None:
            _bounded_text(
                self.display_name_snapshot,
                "provider display name snapshot",
            )

    def as_mapping(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "username_snapshot": self.username_snapshot,
            "display_name_snapshot": self.display_name_snapshot,
        }


@dataclass(frozen=True)
class ProviderMembership:
    active: bool
    roles: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.active) is not bool:
            raise ConfigurationError("provider membership active must be boolean")
        if not isinstance(self.roles, tuple) or any(
            not isinstance(role, str) or ROLE_RE.fullmatch(role) is None
            for role in self.roles
        ):
            raise ConfigurationError("provider membership contains an invalid role")
        if tuple(sorted(set(self.roles))) != self.roles:
            raise ConfigurationError(
                "provider membership roles must be unique and sorted"
            )
        if self.active and not self.roles:
            raise ConfigurationError("active provider membership requires a role")
        if not self.active and self.roles:
            raise ConfigurationError("inactive provider membership cannot grant roles")

    def as_mapping(self) -> dict[str, object]:
        return {"active": self.active, "roles": list(self.roles)}


@dataclass(frozen=True)
class ProviderBranchProtection:
    protected: bool
    direct_user_push: bool
    canonical_writer: str | None

    def __post_init__(self) -> None:
        if type(self.protected) is not bool or type(self.direct_user_push) is not bool:
            raise ConfigurationError("provider branch protection flags must be boolean")
        if self.canonical_writer is not None:
            _bounded_text(
                self.canonical_writer,
                "provider canonical writer",
                maximum=128,
            )

    @property
    def coordinator_only(self) -> bool:
        return (
            self.protected
            and not self.direct_user_push
            and self.canonical_writer == "aria-coordinator"
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "protected": self.protected,
            "direct_user_push": self.direct_user_push,
            "canonical_writer": self.canonical_writer,
            "coordinator_only": self.coordinator_only,
        }


@dataclass(frozen=True)
class ProviderInspection:
    provider: str
    repository_id: str
    actor: ProviderActor
    membership: ProviderMembership
    protection: ProviderBranchProtection

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or PROVIDER_ID_RE.fullmatch(self.provider) is None
        ):
            raise ConfigurationError("provider inspection adapter id is invalid")
        _bounded_text(self.repository_id, "provider repository_id")
        if not isinstance(self.actor, ProviderActor):
            raise ConfigurationError("provider inspection actor is invalid")
        if not isinstance(self.membership, ProviderMembership):
            raise ConfigurationError("provider inspection membership is invalid")
        if not isinstance(self.protection, ProviderBranchProtection):
            raise ConfigurationError("provider inspection protection is invalid")

    def as_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "repository_id": self.repository_id,
            "actor": self.actor.as_mapping(),
            "membership": self.membership.as_mapping(),
            "protection": self.protection.as_mapping(),
        }


@dataclass(frozen=True)
class ProviderTeamMember:
    provider: str
    actor: ProviderActor
    membership: ProviderMembership

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or PROVIDER_ID_RE.fullmatch(self.provider) is None
        ):
            raise ConfigurationError("provider team member adapter id is invalid")
        if not isinstance(self.actor, ProviderActor):
            raise ConfigurationError("provider team member actor is invalid")
        if not isinstance(self.membership, ProviderMembership):
            raise ConfigurationError("provider team member membership is invalid")
        if not self.membership.active:
            raise ConfigurationError("provider team listing cannot assert inactive membership")

    def as_mapping(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "actor": self.actor.as_mapping(),
            "membership": self.membership.as_mapping(),
        }


class ProviderAdapter(Protocol):
    """Read-only provider boundary. Implementations own sessions and never expose tokens."""

    provider_id: str

    def inspect_collaboration(
        self,
        *,
        repository_id: str,
        control_branch: str,
    ) -> ProviderInspection: ...


class ProviderTeamAdapter(Protocol):
    provider_id: str

    def list_collaborators(self, *, repository_id: str) -> tuple[ProviderTeamMember, ...]: ...


def validate_provider_inspection(
    inspection: ProviderInspection,
    *,
    expected_provider: str,
    expected_repository_id: str,
) -> None:
    if not isinstance(inspection, ProviderInspection):
        raise ConfigurationError("Provider adapter returned invalid read-back")
    if inspection.provider != expected_provider:
        raise ConfigurationError(
            "Provider inspection does not match the requested adapter"
        )
    if inspection.repository_id != expected_repository_id:
        raise ConfigurationError(
            "Provider inspection does not match immutable repository_id"
        )
