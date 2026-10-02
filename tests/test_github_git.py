from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aria.errors import ConfigurationError
from aria.github_git import github_git_environment, resolve_github_askpass


class GitHubGitTests(unittest.TestCase):
    def test_environment_forces_askpass_without_embedding_a_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            askpass = Path(temporary) / "aria-github-askpass.exe"
            askpass.write_bytes(b"test")
            environment = github_git_environment(
                client_id="Iv1.client123",
                askpass=askpass,
            )
        self.assertEqual(environment["ARIA_GITHUB_CLIENT_ID"], "Iv1.client123")
        self.assertEqual(environment["GIT_ASKPASS"], str(askpass))
        self.assertEqual(environment["GIT_ASKPASS_REQUIRE"], "force")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("ARIA_GITHUB_TOKEN", environment)

    def test_invalid_client_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            askpass = Path(temporary) / "aria-github-askpass.exe"
            askpass.write_bytes(b"test")
            with self.assertRaisesRegex(ConfigurationError, "invalid"):
                github_git_environment(client_id="bad client", askpass=askpass)

    def test_explicit_missing_askpass_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.exe"
            with self.assertRaisesRegex(ConfigurationError, "unavailable"):
                resolve_github_askpass(missing)


if __name__ == "__main__":
    unittest.main()
