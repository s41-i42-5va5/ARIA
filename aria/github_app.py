from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aria.errors import ConfigurationError, ProviderAdapterError
from aria.github_session import CredentialBackend, TARGET_PREFIX


MAX_APP_RESPONSE_BYTES = 64 * 1024
APP_CREDENTIAL_PREFIX = f"{TARGET_PREFIX}app/"
REPOSITORY_PATH_RE = re.compile(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GitHubAppError(ProviderAdapterError):
    pass


@dataclass(frozen=True)
class GitHubInstallationToken:
    installation_id: int
    repository_id: int
    token: str = field(repr=False)
    expires_at: str


class GitHubAppTransport(Protocol):
    def get_repository_installation(
        self, *, repository_path: str, app_jwt: str
    ) -> dict[str, object]: ...

    def create_installation_token(
        self,
        *,
        installation_id: int,
        repository_id: int,
        app_jwt: str,
    ) -> dict[str, object]: ...


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigurationError(f"{label} must be a positive integer")
    return value


def _token(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise GitHubAppError(f"GitHub App {label} is invalid")
    return value


def _stamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GitHubAppError(f"GitHub App {label} is invalid")
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise GitHubAppError(f"GitHub App {label} is invalid") from error
    if stamp.astimezone(UTC) != stamp:
        raise GitHubAppError(f"GitHub App {label} must be UTC")
    return stamp


def _private_key(content: bytes) -> rsa.RSAPrivateKey:
    if not isinstance(content, bytes) or not content:
        raise ConfigurationError("GitHub App private key is empty")
    try:
        key = serialization.load_pem_private_key(content, password=None)
    except (TypeError, ValueError) as error:
        raise ConfigurationError("GitHub App private key is invalid") from error
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ConfigurationError("GitHub App private key must be RSA 2048-bit or stronger")
    return key


class GitHubAppCredentialVault:
    def __init__(self, *, backend: CredentialBackend, app_id: int) -> None:
        self.app_id = _positive(app_id, "GitHub App id")
        self._backend = backend
        self.target = f"{APP_CREDENTIAL_PREFIX}{app_id}"

    def save_private_key(self, content: bytes) -> None:
        _private_key(content)
        self._backend.write(self.target, content)

    def load_private_key(self) -> rsa.RSAPrivateKey:
        content = self._backend.read(self.target)
        if content is None:
            raise GitHubAppError("GitHub App coordinator key is not configured")
        return _private_key(content)

    def configured(self) -> bool:
        content = self._backend.read(self.target)
        if content is None:
            return False
        _private_key(content)
        return True

    def delete(self) -> None:
        self._backend.delete(self.target)


def _b64url(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).rstrip(b"=").decode("ascii")


def create_app_jwt(
    *,
    app_id: int,
    private_key: rsa.RSAPrivateKey,
    now: datetime,
) -> str:
    app_id = _positive(app_id, "GitHub App id")
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ConfigurationError("GitHub App signing key is invalid")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ConfigurationError("GitHub App JWT time must be timezone-aware")
    moment = now.astimezone(UTC)
    issued = int((moment - timedelta(seconds=60)).timestamp())
    expires = int((moment + timedelta(minutes=9)).timestamp())
    header = _b64url(b'{"alg":"RS256","typ":"JWT"}')
    payload = _b64url(
        json.dumps(
            {"iat": issued, "exp": expires, "iss": str(app_id)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class GitHubAppHttpTransport:
    def __init__(self, *, opener: object | None = None, timeout_seconds: float = 30.0) -> None:
        if opener is not None and not hasattr(opener, "open"):
            raise ConfigurationError("GitHub App opener is invalid")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ConfigurationError("GitHub App timeout is invalid")
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._timeout = timeout_seconds

    def _request(
        self, path: str, *, app_jwt: str, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        data = None
        method = "GET"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {_token(app_jwt, 'JWT')}",
            "User-Agent": "ARIA-Codex/1.5.5",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            method = "POST"
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"https://api.github.com{path}", data=data, method=method, headers=headers
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                if getattr(response, "status", 200) not in {200, 201}:
                    raise GitHubAppError("GitHub App API returned an unexpected status")
                content = response.read(MAX_APP_RESPONSE_BYTES + 1)
                if len(content) > MAX_APP_RESPONSE_BYTES:
                    raise GitHubAppError("GitHub App API response is too large")
        except urllib.error.HTTPError as error:
            request_id = error.headers.get("X-GitHub-Request-Id", "unknown")
            raise GitHubAppError(
                f"GitHub App API failed: status={error.code}, request_id={request_id}"
            ) from error
        except (OSError, urllib.error.URLError, ValueError) as error:
            raise GitHubAppError("GitHub App API transport failed") from error
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubAppError("GitHub App API returned invalid JSON") from error
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise GitHubAppError("GitHub App API response has invalid shape")
        return value

    def get_repository_installation(
        self, *, repository_path: str, app_jwt: str
    ) -> dict[str, object]:
        if REPOSITORY_PATH_RE.fullmatch(repository_path) is None:
            raise ConfigurationError("GitHub repository API path is invalid")
        return self._request(f"{repository_path}/installation", app_jwt=app_jwt)

    def create_installation_token(
        self,
        *,
        installation_id: int,
        repository_id: int,
        app_jwt: str,
    ) -> dict[str, object]:
        installation_id = _positive(installation_id, "GitHub installation id")
        repository_id = _positive(repository_id, "GitHub repository id")
        return self._request(
            f"/app/installations/{installation_id}/access_tokens",
            app_jwt=app_jwt,
            body={
                "repository_ids": [repository_id],
                "permissions": {
                    "administration": "read",
                    "checks": "write",
                    "contents": "write",
                    "issues": "write",
                    "metadata": "read",
                    "pull_requests": "read",
                    "statuses": "read",
                },
            },
        )


class GitHubInstallationSession:
    def __init__(
        self,
        *,
        app_id: int,
        repository_id: int,
        repository_path: str,
        vault: GitHubAppCredentialVault,
        transport: GitHubAppTransport,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        refresh_skew_seconds: int = 300,
    ) -> None:
        self.app_id = _positive(app_id, "GitHub App id")
        self.repository_id = _positive(repository_id, "GitHub repository id")
        if vault.app_id != self.app_id:
            raise ConfigurationError("GitHub App vault belongs to another app")
        if REPOSITORY_PATH_RE.fullmatch(repository_path) is None:
            raise ConfigurationError("GitHub repository API path is invalid")
        if type(refresh_skew_seconds) is not int or refresh_skew_seconds < 0:
            raise ConfigurationError("GitHub App refresh skew is invalid")
        self.repository_path = repository_path
        self._vault = vault
        self._transport = transport
        self._now = now
        self._refresh_skew = timedelta(seconds=refresh_skew_seconds)
        self._cached: GitHubInstallationToken | None = None

    def access_token(self) -> str:
        now = self._now().astimezone(UTC)
        if self._cached is not None and now + self._refresh_skew < _stamp(
            self._cached.expires_at, "token expiry"
        ):
            return self._cached.token
        jwt = create_app_jwt(
            app_id=self.app_id,
            private_key=self._vault.load_private_key(),
            now=now,
        )
        installation = self._transport.get_repository_installation(
            repository_path=self.repository_path, app_jwt=jwt
        )
        installation_id = _positive(
            installation.get("id"), "GitHub installation id"
        )
        if _positive(installation.get("app_id"), "GitHub installation app id") != self.app_id:
            raise GitHubAppError("GitHub repository installation belongs to another app")
        response = self._transport.create_installation_token(
            installation_id=installation_id,
            repository_id=self.repository_id,
            app_jwt=jwt,
        )
        token = _token(response.get("token"), "installation token")
        expires_at = response.get("expires_at")
        expiry = _stamp(expires_at, "token expiry")
        if expiry <= now + self._refresh_skew:
            raise GitHubAppError("GitHub App installation token expires too soon")
        self._cached = GitHubInstallationToken(
            installation_id=installation_id,
            repository_id=self.repository_id,
            token=token,
            expires_at=str(expires_at),
        )
        return token


def _public_key_sha256(key: rsa.RSAPrivateKey) -> str:
    content = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(content).hexdigest()


def configure_github_app_key(
    *,
    backend: CredentialBackend,
    app_id: int,
    private_key_path: Path,
) -> dict[str, object]:
    if not isinstance(private_key_path, Path) or not private_key_path.is_file():
        raise ConfigurationError("GitHub App private key file is unavailable")
    try:
        content = private_key_path.read_bytes()
    except OSError as error:
        raise ConfigurationError("Cannot read GitHub App private key file") from error
    vault = GitHubAppCredentialVault(backend=backend, app_id=app_id)
    key = _private_key(content)
    vault.save_private_key(content)
    read_back = vault.load_private_key()
    if _public_key_sha256(read_back) != _public_key_sha256(key):
        raise GitHubAppError("GitHub App private key read-back failed")
    return {
        "ok": True,
        "app_id": app_id,
        "configured": True,
        "public_key_sha256": _public_key_sha256(read_back),
        "credential_store": "windows-credential-manager",
    }


def github_app_key_status(
    *, backend: CredentialBackend, app_id: int
) -> dict[str, object]:
    vault = GitHubAppCredentialVault(backend=backend, app_id=app_id)
    configured = vault.configured()
    return {
        "ok": True,
        "app_id": app_id,
        "configured": configured,
        "public_key_sha256": (
            _public_key_sha256(vault.load_private_key()) if configured else None
        ),
        "credential_store": "windows-credential-manager",
    }


def remove_github_app_key(
    *, backend: CredentialBackend, app_id: int
) -> dict[str, object]:
    vault = GitHubAppCredentialVault(backend=backend, app_id=app_id)
    existed = vault.configured()
    vault.delete()
    return {
        "ok": True,
        "app_id": app_id,
        "removed": existed,
        "configured": False,
    }
