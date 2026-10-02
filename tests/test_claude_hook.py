from __future__ import annotations

import unittest

from aria.claude_hook import evaluate


def payload(tool: str, tool_input: object) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }


class ClaudeHookTests(unittest.TestCase):
    def assert_denied(self, value: object) -> None:
        self.assertEqual(
            value["hookSpecificOutput"]["permissionDecision"],  # type: ignore[index]
            "deny",
        )

    def test_denies_direct_control_file_writes_case_insensitively(self) -> None:
        for tool, field, target in (
            ("Write", "file_path", r"C:\repo\aria-docs\STATE.yaml"),
            ("Edit", "file_path", r"C:\repo\BACKLOG.YAML"),
            ("NotebookEdit", "notebook_path", r"C:\repo\HISTORY.jsonl"),
        ):
            with self.subTest(tool=tool, target=target):
                self.assert_denied(evaluate(payload(tool, {field: target})))

    def test_allows_normal_source_edits_and_control_reads(self) -> None:
        self.assertIsNone(
            evaluate(payload("Edit", {"file_path": r"C:\repo\src\service.py"}))
        )
        self.assertIsNone(
            evaluate(payload("PowerShell", {"command": "Get-Content .\\STATE.yaml"}))
        )
        self.assertIsNone(
            evaluate(payload("Bash", {"command": "git log origin/aria-control -n 3"}))
        )

    def test_denies_shell_mutation_of_control_plane(self) -> None:
        commands = (
            "git push origin HEAD:aria-control",
            "git switch aria-control",
            "Set-Content -Path STATE.yaml -Value x",
            "python -c \"open('BACKLOG.yaml','w').write('x')\"",
            "rm aria-docs/ARIA_TEAM.yaml",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assert_denied(evaluate(payload("PowerShell", {"command": command})))

    def test_malformed_and_unexpected_requests_fail_closed_without_echo(self) -> None:
        secret = "ghp_secret-must-not-leak"
        results = (
            evaluate(None),
            evaluate({"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {}}),
            evaluate(payload("Write", {})),
            evaluate(payload("Bash", {"command": secret})),
        )
        for result in results[:3]:
            self.assert_denied(result)
            self.assertNotIn(secret, str(result))
        self.assertIsNone(results[3])


if __name__ == "__main__":
    unittest.main()
