from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aria.claude_integration import (
    claude_integration_paths,
    claude_integration_status,
    install_claude_integration,
    remove_claude_integration,
)
from aria.cli import build_parser
from aria.errors import ConfigurationError, WorkflowError


class ClaudeIntegrationTests(unittest.TestCase):
    def _roots(self, temporary: str) -> tuple[dict[str, str], Path, Path]:
        base = Path(temporary)
        home = base / "home"
        runtime = base / "runtime"
        python = base / "runtime" / "venv" / "Scripts" / "python.exe"
        home.mkdir()
        python.parent.mkdir(parents=True)
        python.write_bytes(b"synthetic executable")
        return {"USERPROFILE": str(home)}, runtime, python

    def test_install_is_idempotent_and_preserves_unrelated_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environ, runtime, python = self._roots(temporary)
            paths = claude_integration_paths(environ=environ, runtime_root=runtime)
            paths.settings.parent.mkdir(parents=True)
            original_hook = {
                "matcher": "Read",
                "hooks": [{"type": "command", "command": "audit.exe"}],
            }
            paths.settings.write_text(
                json.dumps(
                    {"model": "sonnet", "hooks": {"PreToolUse": [original_hook]}},
                    indent=2,
                ),
                encoding="utf-8",
            )

            first = install_claude_integration(
                environ=environ,
                runtime_root=runtime,
                python_executable=python,
                github_client_id="Iv1.client123",
                coordinator_integration_id=9001,
            )
            second = install_claude_integration(
                environ=environ,
                runtime_root=runtime,
                python_executable=python,
                github_client_id="Iv1.client123",
                coordinator_integration_id=9001,
            )
            status = claude_integration_status(environ=environ, runtime_root=runtime)
            settings = json.loads(paths.settings.read_text(encoding="utf-8"))

            self.assertTrue(first["installed"])
            self.assertFalse(second["installed"])
            self.assertFalse(second["settings_updated"])
            self.assertTrue(status["ok"])
            self.assertFalse(status["mcp_server"])
            self.assertEqual(settings["model"], "sonnet")
            self.assertEqual(settings["hooks"]["PreToolUse"][0], original_hook)
            self.assertEqual(len(settings["hooks"]["PreToolUse"]), 2)
            self.assertNotIn("token", paths.provider_profile.read_text(encoding="utf-8").lower())

    def test_skill_and_hook_drift_require_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environ, runtime, python = self._roots(temporary)
            install_claude_integration(
                environ=environ, runtime_root=runtime, python_executable=python
            )
            paths = claude_integration_paths(environ=environ, runtime_root=runtime)
            paths.installed_skill.write_text("modified\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "skill differs"):
                install_claude_integration(
                    environ=environ, runtime_root=runtime, python_executable=python
                )
            with self.assertRaisesRegex(WorkflowError, "was modified"):
                remove_claude_integration(environ=environ, runtime_root=runtime)
            result = install_claude_integration(
                environ=environ,
                runtime_root=runtime,
                python_executable=python,
                replace=True,
            )
            self.assertTrue(result["updated"])

            settings = json.loads(paths.settings.read_text(encoding="utf-8"))
            aria_hook = settings["hooks"]["PreToolUse"][-1]
            aria_hook["matcher"] = "Write"
            paths.settings.write_text(json.dumps(settings), encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "hook differs"):
                install_claude_integration(
                    environ=environ, runtime_root=runtime, python_executable=python
                )

    def test_remove_only_aria_entries_and_preserves_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environ, runtime, python = self._roots(temporary)
            install_claude_integration(
                environ=environ,
                runtime_root=runtime,
                python_executable=python,
                github_client_id="Iv1.client123",
                coordinator_integration_id=9001,
            )
            paths = claude_integration_paths(environ=environ, runtime_root=runtime)
            settings = json.loads(paths.settings.read_text(encoding="utf-8"))
            settings["permissions"] = {"allow": ["Read"]}
            settings["hooks"]["PreToolUse"].insert(
                0,
                {"matcher": "Read", "hooks": [{"type": "command", "command": "audit.exe"}]},
            )
            paths.settings.write_text(json.dumps(settings), encoding="utf-8")

            removed = remove_claude_integration(environ=environ, runtime_root=runtime)
            after = json.loads(paths.settings.read_text(encoding="utf-8"))
            self.assertTrue(removed["removed"])
            self.assertTrue(removed["hook_removed"])
            self.assertTrue(removed["provider_profile_preserved"])
            self.assertEqual(after["permissions"], {"allow": ["Read"]})
            self.assertEqual(len(after["hooks"]["PreToolUse"]), 1)

    def test_claude_config_dir_must_be_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environ, runtime, _ = self._roots(temporary)
            environ["CLAUDE_CONFIG_DIR"] = "relative"
            with self.assertRaisesRegex(ConfigurationError, "absolute"):
                claude_integration_paths(environ=environ, runtime_root=runtime)

    def test_public_cli_exposes_claude_lifecycle(self) -> None:
        parser = build_parser()
        install = parser.parse_args(
            [
                "claude",
                "install",
                "--github-client-id",
                "Iv1.client123",
                "--coordinator-integration-id",
                "9001",
            ]
        )
        status = parser.parse_args(["claude", "status"])
        remove = parser.parse_args(["claude", "remove", "--force"])
        self.assertEqual(install.claude_action, "install")
        self.assertEqual(status.claude_action, "status")
        self.assertTrue(remove.force)
        self.assertIn("claude", parser.format_help())


if __name__ == "__main__":
    unittest.main()
