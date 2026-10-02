from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from aria.errors import (
    ConfigurationError,
    ProviderAdapterError,
    ProviderCapabilityError,
)
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
    ProviderTeamMember,
)


GITHUB_API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class GitHubApiError(ProviderAdapterError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str

    def __post_init__(self) -> None:
        if OWNER_RE.fullmatch(self.owner) is None:
            raise ConfigurationError(f"Invalid GitHub repository owner: {self.owner!r}")
        if REPOSITORY_RE.fullmatch(self.name) is None or self.name in {".", ".."}:
            raise ConfigurationError(f"Invalid GitHub repository name: {self.name!r}")

    @property
    def api_path(self) -> str:
        owner = urllib.parse.quote(self.owner, safe="")
        name = urllib.parse.quote(self.name, safe="")
        return f"/repos/{owner}/{name}"


def parse_github_remote(remote_url: str) -> GitHubRepository:
    if not isinstance(remote_url, str) or not remote_url.strip():
        raise ConfigurationError("GitHub remote URL is missing")
    value = remote_url.strip()
    owner: str
    name: str
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
        parts = path.split("/")
        if len(parts) != 2:
            raise ConfigurationError("GitHub SSH remote must identify owner/repository")
        owner, name = parts
    else:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com":
            raise ConfigurationError("Only github.com HTTPS or SSH remotes are supported")
        if parsed.password is not None or (
            parsed.scheme == "https" and parsed.username is not None
        ) or (parsed.scheme == "ssh" and parsed.username not in {None, "git"}):
            raise ConfigurationError("GitHub remote must not contain embedded credentials")
        if parsed.query or parsed.fragment:
            raise ConfigurationError("GitHub remote URL must not contain query or fragment")
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2:
            raise ConfigurationError("GitHub remote must identify owner/repository")
        owner, name = parts
    if name.endswith(".git"):
        name = name[:-4]
    return GitHubRepository(owner=owner, name=name)


class GitHubJsonTransport(Protocol):
    def get_json(self, path: str) -> object: ...


class GitHubMutationTransport(GitHubJsonTransport, Protocol):
    def post_json(self, path: str, payload: object) -> object: ...
    def patch_json(self, path: str, payload: object) -> object: ...
    def put_json(self, path: str, payload: object) -> object: ...
    def delete_json(self, path: str) -> object: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class GitHubHttpClient:
    def __init__(
        self,
        *,
        token_source: Callable[[], str],
        api_base: str = "https://api.github.com",
        opener: object | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        parsed = urllib.parse.urlsplit(api_base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise ConfigurationError("GitHub API base must be an HTTPS origin")
        if parsed.path not in {"", "/"}:
            raise ConfigurationError("GitHub API base must not contain a path")
        if not callable(token_source):
            raise ConfigurationError("GitHub token source must be callable")
        if opener is not None and not hasattr(opener, "open"):
            raise ConfigurationError("GitHub HTTP opener is invalid")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ConfigurationError("GitHub API timeout must be between 0 and 120 seconds")
        self._token_source = token_source
        self._api_base = api_base.rstrip("/")
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _token(value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or any(character.isspace() for character in value)
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
        ):
            raise GitHubApiError("GitHub session token is unavailable or invalid")
        return value

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        expected_statuses: frozenset[int],
    ) -> object:
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ConfigurationError("GitHub API path must be origin-relative")
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise ConfigurationError("GitHub API method is invalid")
        data = None
        if method in {"GET", "DELETE"}:
            if payload is not None:
                raise ConfigurationError(f"GitHub {method} request cannot contain a payload")
        else:
            try:
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise ConfigurationError("GitHub API payload is not JSON serializable") from error
        token = self._token(self._token_source())
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ARIA-Codex/1.5.5",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._api_base}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                if getattr(response, "status", 200) not in expected_statuses:
                    raise GitHubApiError("GitHub API returned an unexpected status")
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > MAX_RESPONSE_BYTES:
                    raise GitHubApiError("GitHub API response exceeds the size limit")
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise GitHubApiError("GitHub API response exceeds the size limit")
        except urllib.error.HTTPError as error:
            request_id = error.headers.get("X-GitHub-Request-Id", "unknown")
            raise GitHubApiError(
                f"GitHub API request failed: status={error.code}, request_id={request_id}",
                status=error.code,
            ) from error
        except (OSError, urllib.error.URLError, ValueError) as error:
            raise GitHubApiError("GitHub API transport failed") from error
        if not content:
            return {}
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubApiError("GitHub API returned invalid JSON") from error

    def get_json(self, path: str) -> object:
        return self._request_json(
            "GET", path, expected_statuses=frozenset({200})
        )

    def post_json(self, path: str, payload: object) -> object:
        return self._request_json(
            "POST", path, payload=payload, expected_statuses=frozenset({200, 201})
        )

    def patch_json(self, path: str, payload: object) -> object:
        return self._request_json(
            "PATCH", path, payload=payload, expected_statuses=frozenset({200})
        )

    def put_json(self, path: str, payload: object) -> object:
        return self._request_json(
            "PUT", path, payload=payload, expected_statuses=frozenset({200, 201, 204})
        )

    def delete_json(self, path: str) -> object:
        return self._request_json(
            "DELETE", path, expected_statuses=frozenset({204})
        )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GitHubApiError(f"GitHub {label} response has invalid shape")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GitHubApiError(f"GitHub {label} is missing")
    return value


def _required_id(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise GitHubApiError(f"GitHub {label} is missing")
    return value


def _membership_role(permission: str, role_name: str | None) -> str | None:
    normalized = (role_name or permission).lower()
    if permission == "admin":
        return "admin"
    if permission == "maintain" or normalized == "maintain":
        return "maintainer"
    if permission in {"write", "push"}:
        return "contributor"
    if permission in {"read", "pull", "triage"}:
        return "viewer"
    return None


class GitHubProviderAdapter:
    provider_id = "github"

    def __init__(
        self,
        *,
        repository: GitHubRepository,
        transport: GitHubJsonTransport,
        coordinator_integration_id: int | None,
    ) -> None:
        if not isinstance(repository, GitHubRepository):
            raise ConfigurationError("GitHub repository locator is invalid")
        if not hasattr(transport, "get_json"):
            raise ConfigurationError("GitHub transport is invalid")
        if coordinator_integration_id is not None and (
            type(coordinator_integration_id) is not int
            or coordinator_integration_id <= 0
        ):
            raise ConfigurationError("GitHub coordinator integration id is invalid")
        self._repository = repository
        self._transport = transport
        self._coordinator_integration_id = coordinator_integration_id

    def _protection(self, control_branch: str) -> ProviderBranchProtection:
        if (
            not isinstance(control_branch, str)
            or not control_branch
            or any(character in control_branch for character in "?*[]")
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in control_branch
            )
        ):
            raise ConfigurationError("GitHub control branch name is invalid")
        branch = urllib.parse.quote(control_branch, safe="")
        try:
            active_rules = self._transport.get_json(
                f"{self._repository.api_path}/rules/branches/{branch}?per_page=100"
            )
        except GitHubApiError as error:
            if error.status == 404:
                active_rules = []
            elif error.status == 403:
                raise ProviderCapabilityError(
                    "GitHub denied ruleset read-back; coordinator-only aria-control "
                    "protection requires repository rulesets to be enforced by the "
                    "current GitHub plan and permitted for this user token"
                ) from error
            else:
                raise
        if not isinstance(active_rules, list):
            raise GitHubApiError("GitHub active rules response has invalid shape")
        ruleset_ids = sorted(
            {
                rule.get("ruleset_id")
                for rule in active_rules
                if isinstance(rule, dict)
                and rule.get("type") in {"creation", "update"}
                and type(rule.get("ruleset_id")) is int
            }
        )
        relevant = 0
        all_bypass = True
        for ruleset_id in ruleset_ids:
            try:
                ruleset_value = self._transport.get_json(
                    f"{self._repository.api_path}/rulesets/{ruleset_id}"
                    "?includes_parents=true"
                )
            except GitHubApiError as error:
                if error.status == 403:
                    raise ProviderCapabilityError(
                        "GitHub denied ruleset detail read-back; coordinator-only "
                        "aria-control protection requires repository rulesets to be "
                        "enforced by the current GitHub plan and permitted for this "
                        "user token"
                    ) from error
                raise
            raw = _mapping(
                ruleset_value,
                "ruleset",
            )
            if raw.get("target") != "branch" or raw.get("enforcement") != "active":
                continue
            rules = raw.get("rules")
            if not isinstance(rules, list):
                continue
            rule_types = {
                rule.get("type") for rule in rules if isinstance(rule, dict)
            }
            if not {"creation", "update"}.issubset(rule_types):
                continue
            relevant += 1
            bypass = raw.get("bypass_actors")
            if not isinstance(bypass, list) or len(bypass) != 1:
                all_bypass = False
                continue
            actor = bypass[0]
            if not isinstance(actor, dict):
                continue
            matching_bypass = (
                self._coordinator_integration_id is not None
                and actor.get("actor_type") == "Integration"
                and actor.get("actor_id") == self._coordinator_integration_id
                and actor.get("bypass_mode") == "always"
            )
            if not matching_bypass:
                all_bypass = False
        strong = relevant > 0 and all_bypass
        return ProviderBranchProtection(
            protected=strong,
            direct_user_push=not strong,
            canonical_writer="aria-coordinator" if strong else None,
        )

    def ensure_control_protection(
        self,
        *,
        repository_id: str,
        control_branch: str,
    ) -> dict[str, object]:
        if self._coordinator_integration_id is None:
            raise ConfigurationError("GitHub coordinator integration id is required")
        if not all(
            hasattr(self._transport, method) for method in ("get_json", "post_json")
        ):
            raise ConfigurationError("GitHub provider transport is read-only")
        repository = _mapping(
            self._transport.get_json(self._repository.api_path), "repository"
        )
        actual_repository_id = _required_id(repository.get("id"), "repository id")
        if str(actual_repository_id) != repository_id:
            raise ConfigurationError(
                "GitHub protection target does not match immutable repository_id"
            )
        current = self._protection(control_branch)
        if current.coordinator_only:
            return {"created": False, "protection": current.as_mapping()}
        branch = urllib.parse.quote(control_branch, safe="")
        try:
            active_rules = self._transport.get_json(
                f"{self._repository.api_path}/rules/branches/{branch}?per_page=100"
            )
        except GitHubApiError as error:
            if error.status == 403:
                raise ProviderCapabilityError(
                    "GitHub denied ruleset conflict read-back; coordinator-only "
                    "aria-control protection requires repository rulesets to be "
                    "enforced by the current GitHub plan and permitted for this "
                    "user token"
                ) from error
            raise
        if not isinstance(active_rules, list):
            raise GitHubApiError("GitHub active rules response has invalid shape")
        conflicting_ids = sorted(
            {
                rule.get("ruleset_id")
                for rule in active_rules
                if isinstance(rule, dict)
                and rule.get("type") in {"creation", "update"}
                and type(rule.get("ruleset_id")) is int
            }
        )
        if conflicting_ids:
            raise ProviderAdapterError(
                "Existing aria-control rulesets conflict with coordinator-only policy"
            )
        try:
            created_value = self._transport.post_json(
                f"{self._repository.api_path}/rulesets",
                {
                    "name": "ARIA Coordinator control branch",
                    "target": "branch",
                    "enforcement": "active",
                    "bypass_actors": [
                        {
                            "actor_id": self._coordinator_integration_id,
                            "actor_type": "Integration",
                            "bypass_mode": "always",
                        }
                    ],
                    "conditions": {
                        "ref_name": {
                            "include": [f"refs/heads/{control_branch}"],
                            "exclude": [],
                        }
                    },
                    "rules": [
                        {"type": "creation"},
                        {"type": "update"},
                        {"type": "deletion"},
                        {"type": "non_fast_forward"},
                    ],
                },
            )
        except GitHubApiError as error:
            if error.status == 403:
                raise ProviderCapabilityError(
                    "GitHub denied ruleset creation; coordinator-only aria-control "
                    "protection requires repository rulesets to be enforced by the "
                    "current GitHub plan and Administration write permission"
                ) from error
            raise
        created = _mapping(created_value, "created ruleset")
        ruleset_id = _required_id(created.get("id"), "ruleset id")
        verified = self._protection(control_branch)
        if not verified.coordinator_only:
            raise ProviderAdapterError(
                "GitHub ruleset creation did not produce coordinator-only protection"
            )
        return {
            "created": True,
            "ruleset_id": ruleset_id,
            "protection": verified.as_mapping(),
        }

    def inspect_collaboration(
        self,
        *,
        repository_id: str,
        control_branch: str,
    ) -> ProviderInspection:
        if (
            not isinstance(repository_id, str)
            or re.fullmatch(r"[0-9]+", repository_id) is None
        ):
            raise ConfigurationError("GitHub repository_id must be a numeric string")
        user = _mapping(self._transport.get_json("/user"), "user")
        actor_id = _required_id(user.get("id"), "user id")
        login = _required_string(user.get("login"), "login")
        display_name = user.get("name")
        if display_name is not None and not isinstance(display_name, str):
            raise GitHubApiError("GitHub display name has invalid shape")

        repository = _mapping(
            self._transport.get_json(self._repository.api_path),
            "repository",
        )
        actual_repository_id = _required_id(repository.get("id"), "repository id")
        if str(actual_repository_id) != repository_id:
            raise ConfigurationError(
                "GitHub repository does not match immutable repository_id"
            )
        full_name = _required_string(repository.get("full_name"), "repository full_name")
        expected_full_name = f"{self._repository.owner}/{self._repository.name}"
        if full_name.casefold() != expected_full_name.casefold():
            raise ConfigurationError("GitHub repository locator read-back mismatch")

        login_path = urllib.parse.quote(login, safe="")
        try:
            permission_payload = _mapping(
                self._transport.get_json(
                    f"{self._repository.api_path}/collaborators/{login_path}/permission"
                ),
                "permission",
            )
        except GitHubApiError as error:
            if error.status != 404:
                raise
            permission_payload = {"permission": "none"}
        permission = _required_string(
            permission_payload.get("permission"),
            "repository permission",
        ).lower()
        role_name_value = permission_payload.get("role_name")
        role_name = role_name_value if isinstance(role_name_value, str) else None
        permission_user = permission_payload.get("user")
        if permission_user is not None:
            permission_user_mapping = _mapping(permission_user, "permission user")
            permission_user_id = _required_id(
                permission_user_mapping.get("id"),
                "permission user id",
            )
            if permission_user_id != actor_id:
                raise ConfigurationError(
                    "GitHub membership read-back belongs to another user id"
                )
        role = _membership_role(permission, role_name)
        active = role is not None
        membership = ProviderMembership(
            active=active,
            roles=(role,) if role is not None else (),
        )
        return ProviderInspection(
            provider="github",
            repository_id=str(actual_repository_id),
            actor=ProviderActor(
                user_id=str(actor_id),
                username_snapshot=login,
                display_name_snapshot=display_name.strip()
                if isinstance(display_name, str) and display_name.strip()
                else None,
            ),
            membership=membership,
            protection=self._protection(control_branch),
        )

    def list_collaborators(
        self,
        *,
        repository_id: str,
    ) -> tuple[ProviderTeamMember, ...]:
        if not isinstance(repository_id, str) or not repository_id.isdecimal():
            raise ConfigurationError("GitHub repository_id must be a numeric string")
        repository = _mapping(
            self._transport.get_json(self._repository.api_path),
            "repository",
        )
        actual_repository_id = _required_id(repository.get("id"), "repository id")
        if str(actual_repository_id) != repository_id:
            raise ConfigurationError(
                "GitHub collaborator listing does not match immutable repository_id"
            )
        members: dict[int, ProviderTeamMember] = {}
        for page in range(1, 101):
            value = self._transport.get_json(
                f"{self._repository.api_path}/collaborators"
                f"?affiliation=all&per_page=100&page={page}"
            )
            if not isinstance(value, list):
                raise GitHubApiError("GitHub collaborators response has invalid shape")
            for raw_value in value:
                raw = _mapping(raw_value, "collaborator")
                actor_id = _required_id(raw.get("id"), "collaborator id")
                if actor_id in members:
                    raise GitHubApiError("GitHub collaborators contain a duplicate user id")
                login = _required_string(raw.get("login"), "collaborator login")
                permissions = _mapping(raw.get("permissions"), "collaborator permissions")
                permission = next(
                    (
                        candidate
                        for candidate in ("admin", "maintain", "push", "triage", "pull")
                        if permissions.get(candidate) is True
                    ),
                    None,
                )
                role_name_value = raw.get("role_name")
                role_name = role_name_value if isinstance(role_name_value, str) else None
                role = _membership_role(permission or "none", role_name)
                if role is None:
                    raise GitHubApiError(
                        "GitHub collaborator has no recognized repository permission"
                    )
                display_name = raw.get("name")
                if display_name is not None and not isinstance(display_name, str):
                    raise GitHubApiError("GitHub collaborator display name is invalid")
                members[actor_id] = ProviderTeamMember(
                    provider="github",
                    actor=ProviderActor(
                        user_id=str(actor_id),
                        username_snapshot=login,
                        display_name_snapshot=(
                            display_name.strip()
                            if isinstance(display_name, str) and display_name.strip()
                            else None
                        ),
                    ),
                    membership=ProviderMembership(active=True, roles=(role,)),
                )
            if len(value) < 100:
                return tuple(members[key] for key in sorted(members))
        raise GitHubApiError("GitHub collaborators pagination exceeds the safety limit")
