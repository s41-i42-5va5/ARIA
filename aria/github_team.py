from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

from aria.errors import ConfigurationError, WorkflowError
from aria.github import GitHubApiError, GitHubMutationTransport, GitHubRepository, OWNER_RE
from aria.provider import ProviderActor


@dataclass(frozen=True)
class GitHubInvitationReceipt:
    invitation_id: str | None
    actor: ProviderActor
    state: str
    permission: str

    def __post_init__(self) -> None:
        if self.state not in {"invited", "active"}:
            raise ConfigurationError("GitHub invitation state is invalid")
        if self.permission not in {"pull", "triage", "push", "maintain", "admin"}:
            raise ConfigurationError("GitHub invitation permission is invalid")
        if self.state == "invited" and (
            not isinstance(self.invitation_id, str) or not self.invitation_id.isdecimal()
        ):
            raise ConfigurationError("GitHub invitation id is invalid")
        if self.state == "active" and self.invitation_id is not None:
            raise ConfigurationError("active GitHub collaborator cannot retain invitation id")


class GitHubCollaboratorManager:
    def __init__(
        self, *, transport: GitHubMutationTransport, repository: GitHubRepository
    ) -> None:
        if not all(
            hasattr(transport, method)
            for method in ("get_json", "put_json", "delete_json")
        ):
            raise ConfigurationError("GitHub collaborator transport is invalid")
        self._transport = transport
        self._repository = repository

    @staticmethod
    def _mapping(value: object, label: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ConfigurationError(f"GitHub {label} response is invalid")
        return value

    @staticmethod
    def _actor(value: object, label: str) -> ProviderActor:
        raw = GitHubCollaboratorManager._mapping(value, label)
        user_id = raw.get("id")
        login = raw.get("login")
        name = raw.get("name")
        if type(user_id) is not int or user_id <= 0 or not isinstance(login, str):
            raise ConfigurationError(f"GitHub {label} identity is invalid")
        return ProviderActor(
            str(user_id),
            login,
            name.strip() if isinstance(name, str) and name.strip() else None,
        )

    def resolve_user(self, username: str) -> ProviderActor:
        if not isinstance(username, str) or OWNER_RE.fullmatch(username) is None:
            raise ConfigurationError("GitHub username is invalid")
        encoded = urllib.parse.quote(username, safe="")
        actor = self._actor(self._transport.get_json(f"/users/{encoded}"), "user")
        if actor.username_snapshot.casefold() != username.casefold():
            raise WorkflowError("GitHub username read-back mismatch")
        return actor

    def invite(self, *, username: str, permission: str = "push") -> GitHubInvitationReceipt:
        if permission not in {"pull", "triage", "push", "maintain", "admin"}:
            raise ConfigurationError("GitHub collaborator permission is invalid")
        actor = self.resolve_user(username)
        desired_permission = {
            "pull": "read",
            "triage": "triage",
            "push": "write",
            "maintain": "maintain",
            "admin": "admin",
        }[permission]
        encoded = urllib.parse.quote(actor.username_snapshot, safe="")
        result = self._mapping(
            self._transport.put_json(
                f"{self._repository.api_path}/collaborators/{encoded}",
                {"permission": permission},
            ),
            "collaborator invitation",
        )
        if not result:
            permission_readback = self._mapping(
                self._transport.get_json(
                    f"{self._repository.api_path}/collaborators/{encoded}/permission"
                ),
                "collaborator permission",
            )
            user = permission_readback.get("user")
            verified = self._actor(user, "collaborator permission user")
            if (
                verified.user_id != actor.user_id
                or permission_readback.get("permission") != desired_permission
            ):
                raise WorkflowError("GitHub active collaborator read-back mismatch")
            return GitHubInvitationReceipt(None, actor, "active", permission)
        invitation_id = result.get("id")
        invitee = self._actor(result.get("invitee"), "invitation invitee")
        if type(invitation_id) is not int or invitation_id <= 0 or invitee.user_id != actor.user_id:
            raise WorkflowError("GitHub invitation response does not match requested user")
        invitations = self._transport.get_json(
            f"{self._repository.api_path}/invitations?per_page=100&page=1"
        )
        if not isinstance(invitations, list):
            raise ConfigurationError("GitHub invitation read-back is invalid")
        match = next(
            (
                row for row in invitations
                if isinstance(row, dict)
                and row.get("id") == invitation_id
                and isinstance(row.get("invitee"), dict)
                and row["invitee"].get("id") == int(actor.user_id)
            ),
            None,
        )
        if match is None:
            raise WorkflowError("GitHub invitation was not found by read-back")
        if match.get("permissions", match.get("permission")) != desired_permission:
            raise WorkflowError("GitHub invitation permission read-back mismatch")
        return GitHubInvitationReceipt(str(invitation_id), actor, "invited", permission)

    def read_invitation(
        self, *, username: str, permission: str
    ) -> GitHubInvitationReceipt | None:
        if permission not in {"pull", "triage", "push", "maintain", "admin"}:
            raise ConfigurationError("GitHub collaborator permission is invalid")
        permission_readback = {
            "pull": "read",
            "triage": "triage",
            "push": "write",
            "maintain": "maintain",
            "admin": "admin",
        }[permission]
        actor = self.resolve_user(username)
        encoded = urllib.parse.quote(actor.username_snapshot, safe="")
        try:
            active = self._mapping(self._transport.get_json(
                f"{self._repository.api_path}/collaborators/{encoded}/permission"
            ), "collaborator permission recovery")
        except GitHubApiError as error:
            if error.status != 404:
                raise
        else:
            verified = self._actor(
                active.get("user"), "collaborator permission recovery user"
            )
            if (
                verified.user_id != actor.user_id
                or verified.username_snapshot.casefold()
                != actor.username_snapshot.casefold()
                or active.get("permission") != permission_readback
            ):
                return None
            return GitHubInvitationReceipt(None, actor, "active", permission)
        for page in range(1, 11):
            invitations = self._transport.get_json(
                f"{self._repository.api_path}/invitations?per_page=100&page={page}"
            )
            if not isinstance(invitations, list):
                raise ConfigurationError("GitHub invitation recovery read-back is invalid")
            for value in invitations:
                row = self._mapping(value, "invitation recovery")
                invitee = row.get("invitee")
                if isinstance(invitee, dict) and invitee.get("id") == int(actor.user_id):
                    observed_permission = row.get("permissions", row.get("permission"))
                    if observed_permission != permission_readback:
                        return None
                    invitation_id = row.get("id")
                    if type(invitation_id) is not int or invitation_id <= 0:
                        raise ConfigurationError("GitHub invitation recovery id is invalid")
                    return GitHubInvitationReceipt(
                        str(invitation_id), actor, "invited", permission
                    )
            if len(invitations) < 100:
                return None
        raise WorkflowError("GitHub invitation recovery exceeds pagination limit")

    def revoke(self, *, username: str) -> ProviderActor:
        actor = self.resolve_user(username)
        encoded = urllib.parse.quote(actor.username_snapshot, safe="")
        self._transport.delete_json(
            f"{self._repository.api_path}/collaborators/{encoded}"
        )
        try:
            self._transport.get_json(
                f"{self._repository.api_path}/collaborators/{encoded}/permission"
            )
        except GitHubApiError as error:
            if error.status == 404:
                return actor
            raise
        raise WorkflowError("GitHub collaborator revocation read-back still grants access")

    def read_revocation(self, *, username: str) -> ProviderActor | None:
        actor = self.resolve_user(username)
        encoded = urllib.parse.quote(actor.username_snapshot, safe="")
        try:
            self._transport.get_json(
                f"{self._repository.api_path}/collaborators/{encoded}/permission"
            )
        except GitHubApiError as error:
            if error.status != 404:
                raise
        else:
            return None
        for page in range(1, 11):
            invitations = self._transport.get_json(
                f"{self._repository.api_path}/invitations?per_page=100&page={page}"
            )
            if not isinstance(invitations, list):
                raise ConfigurationError("GitHub revocation recovery read-back is invalid")
            if any(
                isinstance(row, dict)
                and isinstance(row.get("invitee"), dict)
                and row["invitee"].get("id") == int(actor.user_id)
                for row in invitations
            ):
                return None
            if len(invitations) < 100:
                return actor
        raise WorkflowError("GitHub revocation recovery exceeds pagination limit")
