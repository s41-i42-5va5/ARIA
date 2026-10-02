from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from aria.github_app import (
    GitHubAppCredentialVault,
    GitHubAppError,
    GitHubInstallationSession,
    configure_github_app_key,
    create_app_jwt,
    github_app_key_status,
    remove_github_app_key,
)


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
    def __init__(self, expires_at: str) -> None:
        self.expires_at = expires_at
        self.installation_calls: list[tuple[str, str]] = []
        self.token_calls: list[tuple[int, int, str]] = []

    def get_repository_installation(self, *, repository_path: str, app_jwt: str):
        self.installation_calls.append((repository_path, app_jwt))
        return {"id": 7001, "app_id": 9001}

    def create_installation_token(
        self, *, installation_id: int, repository_id: int, app_jwt: str
    ):
        self.token_calls.append((installation_id, repository_id, app_jwt))
        return {"token": "ghs_installation_secret", "expires_at": self.expires_at}


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class GitHubAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def setUp(self) -> None:
        self.backend = _Backend()
        self.vault = GitHubAppCredentialVault(backend=self.backend, app_id=9001)
        self.now = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)

    def test_private_key_vault_validates_and_never_returns_raw_bytes(self) -> None:
        self.vault.save_private_key(self.pem)
        self.assertTrue(self.vault.configured())
        loaded = self.vault.load_private_key()
        self.assertIsInstance(loaded, rsa.RSAPrivateKey)
        self.assertEqual(set(self.backend.values), {self.vault.target})
        with self.assertRaisesRegex(Exception, "private key is invalid"):
            self.vault.save_private_key(b"not-a-private-key")
        self.vault.delete()
        self.assertFalse(self.vault.configured())

    def test_app_jwt_has_bounded_claims_and_valid_signature(self) -> None:
        token = create_app_jwt(app_id=9001, private_key=self.private_key, now=self.now)
        header, payload, signature = token.split(".")
        self.assertEqual(json.loads(_decode(header)), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(_decode(payload))
        self.assertEqual(claims["iss"], "9001")
        self.assertEqual(claims["iat"], int(self.now.timestamp()) - 60)
        self.assertEqual(claims["exp"], int(self.now.timestamp()) + 540)
        self.private_key.public_key().verify(
            _decode(signature),
            f"{header}.{payload}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_installation_token_is_repository_scoped_cached_and_hidden(self) -> None:
        self.vault.save_private_key(self.pem)
        transport = _Transport("2026-08-26T16:00:00Z")
        session = GitHubInstallationSession(
            app_id=9001,
            repository_id=123456789,
            repository_path="/repos/acme/product",
            vault=self.vault,
            transport=transport,
            now=lambda: self.now,
        )
        self.assertEqual(session.access_token(), "ghs_installation_secret")
        self.assertEqual(session.access_token(), "ghs_installation_secret")
        self.assertEqual(len(transport.installation_calls), 1)
        self.assertEqual(len(transport.token_calls), 1)
        self.assertEqual(transport.token_calls[0][:2], (7001, 123456789))
        self.assertNotIn("ghs_installation_secret", repr(session._cached))

    def test_expiring_token_rotates_and_missing_key_fails_safely(self) -> None:
        transport = _Transport("2026-08-26T16:00:00Z")
        missing = GitHubInstallationSession(
            app_id=9001,
            repository_id=123456789,
            repository_path="/repos/acme/product",
            vault=self.vault,
            transport=transport,
            now=lambda: self.now,
        )
        with self.assertRaisesRegex(GitHubAppError, "not configured"):
            missing.access_token()

        self.vault.save_private_key(self.pem)
        moments = iter([self.now, self.now + timedelta(minutes=56)])
        session = GitHubInstallationSession(
            app_id=9001,
            repository_id=123456789,
            repository_path="/repos/acme/product",
            vault=self.vault,
            transport=transport,
            now=lambda: next(moments),
        )
        session.access_token()
        transport.expires_at = "2026-08-26T17:00:00Z"
        session.access_token()
        self.assertEqual(len(transport.token_calls), 2)

    def test_configure_status_remove_return_only_public_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.pem"
            path.write_bytes(self.pem)
            configured = configure_github_app_key(
                backend=self.backend, app_id=9001, private_key_path=path
            )
        self.assertTrue(configured["configured"])
        self.assertEqual(len(configured["public_key_sha256"]), 64)
        self.assertNotIn("PRIVATE KEY", json.dumps(configured))
        status = github_app_key_status(backend=self.backend, app_id=9001)
        self.assertEqual(status, configured)
        removed = remove_github_app_key(backend=self.backend, app_id=9001)
        self.assertTrue(removed["removed"])
        self.assertFalse(
            github_app_key_status(backend=self.backend, app_id=9001)["configured"]
        )


if __name__ == "__main__":
    unittest.main()
