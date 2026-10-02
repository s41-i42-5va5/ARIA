from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from aria.github_auth import GitHubTokenSet
from aria.github_session import (
    GitHubCredentialVault,
    GitHubSessionError,
    GitHubSessionManager,
    WindowsCredentialBackend,
)


CLIENT_ID = "Iv1.client123"


class _Backend:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def write(self, target: str, secret: bytes) -> None:
        self.values[target] = secret

    def read(self, target: str) -> bytes | None:
        return self.values.get(target)

    def delete(self, target: str) -> None:
        self.values.pop(target, None)


class _Transport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post_form(self, url: str, fields: dict[str, str]) -> dict[str, object]:
        self.calls.append((url, fields))
        return self.response


def _token(*, access: str = "ghu_access", refresh: str | None = "ghr_refresh") -> GitHubTokenSet:
    return GitHubTokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_in=28800,
        refresh_token_expires_in=15897600 if refresh else None,
    )


class GitHubSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _Backend()
        self.vault = GitHubCredentialVault(backend=self.backend, client_id=CLIENT_ID)
        self.now = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)

    def test_vault_round_trip_hides_tokens_and_uses_only_credential_backend(self) -> None:
        stored = self.vault.save_token_set(_token(), now=self.now)
        self.assertNotIn("ghu_access", repr(stored))
        self.assertNotIn("ghr_refresh", repr(stored))
        self.assertEqual(set(self.backend.values), {self.vault.target})
        loaded = self.vault.load()
        self.assertEqual(loaded, stored)

    def test_fresh_session_returns_access_token_without_refresh(self) -> None:
        self.vault.save_token_set(_token(), now=self.now)
        transport = _Transport({"error": "must-not-be-called"})
        manager = GitHubSessionManager(
            vault=self.vault,
            transport=transport,
            now=lambda: self.now + timedelta(hours=1),
        )
        self.assertEqual(manager.access_token(), "ghu_access")
        self.assertEqual(transport.calls, [])

    def test_expiring_session_rotates_both_tokens_and_persists_new_pair(self) -> None:
        self.vault.save_token_set(_token(), now=self.now)
        transport = _Transport(
            {
                "access_token": "ghu_rotated",
                "refresh_token": "ghr_rotated",
                "token_type": "bearer",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
            }
        )
        manager = GitHubSessionManager(
            vault=self.vault,
            transport=transport,
            now=lambda: self.now + timedelta(hours=7, minutes=59),
        )
        self.assertEqual(manager.access_token(), "ghu_rotated")
        loaded = self.vault.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.access_token, "ghu_rotated")
        self.assertEqual(loaded.refresh_token, "ghr_rotated")
        self.assertNotIn("client_secret", transport.calls[0][1])

    def test_expired_session_without_refresh_requires_login_and_logout_deletes(self) -> None:
        self.vault.save_token_set(_token(refresh=None), now=self.now)
        manager = GitHubSessionManager(
            vault=self.vault,
            transport=_Transport({}),
            now=lambda: self.now + timedelta(hours=9),
        )
        with self.assertRaisesRegex(GitHubSessionError, "login is required"):
            manager.access_token()
        manager.logout()
        self.assertIsNone(self.vault.load())

    @unittest.skipUnless(os.name == "nt", "Windows Credential Manager only")
    def test_windows_backend_can_read_a_missing_generic_credential(self) -> None:
        backend = WindowsCredentialBackend()
        self.assertIsNone(
            backend.read("ARIA-Codex/github/aria-test-credential-that-must-not-exist")
        )


if __name__ == "__main__":
    unittest.main()
