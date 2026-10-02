from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aria.cli import build_parser
from aria.codex_integration import (
    codex_integration_paths,
    codex_integration_status,
    install_codex_integration,
    remove_codex_integration,
)
from aria.errors import WorkflowError


class CodexIntegrationTests(unittest.TestCase):
    def _roots(self, temporary: str) -> tuple[dict[str, str], Path]:
        base = Path(temporary)
        home = base / "home"
        runtime = base / "runtime"
        home.mkdir()
        return {"USERPROFILE": str(home)}, runtime

    def test_install_is_idempotent_and_status_verifies_exact_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environ, runtime = self._roots(temporary)
            first = install_codex_integration(
                environ=environ,
                runtime_root=runtime,
                github_client_id="Iv1.client123",
                coordinator_integration_id=9001,
            )
            second = install_codex_integration(
                environ=environ,
                runtime_root=runtime,
                github_client_id="Iv1.client123",
                coordinator_integration_id=9001,
            )
            status = codex_integration_status(
                environ=environ, runtime_root=runtime
            )

            self.assertTrue(first["installed"])
            self.assertFalse(second["installed"])
            self.assertFalse(second["updated"])
            self.assertTrue(status["ok"])
            self.assertTrue(status["matches_release"])
            self.assertEqual(
                status["provider"]["coordinator_integration_id"], 9001
            )
            paths = codex_integration_paths(
                environ=environ, runtime_root=runtime
            )
            profile = json.loads(paths.provider_profile.read_text(encoding="utf-8"))
            self.assertNotIn("token", json.dumps(profile).lower())

    def test_drift_requires_explicit_replace_and_safe_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environ, runtime = self._roots(temporary)
            install_codex_integration(environ=environ, runtime_root=runtime)
            paths = codex_integration_paths(
                environ=environ, runtime_root=runtime
            )
            paths.installed_skill.write_text("user modification\n", encoding="utf-8")

            with self.assertRaisesRegex(WorkflowError, "differs"):
                install_codex_integration(environ=environ, runtime_root=runtime)
            with self.assertRaisesRegex(WorkflowError, "modified"):
                remove_codex_integration(environ=environ, runtime_root=runtime)

            replaced = install_codex_integration(
                environ=environ, runtime_root=runtime, replace=True
            )
            removed = remove_codex_integration(
                environ=environ, runtime_root=runtime
            )
            self.assertTrue(replaced["updated"])
            self.assertTrue(removed["removed"])
            self.assertFalse(paths.installed_skill.exists())

    def test_provider_profile_change_requires_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environ, runtime = self._roots(temporary)
            install_codex_integration(
                environ=environ,
                runtime_root=runtime,
                github_client_id="Iv1.client123",
                coordinator_integration_id=9001,
            )
            with self.assertRaisesRegex(WorkflowError, "provider profile differs"):
                install_codex_integration(
                    environ=environ,
                    runtime_root=runtime,
                    github_client_id="Iv1.client456",
                    coordinator_integration_id=9002,
                )
            paths = codex_integration_paths(
                environ=environ, runtime_root=runtime
            )
            unchanged = json.loads(paths.provider_profile.read_text(encoding="utf-8"))
            self.assertEqual(unchanged["coordinator_integration_id"], 9001)
            updated = install_codex_integration(
                environ=environ,
                runtime_root=runtime,
                github_client_id="Iv1.client456",
                coordinator_integration_id=9002,
                replace=True,
            )
            self.assertTrue(updated["provider_profile_updated"])

    def test_public_cli_exposes_codex_lifecycle(self) -> None:
        parser = build_parser()
        install = parser.parse_args(
            [
                "codex",
                "install",
                "--github-client-id",
                "Iv1.client123",
                "--coordinator-integration-id",
                "9001",
            ]
        )
        status = parser.parse_args(["codex", "status"])
        remove = parser.parse_args(["codex", "remove", "--force"])
        self.assertEqual(install.codex_action, "install")
        self.assertEqual(status.codex_action, "status")
        self.assertTrue(remove.force)
        self.assertIn("codex", parser.format_help())


if __name__ == "__main__":
    unittest.main()
