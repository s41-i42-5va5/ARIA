from __future__ import annotations

import unittest
from datetime import UTC, datetime

from aria.github_askpass import askpass_response
from aria.github_auth import GitHubTokenSet
from aria.github_session import GitHubCredentialVault


class _Backend:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def read(self, target: str) -> bytes | None:
        return self.values.get(target)

    def write(self, target: str, secret: bytes) -> None:
        self.values[target] = secret

    def delete(self, target: str) -> None:
        self.values.pop(target, None)


class GitHubAskPassTests(unittest.TestCase):
    def test_username_is_static_and_password_comes_from_session_vault(self) -> None:
        backend = _Backend()
        vault = GitHubCredentialVault(backend=backend, client_id="Iv1.client123")
        vault.save_token_set(
            GitHubTokenSet(
                access_token="ghu_private_token",
                token_type="bearer",
            ),
            now=datetime(2026, 8, 26, tzinfo=UTC),
        )
        self.assertEqual(
            askpass_response(
                "Username for 'https://github.com':",
                client_id="Iv1.client123",
                credential_backend=backend,
            ),
            "x-access-token",
        )
        self.assertEqual(
            askpass_response(
                "Password for 'https://x-access-token@github.com':",
                client_id="Iv1.client123",
                credential_backend=backend,
            ),
            "ghu_private_token",
        )


if __name__ == "__main__":
    unittest.main()
