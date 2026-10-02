from __future__ import annotations

import re
import subprocess
from pathlib import Path

from aria.errors import ConfigurationError, ProviderAdapterError
from aria.github import (
    GitHubHttpClient,
    GitHubProviderAdapter,
    parse_github_remote,
)
from aria.github_app import (
    GitHubAppCredentialVault,
    GitHubAppHttpTransport,
    GitHubInstallationSession,
)
from aria.github_control import GitHubControlPlaneWriter
from aria.github_request_queue import GitHubRequestQueue
from aria.github_repository import GitHubRepositoryProvisioner
from aria.github_team import GitHubCollaboratorManager
from aria.github_integration import GitHubIntegrationVerifier
from aria.github_git import github_git_environment, resolve_github_askpass
from aria.github_auth import GitHubAuthHttpTransport
from aria.github_session import (
    CredentialBackend,
    GitHubCredentialVault,
    GitHubSessionManager,
    WindowsCredentialBackend,
)


REMOTE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def build_github_git_environment(*, client_id: str) -> dict[str, str]:
    return github_git_environment(
        client_id=client_id,
        askpass=resolve_github_askpass(),
    )


def _remote_url(code_root: Path, remote: str) -> str:
    if not isinstance(code_root, Path) or not code_root.is_dir():
        raise ConfigurationError("GitHub code root is invalid")
    if not isinstance(remote, str) or REMOTE_NAME_RE.fullmatch(remote) is None:
        raise ConfigurationError("Git remote name is invalid")
    try:
        completed = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=code_root,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ProviderAdapterError("Cannot read the configured GitHub remote") from error
    if completed.returncode != 0:
        raise ProviderAdapterError("Cannot read the configured GitHub remote")
    value = completed.stdout.strip()
    if not value or "\n" in value or "\r" in value:
        raise ProviderAdapterError("Configured GitHub remote is invalid")
    return value


def build_authenticated_github_adapter(
    *,
    code_root: Path,
    remote: str,
    client_id: str,
    coordinator_integration_id: int | None,
    credential_backend: CredentialBackend | None = None,
) -> GitHubProviderAdapter:
    repository = parse_github_remote(_remote_url(code_root, remote))
    backend = credential_backend or WindowsCredentialBackend()
    vault = GitHubCredentialVault(backend=backend, client_id=client_id)
    auth_transport = GitHubAuthHttpTransport()
    session = GitHubSessionManager(vault=vault, transport=auth_transport)
    api = GitHubHttpClient(token_source=session.access_token)
    return GitHubProviderAdapter(
        repository=repository,
        transport=api,
        coordinator_integration_id=coordinator_integration_id,
    )


def build_github_control_writer(
    *,
    code_root: Path,
    remote: str,
    repository_id: str,
    app_id: int,
    control_branch: str = "aria-control",
    credential_backend: CredentialBackend | None = None,
) -> GitHubControlPlaneWriter:
    if not isinstance(repository_id, str) or not repository_id.isdecimal():
        raise ConfigurationError("GitHub repository id must be numeric")
    repository = parse_github_remote(_remote_url(code_root, remote))
    backend = credential_backend or WindowsCredentialBackend()
    vault = GitHubAppCredentialVault(backend=backend, app_id=app_id)
    session = GitHubInstallationSession(
        app_id=app_id,
        repository_id=int(repository_id),
        repository_path=repository.api_path,
        vault=vault,
        transport=GitHubAppHttpTransport(),
    )
    return GitHubControlPlaneWriter(
        repository=repository,
        repository_id=repository_id,
        control_branch=control_branch,
        transport=GitHubHttpClient(token_source=session.access_token),
    )


def build_authenticated_github_request_queue(
    *,
    code_root: Path,
    remote: str,
    client_id: str,
    credential_backend: CredentialBackend | None = None,
) -> GitHubRequestQueue:
    repository = parse_github_remote(_remote_url(code_root, remote))
    backend = credential_backend or WindowsCredentialBackend()
    vault = GitHubCredentialVault(backend=backend, client_id=client_id)
    session = GitHubSessionManager(
        vault=vault, transport=GitHubAuthHttpTransport()
    )
    return GitHubRequestQueue(
        repository=repository,
        transport=GitHubHttpClient(token_source=session.access_token),
    )


def build_github_app_request_queue(
    *,
    code_root: Path,
    remote: str,
    repository_id: str,
    app_id: int,
    credential_backend: CredentialBackend | None = None,
) -> GitHubRequestQueue:
    if not isinstance(repository_id, str) or not repository_id.isdecimal():
        raise ConfigurationError("GitHub repository id must be numeric")
    repository = parse_github_remote(_remote_url(code_root, remote))
    backend = credential_backend or WindowsCredentialBackend()
    session = GitHubInstallationSession(
        app_id=app_id,
        repository_id=int(repository_id),
        repository_path=repository.api_path,
        vault=GitHubAppCredentialVault(backend=backend, app_id=app_id),
        transport=GitHubAppHttpTransport(),
    )
    return GitHubRequestQueue(
        repository=repository,
        transport=GitHubHttpClient(token_source=session.access_token),
    )


def build_github_repository_provisioner(
    *,
    client_id: str,
    credential_backend: CredentialBackend | None = None,
) -> GitHubRepositoryProvisioner:
    backend = credential_backend or WindowsCredentialBackend()
    session = GitHubSessionManager(
        vault=GitHubCredentialVault(backend=backend, client_id=client_id),
        transport=GitHubAuthHttpTransport(),
    )
    return GitHubRepositoryProvisioner(
        transport=GitHubHttpClient(token_source=session.access_token)
    )


def build_github_collaborator_manager(
    *,
    code_root: Path,
    remote: str,
    client_id: str,
    credential_backend: CredentialBackend | None = None,
) -> GitHubCollaboratorManager:
    repository = parse_github_remote(_remote_url(code_root, remote))
    backend = credential_backend or WindowsCredentialBackend()
    session = GitHubSessionManager(
        vault=GitHubCredentialVault(backend=backend, client_id=client_id),
        transport=GitHubAuthHttpTransport(),
    )
    return GitHubCollaboratorManager(
        repository=repository,
        transport=GitHubHttpClient(token_source=session.access_token),
    )


def build_github_app_integration_verifier(
    *,
    code_root: Path,
    remote: str,
    repository_id: str,
    app_id: int,
    credential_backend: CredentialBackend | None = None,
) -> GitHubIntegrationVerifier:
    if not isinstance(repository_id, str) or not repository_id.isdecimal():
        raise ConfigurationError("GitHub repository id must be numeric")
    repository = parse_github_remote(_remote_url(code_root, remote))
    backend = credential_backend or WindowsCredentialBackend()
    session = GitHubInstallationSession(
        app_id=app_id,
        repository_id=int(repository_id),
        repository_path=repository.api_path,
        vault=GitHubAppCredentialVault(backend=backend, app_id=app_id),
        transport=GitHubAppHttpTransport(),
    )
    return GitHubIntegrationVerifier(
        repository=repository,
        transport=GitHubHttpClient(token_source=session.access_token),
        coordinator_integration_id=app_id,
    )
