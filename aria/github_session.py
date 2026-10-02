from __future__ import annotations

import ctypes
import json
import os
import re
from ctypes import wintypes
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aria.errors import ConfigurationError, ProviderAdapterError
from aria.github_auth import (
    CLIENT_ID_RE,
    GitHubAuthTransport,
    GitHubTokenSet,
    refresh_user_token,
)


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_CREDENTIAL_BYTES = 2560
TARGET_PREFIX = "ARIA-Codex/github/"


class GitHubSessionError(ProviderAdapterError):
    pass


class CredentialBackend(Protocol):
    def write(self, target: str, secret: bytes) -> None: ...
    def read(self, target: str) -> bytes | None: ...
    def delete(self, target: str) -> None: ...


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialBackend:
    def __init__(self, *, api: object | None = None) -> None:
        if os.name != "nt" and api is None:
            raise ConfigurationError("Windows Credential Manager is unavailable on this OS")
        self._api = api or ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._configure_api()

    def _configure_api(self) -> None:
        self._api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    @staticmethod
    def _target(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value.startswith(TARGET_PREFIX)
            or len(value) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ConfigurationError("GitHub credential target is invalid")
        return value

    def write(self, target: str, secret: bytes) -> None:
        target = self._target(target)
        if not isinstance(secret, bytes) or not secret or len(secret) > MAX_CREDENTIAL_BYTES:
            raise ConfigurationError("GitHub credential payload is invalid")
        blob = (ctypes.c_ubyte * len(secret)).from_buffer_copy(secret)
        credential = _CREDENTIALW(
            Flags=0,
            Type=CRED_TYPE_GENERIC,
            TargetName=target,
            Comment="ARIA Codex GitHub user session",
            CredentialBlobSize=len(secret),
            CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)),
            Persist=CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount=0,
            Attributes=None,
            TargetAlias=None,
            UserName="ARIA-Codex",
        )
        if not self._api.CredWriteW(ctypes.byref(credential), 0):
            raise GitHubSessionError(
                f"Windows Credential Manager write failed: error={ctypes.get_last_error()}"
            )

    def read(self, target: str) -> bytes | None:
        target = self._target(target)
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == ERROR_NOT_FOUND:
                return None
            raise GitHubSessionError(
                f"Windows Credential Manager read failed: error={error}"
            )
        try:
            credential = pointer.contents
            return ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
        finally:
            self._api.CredFree(pointer)

    def delete(self, target: str) -> None:
        target = self._target(target)
        if not self._api.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != ERROR_NOT_FOUND:
                raise GitHubSessionError(
                    f"Windows Credential Manager delete failed: error={error}"
                )


@dataclass(frozen=True)
class StoredGitHubSession:
    client_id: str
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = "bearer"
    issued_at: str = ""
    expires_at: str | None = None
    refresh_expires_at: str | None = None


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


def _format(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _secret(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ConfigurationError(f"{label} is invalid")
    return value


class GitHubCredentialVault:
    def __init__(self, *, backend: CredentialBackend, client_id: str) -> None:
        if CLIENT_ID_RE.fullmatch(client_id) is None:
            raise ConfigurationError("GitHub App client_id is invalid")
        self._backend = backend
        self.client_id = client_id
        self.target = f"{TARGET_PREFIX}{client_id}"

    def save_token_set(
        self,
        token: GitHubTokenSet,
        *,
        now: datetime,
    ) -> StoredGitHubSession:
        if not isinstance(token, GitHubTokenSet):
            raise ConfigurationError("GitHub token set is invalid")
        now = now.astimezone(UTC)
        session = StoredGitHubSession(
            client_id=self.client_id,
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            token_type=token.token_type,
            issued_at=_format(now),
            expires_at=(
                _format(now + timedelta(seconds=token.expires_in))
                if token.expires_in is not None
                else None
            ),
            refresh_expires_at=(
                _format(now + timedelta(seconds=token.refresh_token_expires_in))
                if token.refresh_token_expires_in is not None
                else None
            ),
        )
        payload = {
            "schema_version": 1,
            "client_id": session.client_id,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "token_type": session.token_type,
            "issued_at": session.issued_at,
            "expires_at": session.expires_at,
            "refresh_expires_at": session.refresh_expires_at,
        }
        self._backend.write(
            self.target,
            (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        return session

    def load(self) -> StoredGitHubSession | None:
        content = self._backend.read(self.target)
        if content is None:
            return None
        try:
            raw = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigurationError("Stored GitHub session is invalid") from error
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "client_id",
            "access_token",
            "refresh_token",
            "token_type",
            "issued_at",
            "expires_at",
            "refresh_expires_at",
        }:
            raise ConfigurationError("Stored GitHub session schema is invalid")
        if (
            type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != 1
            or raw.get("client_id") != self.client_id
        ):
            raise ConfigurationError("Stored GitHub session identity is invalid")
        if raw.get("token_type") != "bearer":
            raise ConfigurationError("Stored GitHub session token type is invalid")
        issued = _stamp(raw.get("issued_at"), "GitHub session issued_at")
        expires = raw.get("expires_at")
        refresh_expires = raw.get("refresh_expires_at")
        if expires is not None and _stamp(expires, "GitHub session expires_at") <= issued:
            raise ConfigurationError("Stored GitHub session expiry is invalid")
        if refresh_expires is not None and _stamp(
            refresh_expires, "GitHub session refresh_expires_at"
        ) <= issued:
            raise ConfigurationError("Stored GitHub refresh expiry is invalid")
        if (raw.get("refresh_token") is None) != (refresh_expires is None):
            raise ConfigurationError("Stored GitHub refresh token metadata is inconsistent")
        return StoredGitHubSession(
            client_id=self.client_id,
            access_token=str(_secret(raw.get("access_token"), "GitHub access token")),
            refresh_token=_secret(
                raw.get("refresh_token"), "GitHub refresh token", optional=True
            ),
            token_type="bearer",
            issued_at=str(raw["issued_at"]),
            expires_at=expires if isinstance(expires, str) else None,
            refresh_expires_at=(
                refresh_expires if isinstance(refresh_expires, str) else None
            ),
        )

    def delete(self) -> None:
        self._backend.delete(self.target)


class GitHubSessionManager:
    def __init__(
        self,
        *,
        vault: GitHubCredentialVault,
        transport: GitHubAuthTransport,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        refresh_skew_seconds: int = 300,
    ) -> None:
        if type(refresh_skew_seconds) is not int or refresh_skew_seconds < 0:
            raise ConfigurationError("GitHub refresh skew is invalid")
        self._vault = vault
        self._transport = transport
        self._now = now
        self._refresh_skew = timedelta(seconds=refresh_skew_seconds)

    def store(self, token: GitHubTokenSet) -> StoredGitHubSession:
        return self._vault.save_token_set(token, now=self._now())

    def access_token(self) -> str:
        session = self._vault.load()
        if session is None:
            raise GitHubSessionError("GitHub login is required")
        now = self._now().astimezone(UTC)
        if session.expires_at is None or now + self._refresh_skew < _stamp(
            session.expires_at, "GitHub session expires_at"
        ):
            return session.access_token
        if session.refresh_token is None:
            raise GitHubSessionError("GitHub session expired; login is required")
        if session.refresh_expires_at is not None and now >= _stamp(
            session.refresh_expires_at, "GitHub session refresh_expires_at"
        ):
            raise GitHubSessionError("GitHub refresh token expired; login is required")
        rotated = refresh_user_token(
            self._transport,
            client_id=self._vault.client_id,
            refresh_token=session.refresh_token,
        )
        return self._vault.save_token_set(rotated, now=now).access_token

    def logout(self) -> None:
        self._vault.delete()
