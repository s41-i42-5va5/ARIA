from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from aria.cli import build_parser
from aria.errors import ConfigurationError
from aria.github_auth import GitHubTokenSet
from aria.github_login import GitHubLoginService


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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post_form(self, url: str, fields: dict[str, str]) -> dict[str, object]:
        self.calls.append((url, fields))
        return {
            "device_code": "device-secret",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }


class GitHubLoginServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _Backend()
        self.transport = _Transport()
        self.now = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
        self.service = GitHubLoginService(
            client_id="Iv1.client123",
            backend=self.backend,
            transport=self.transport,
            now=lambda: self.now,
        )

    def test_begin_returns_only_browser_handoff_and_stores_device_secret(self) -> None:
        result = self.service.begin(repository_id="123456789")
        self.assertEqual(result["verification_uri"], "https://github.com/login/device")
        self.assertEqual(result["user_code"], "ABCD-EFGH")
        self.assertEqual(result["browser_handoff"], "codex-in-app-required")
        self.assertNotIn("device-secret", str(result))
        self.assertEqual(len(self.backend.values), 1)
        self.assertIn(b"device-secret", next(iter(self.backend.values.values())))

    def test_complete_moves_pending_code_to_token_vault_and_status_is_safe(self) -> None:
        self.service.begin(repository_id="123456789")
        token = GitHubTokenSet(
            access_token="ghu_access-secret",
            refresh_token="ghr_refresh-secret",
            expires_in=28800,
            refresh_token_expires_in=15897600,
        )
        with mock.patch(
            "aria.github_login.complete_device_authorization", return_value=token
        ):
            result = self.service.complete(repository_id="123456789")
        self.assertTrue(result["logged_in"])
        self.assertNotIn("secret", str(result))
        self.assertEqual(len(self.backend.values), 1)
        status = self.service.status()
        self.assertTrue(status["logged_in"])
        self.assertNotIn("secret", str(status))
        logged_out = self.service.logout(repository_id="123456789")
        self.assertFalse(logged_out["logged_in"])
        self.assertEqual(self.backend.values, {})

    def test_expired_pending_login_is_deleted(self) -> None:
        self.service.begin(repository_id="123456789")
        self.now += timedelta(minutes=16)
        with self.assertRaisesRegex(ConfigurationError, "expired"):
            self.service.complete(repository_id="123456789")
        self.assertEqual(self.backend.values, {})

    def test_public_cli_parses_auth_actions_without_actor_id(self) -> None:
        parser = build_parser()
        begin = parser.parse_args(
            [
                "github-auth",
                "login-begin",
                "--client-id",
                "Iv1.client123",
                "--repository-id",
                "123456789",
            ]
        )
        self.assertEqual(begin.command, "github-auth")
        self.assertIsNone(begin.identity_actor)
        status = parser.parse_args(
            ["github-auth", "status", "--client-id", "Iv1.client123"]
        )
        self.assertEqual(status.github_auth_action, "status")


if __name__ == "__main__":
    unittest.main()
