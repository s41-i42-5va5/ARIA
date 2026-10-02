from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from aria.errors import ConfigurationError, ProviderAdapterError


GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_VERIFICATION_URL = "https://github.com/login/device"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_AUTH_RESPONSE_BYTES = 64 * 1024
CLIENT_ID_RE = re.compile(r"[A-Za-z0-9._-]{8,128}")


class GitHubAuthError(ProviderAdapterError):
    pass


@dataclass(frozen=True)
class GitHubDeviceAuthorization:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class GitHubTokenSet:
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = "bearer"
    expires_in: int | None = None
    refresh_token_expires_in: int | None = None


class GitHubAuthTransport(Protocol):
    def post_form(self, url: str, fields: dict[str, str]) -> dict[str, object]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class GitHubAuthHttpTransport:
    def __init__(self, *, opener: object | None = None, timeout_seconds: float = 30.0) -> None:
        if opener is not None and not hasattr(opener, "open"):
            raise ConfigurationError("GitHub auth opener is invalid")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ConfigurationError("GitHub auth timeout is invalid")
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._timeout_seconds = timeout_seconds

    def post_form(self, url: str, fields: dict[str, str]) -> dict[str, object]:
        if url not in {GITHUB_DEVICE_CODE_URL, GITHUB_TOKEN_URL}:
            raise ConfigurationError("GitHub auth URL is not allowed")
        if not isinstance(fields, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in fields.items()
        ):
            raise ConfigurationError("GitHub auth form is invalid")
        body = urllib.parse.urlencode(fields).encode("ascii")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "ARIA-Codex/1.5.5",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                if getattr(response, "status", 200) != 200:
                    raise GitHubAuthError("GitHub authorization returned an unexpected status")
                content = response.read(MAX_AUTH_RESPONSE_BYTES + 1)
                if len(content) > MAX_AUTH_RESPONSE_BYTES:
                    raise GitHubAuthError("GitHub authorization response is too large")
        except urllib.error.HTTPError as error:
            request_id = error.headers.get("X-GitHub-Request-Id", "unknown")
            raise GitHubAuthError(
                f"GitHub authorization failed: status={error.code}, request_id={request_id}"
            ) from error
        except (OSError, urllib.error.URLError, ValueError) as error:
            raise GitHubAuthError("GitHub authorization transport failed") from error
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubAuthError("GitHub authorization returned invalid JSON") from error
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise GitHubAuthError("GitHub authorization response has invalid shape")
        return value


def _client_id(value: str) -> str:
    if not isinstance(value, str) or CLIENT_ID_RE.fullmatch(value) is None:
        raise ConfigurationError("GitHub App client_id is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise GitHubAuthError(f"GitHub {label} is missing or invalid")
    return value


def _token(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise GitHubAuthError(f"GitHub {label} is missing or invalid")
    return value


def request_device_authorization(
    transport: GitHubAuthTransport,
    *,
    client_id: str,
    repository_id: str | None = None,
) -> GitHubDeviceAuthorization:
    fields = {"client_id": _client_id(client_id)}
    if repository_id is not None:
        if not isinstance(repository_id, str) or not repository_id.isdecimal():
            raise ConfigurationError("GitHub repository_id must be numeric")
        fields["repository_id"] = repository_id
    response = transport.post_form(GITHUB_DEVICE_CODE_URL, fields)
    verification_uri = response.get("verification_uri")
    if verification_uri != GITHUB_VERIFICATION_URL:
        raise GitHubAuthError("GitHub returned an unexpected verification URI")
    user_code = response.get("user_code")
    if not isinstance(user_code, str) or re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", user_code) is None:
        raise GitHubAuthError("GitHub user code is missing or invalid")
    return GitHubDeviceAuthorization(
        device_code=_token(response.get("device_code"), "device code"),
        user_code=user_code,
        verification_uri=verification_uri,
        expires_in=_positive_int(response.get("expires_in"), "device expiry"),
        interval=_positive_int(response.get("interval"), "poll interval"),
    )


def _parse_token(response: dict[str, object]) -> GitHubTokenSet:
    token_type = response.get("token_type")
    if token_type != "bearer":
        raise GitHubAuthError("GitHub token type is missing or invalid")
    refresh = response.get("refresh_token")
    if refresh is not None:
        refresh = _token(refresh, "refresh token")
    expires = response.get("expires_in")
    refresh_expires = response.get("refresh_token_expires_in")
    return GitHubTokenSet(
        access_token=_token(response.get("access_token"), "access token"),
        refresh_token=refresh,
        token_type=token_type,
        expires_in=_positive_int(expires, "token expiry") if expires is not None else None,
        refresh_token_expires_in=(
            _positive_int(refresh_expires, "refresh token expiry")
            if refresh_expires is not None
            else None
        ),
    )


def complete_device_authorization(
    transport: GitHubAuthTransport,
    *,
    client_id: str,
    authorization: GitHubDeviceAuthorization,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> GitHubTokenSet:
    client_id = _client_id(client_id)
    started = monotonic()
    interval = authorization.interval
    while True:
        if monotonic() - started + interval >= authorization.expires_in:
            raise GitHubAuthError("GitHub device authorization expired")
        sleep(interval)
        response = transport.post_form(
            GITHUB_TOKEN_URL,
            {
                "client_id": client_id,
                "device_code": authorization.device_code,
                "grant_type": DEVICE_GRANT,
            },
        )
        error = response.get("error")
        if error is None:
            return _parse_token(response)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error in {
            "access_denied",
            "expired_token",
            "incorrect_client_credentials",
            "incorrect_device_code",
            "unverified_user_email",
        }:
            raise GitHubAuthError(f"GitHub device authorization stopped: {error}")
        raise GitHubAuthError("GitHub device authorization returned an unknown error")


def refresh_user_token(
    transport: GitHubAuthTransport,
    *,
    client_id: str,
    refresh_token: str,
) -> GitHubTokenSet:
    response = transport.post_form(
        GITHUB_TOKEN_URL,
        {
            "client_id": _client_id(client_id),
            "grant_type": "refresh_token",
            "refresh_token": _token(refresh_token, "refresh token"),
        },
    )
    if response.get("error") is not None:
        error = response.get("error")
        if error in {"bad_refresh_token", "incorrect_client_credentials"}:
            raise GitHubAuthError(f"GitHub token refresh stopped: {error}")
        raise GitHubAuthError("GitHub token refresh returned an error")
    return _parse_token(response)
