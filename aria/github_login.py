from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from aria.errors import ConfigurationError
from aria.github_auth import (
    CLIENT_ID_RE,
    GitHubAuthHttpTransport,
    GitHubAuthTransport,
    GitHubDeviceAuthorization,
    complete_device_authorization,
    request_device_authorization,
)
from aria.github_session import (
    CredentialBackend,
    GitHubCredentialVault,
    GitHubSessionManager,
    WindowsCredentialBackend,
)


PENDING_TARGET_PREFIX = "ARIA-Codex/github/pending/"


def _format(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _stamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ConfigurationError(f"{label} must be a UTC timestamp")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"{label} is invalid") from error
    if result.astimezone(UTC) != result:
        raise ConfigurationError(f"{label} must be UTC")
    return result


class GitHubLoginService:
    def __init__(
        self,
        *,
        client_id: str,
        backend: CredentialBackend,
        transport: GitHubAuthTransport,
        now=lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(client_id, str) or CLIENT_ID_RE.fullmatch(client_id) is None:
            raise ConfigurationError("GitHub App client_id is invalid")
        self.client_id = client_id
        self._backend = backend
        self._transport = transport
        self._now = now
        self._vault = GitHubCredentialVault(backend=backend, client_id=client_id)

    def _pending_target(self, repository_id: str) -> str:
        if not isinstance(repository_id, str) or not repository_id.isdecimal():
            raise ConfigurationError("GitHub repository_id must be numeric")
        return f"{PENDING_TARGET_PREFIX}{self.client_id}/{repository_id}"

    def begin(self, *, repository_id: str) -> dict[str, object]:
        target = self._pending_target(repository_id)
        authorization = request_device_authorization(
            self._transport,
            client_id=self.client_id,
            repository_id=repository_id,
        )
        now = self._now().astimezone(UTC)
        expires_at = now + timedelta(seconds=authorization.expires_in)
        payload = {
            "schema_version": 1,
            "client_id": self.client_id,
            "repository_id": repository_id,
            "device_code": authorization.device_code,
            "user_code": authorization.user_code,
            "verification_uri": authorization.verification_uri,
            "interval": authorization.interval,
            "created_at": _format(now),
            "expires_at": _format(expires_at),
        }
        self._backend.write(
            target,
            (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        return {
            "ok": True,
            "provider": "github",
            "repository_id": repository_id,
            "verification_uri": authorization.verification_uri,
            "user_code": authorization.user_code,
            "expires_at": _format(expires_at),
            "poll_interval_seconds": authorization.interval,
            "browser_handoff": "codex-in-app-required",
        }

    def _load_pending(self, repository_id: str) -> tuple[str, dict[str, object]]:
        target = self._pending_target(repository_id)
        content = self._backend.read(target)
        if content is None:
            raise ConfigurationError("Pending GitHub login was not found; start login again")
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigurationError("Pending GitHub login is invalid") from error
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "client_id",
            "repository_id",
            "device_code",
            "user_code",
            "verification_uri",
            "interval",
            "created_at",
            "expires_at",
        }:
            raise ConfigurationError("Pending GitHub login schema is invalid")
        if (
            type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
            or raw.get("client_id") != self.client_id
            or raw.get("repository_id") != repository_id
        ):
            raise ConfigurationError("Pending GitHub login identity is invalid")
        return target, raw

    def complete(self, *, repository_id: str) -> dict[str, object]:
        target, pending = self._load_pending(repository_id)
        now = self._now().astimezone(UTC)
        remaining = math.ceil((_stamp(pending["expires_at"], "GitHub login expiry") - now).total_seconds())
        interval = pending.get("interval")
        if type(interval) is not int or interval <= 0:
            raise ConfigurationError("Pending GitHub login interval is invalid")
        if remaining <= interval:
            self._backend.delete(target)
            raise ConfigurationError("Pending GitHub login expired; start login again")
        authorization = GitHubDeviceAuthorization(
            device_code=str(pending["device_code"]),
            user_code=str(pending["user_code"]),
            verification_uri=str(pending["verification_uri"]),
            expires_in=remaining,
            interval=interval,
        )
        token = complete_device_authorization(
            self._transport,
            client_id=self.client_id,
            authorization=authorization,
        )
        session = GitHubSessionManager(
            vault=self._vault,
            transport=self._transport,
            now=self._now,
        ).store(token)
        self._backend.delete(target)
        return {
            "ok": True,
            "provider": "github",
            "repository_id": repository_id,
            "logged_in": True,
            "expires_at": session.expires_at,
            "refresh_expires_at": session.refresh_expires_at,
        }

    def status(self) -> dict[str, object]:
        session = self._vault.load()
        if session is None:
            return {"ok": True, "provider": "github", "logged_in": False}
        now = self._now().astimezone(UTC)
        access_expired = (
            session.expires_at is not None
            and now >= _stamp(session.expires_at, "GitHub session expiry")
        )
        refresh_expired = (
            session.refresh_expires_at is not None
            and now >= _stamp(
                session.refresh_expires_at, "GitHub refresh session expiry"
            )
        )
        return {
            "ok": True,
            "provider": "github",
            "logged_in": not access_expired or (
                session.refresh_token is not None and not refresh_expired
            ),
            "access_expired": access_expired,
            "refresh_available": session.refresh_token is not None and not refresh_expired,
            "expires_at": session.expires_at,
            "refresh_expires_at": session.refresh_expires_at,
        }

    def logout(self, *, repository_id: str) -> dict[str, object]:
        self._vault.delete()
        self._backend.delete(self._pending_target(repository_id))
        return {"ok": True, "provider": "github", "logged_in": False}


def default_github_login_service(client_id: str) -> GitHubLoginService:
    return GitHubLoginService(
        client_id=client_id,
        backend=WindowsCredentialBackend(),
        transport=GitHubAuthHttpTransport(),
    )
