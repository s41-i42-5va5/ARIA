from __future__ import annotations

from dataclasses import dataclass

from aria.errors import ConfigurationError, WorkflowError
from aria.github import (
    GitHubApiError,
    GitHubMutationTransport,
    GitHubRepository,
)
from aria.github_request_queue import QUEUE_KIND_LABELS, QUEUE_LABEL
from aria.provider import ProviderActor


@dataclass(frozen=True)
class ProvisionedGitHubRepository:
    repository: GitHubRepository
    repository_id: str
    clone_url: str
    private: bool
    default_branch: str = "main"


class GitHubRepositoryProvisioner:
    def __init__(self, *, transport: GitHubMutationTransport) -> None:
        if not all(
            hasattr(transport, method)
            for method in ("get_json", "post_json", "put_json", "patch_json")
        ):
            raise ConfigurationError("GitHub repository transport is invalid")
        self._transport = transport

    def ensure_integration_protection(
        self,
        *,
        repository: GitHubRepository,
        branch: str,
        coordinator_integration_id: int,
    ) -> dict[str, object]:
        if not isinstance(branch, str) or not branch:
            raise ConfigurationError("GitHub integration branch is invalid")
        if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
            raise ConfigurationError("GitHub coordinator App id is invalid")
        import urllib.parse

        encoded = urllib.parse.quote(branch, safe="")
        checks_path = (
            f"{repository.api_path}/branches/{encoded}/protection/required_status_checks"
        )
        protection_path = f"{repository.api_path}/branches/{encoded}/protection"
        aria_check = {
            "context": "ARIA integration",
            "app_id": coordinator_integration_id,
        }
        try:
            self._mapping(
                self._transport.get_json(protection_path), "existing branch protection"
            )
        except GitHubApiError as error:
            if error.status != 404:
                raise
            protected = False
        else:
            protected = True
        if not protected:
            self._transport.put_json(
                protection_path,
                {
                    "required_status_checks": {"strict": True, "checks": [aria_check]},
                    "enforce_admins": True,
                    "required_pull_request_reviews": {
                        "dismiss_stale_reviews": True,
                        "required_approving_review_count": 1,
                    },
                    "restrictions": None,
                    "allow_force_pushes": False,
                    "allow_deletions": False,
                },
            )
            expected_checks = [aria_check]
        else:
            try:
                current = self._mapping(
                    self._transport.get_json(checks_path),
                    "existing integration protection",
                )
            except GitHubApiError as error:
                if error.status != 404:
                    raise
                current = {"strict": True, "checks": []}
            existing_checks = current.get("checks")
            if not isinstance(existing_checks, list):
                raise ConfigurationError("existing GitHub required checks are invalid")
            expected_checks = [
                check
                for check in existing_checks
                if isinstance(check, dict) and check.get("context") != "ARIA integration"
            ]
            expected_checks.append(aria_check)
            self._transport.patch_json(
                checks_path,
                {"strict": True, "checks": expected_checks},
            )
        readback = self._mapping(
            self._transport.get_json(checks_path),
            "integration protection",
        )
        if readback.get("strict") is not True or readback.get("checks") != expected_checks:
            raise WorkflowError("GitHub integration protection read-back mismatch")
        return readback

    def ensure_queue_labels(self, *, repository: GitHubRepository) -> dict[str, object]:
        import urllib.parse

        specifications = {
            QUEUE_LABEL: ("5319e7", "ARIA durable Coordinator request"),
            QUEUE_KIND_LABELS["backlog"]: ("1d76db", "ARIA backlog command"),
            QUEUE_KIND_LABELS["activity"]: ("0e8a16", "ARIA activity event"),
        }
        result: dict[str, object] = {}
        for name, (color, description) in specifications.items():
            path = f"{repository.api_path}/labels/{urllib.parse.quote(name, safe='')}"
            try:
                value = self._mapping(self._transport.get_json(path), "queue label")
            except GitHubApiError as error:
                if error.status != 404:
                    raise
                value = self._mapping(
                    self._transport.post_json(
                        f"{repository.api_path}/labels",
                        {"name": name, "color": color, "description": description},
                    ),
                    "created queue label",
                )
            if (
                value.get("name") != name
                or str(value.get("color", "")).casefold() != color
                or value.get("description") != description
            ):
                raise WorkflowError("GitHub queue label read-back mismatch")
            result[name] = {
                "color": color,
                "description": description,
            }
        return result

    @staticmethod
    def _mapping(value: object, label: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ConfigurationError(f"GitHub {label} response is invalid")
        return value

    def actor(self) -> ProviderActor:
        user = self._mapping(self._transport.get_json("/user"), "user")
        user_id = user.get("id")
        login = user.get("login")
        if type(user_id) is not int or user_id <= 0 or not isinstance(login, str):
            raise ConfigurationError("GitHub user identity is invalid")
        name = user.get("name")
        return ProviderActor(
            str(user_id),
            login,
            name.strip() if isinstance(name, str) and name.strip() else None,
        )

    def exists(self, repository: GitHubRepository) -> bool:
        try:
            value = self._transport.get_json(repository.api_path)
        except GitHubApiError as error:
            if error.status == 404:
                return False
            raise
        payload = self._mapping(value, "repository")
        repository_id = payload.get("id")
        full_name = payload.get("full_name")
        if (
            type(repository_id) is not int
            or repository_id <= 0
            or not isinstance(full_name, str)
            or full_name.casefold()
            != f"{repository.owner}/{repository.name}".casefold()
        ):
            raise ConfigurationError("GitHub repository read-back is invalid")
        return True

    def read_existing(
        self, repository: GitHubRepository
    ) -> ProvisionedGitHubRepository:
        payload = self._mapping(
            self._transport.get_json(repository.api_path), "repository"
        )
        repository_id = payload.get("id")
        full_name = payload.get("full_name")
        clone_url = payload.get("clone_url")
        private = payload.get("private")
        default_branch = payload.get("default_branch")
        permissions = payload.get("permissions")
        if (
            type(repository_id) is not int
            or repository_id <= 0
            or not isinstance(full_name, str)
            or full_name.casefold() != f"{repository.owner}/{repository.name}".casefold()
            or clone_url != f"https://github.com/{repository.owner}/{repository.name}.git"
            or type(private) is not bool
            or not isinstance(default_branch, str)
            or not default_branch
            or default_branch.startswith("-")
            or ".." in default_branch
            or any(character.isspace() for character in default_branch)
            or not isinstance(permissions, dict)
            or permissions.get("admin") is not True
        ):
            raise WorkflowError(
                "existing GitHub repository read-back or admin permission is invalid"
            )
        return ProvisionedGitHubRepository(
            repository=repository,
            repository_id=str(repository_id),
            clone_url=clone_url,
            private=private,
            default_branch=default_branch,
        )

    def read_branch_heads(self, repository: GitHubRepository) -> dict[str, str]:
        heads: dict[str, str] = {}
        for page in range(1, 11):
            values = self._transport.get_json(
                f"{repository.api_path}/branches?per_page=100&page={page}"
            )
            if not isinstance(values, list):
                raise ConfigurationError("GitHub branch inventory is invalid")
            for value in values:
                branch = self._mapping(value, "branch")
                name = branch.get("name")
                commit = self._mapping(branch.get("commit"), "branch commit")
                sha = commit.get("sha")
                if (
                    not isinstance(name, str)
                    or not name
                    or not isinstance(sha, str)
                    or len(sha) not in {40, 64}
                    or any(character not in "0123456789abcdef" for character in sha)
                ):
                    raise ConfigurationError("GitHub branch inventory entry is invalid")
                heads[name] = sha
            if len(values) < 100:
                return dict(sorted(heads.items()))
        raise WorkflowError("GitHub repository exceeds the 1000-branch inspection limit")

    def create(
        self,
        *,
        repository: GitHubRepository,
        private: bool,
        description: str,
    ) -> ProvisionedGitHubRepository:
        if type(private) is not bool:
            raise ConfigurationError("GitHub repository visibility is invalid")
        if not isinstance(description, str) or not description or len(description) > 350:
            raise ConfigurationError("GitHub repository description is invalid")
        actor = self.actor()
        path = (
            "/user/repos"
            if repository.owner.casefold() == actor.username_snapshot.casefold()
            else f"/orgs/{repository.owner}/repos"
        )
        raw = self._mapping(
            self._transport.post_json(
                path,
                {
                    "name": repository.name,
                    "description": description,
                    "private": private,
                    "has_issues": True,
                    "auto_init": False,
                },
            ),
            "created repository",
        )
        repository_id = raw.get("id")
        full_name = raw.get("full_name")
        clone_url = raw.get("clone_url")
        if (
            type(repository_id) is not int
            or repository_id <= 0
            or not isinstance(full_name, str)
            or full_name.casefold()
            != f"{repository.owner}/{repository.name}".casefold()
            or not isinstance(clone_url, str)
            or clone_url
            != f"https://github.com/{repository.owner}/{repository.name}.git"
            or raw.get("private") is not private
        ):
            raise WorkflowError("GitHub created repository read-back mismatch")
        return ProvisionedGitHubRepository(
            repository=repository,
            repository_id=str(repository_id),
            clone_url=clone_url,
            private=private,
        )
