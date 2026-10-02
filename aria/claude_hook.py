from __future__ import annotations

import json
import re
import sys
from pathlib import PurePath
from typing import Any


MAX_INPUT_BYTES = 256 * 1024
PROTECTED_FILENAMES = frozenset(
    {
        "access.yaml",
        "activity.yaml",
        "aria_team.yaml",
        "backlog.yaml",
        "control.yaml",
        "history.jsonl",
        "project.yaml",
        "state.yaml",
    }
)
FILE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
SHELL_MUTATION_RE = re.compile(
    r"(?i)(?:^|[\s;&|])(?:git\s+)?(?:push|checkout|switch|branch|worktree|"
    r"update-ref|commit|merge|rebase|reset|add|mv|rm|write|set-content|"
    r"add-content|remove-item|move-item|copy-item|del|erase|ren|rename|"
    r"sed|perl|python|py)(?:\s|$)"
)
CONTROL_BRANCH_RE = re.compile(r"(?i)(?:^|[^a-z0-9_-])aria-control(?:$|[^a-z0-9_-])")


def _deny(reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _basename(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    return PurePath(normalized).name.lower()


def evaluate(payload: Any) -> dict[str, object] | None:
    """Return a Claude Code deny response, or None when the operation is allowed."""
    if not isinstance(payload, dict):
        return _deny("ARIA protection could not validate the Claude tool request.")
    if payload.get("hook_event_name") != "PreToolUse":
        return _deny("ARIA protection received an unexpected hook event.")

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if tool_name not in FILE_TOOLS | SHELL_TOOLS or not isinstance(tool_input, dict):
        return _deny("ARIA protection received an unsupported tool request.")

    if tool_name in FILE_TOOLS:
        field = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
        target = tool_input.get(field)
        if not isinstance(target, str) or not target.strip():
            return _deny("ARIA protection could not validate the target path.")
        if _basename(target) in PROTECTED_FILENAMES:
            return _deny(
                "ARIA control files are Coordinator-owned; use the aria CLI instead of editing them directly."
            )
        return None

    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return _deny("ARIA protection could not validate the shell command.")
    lowered = command.lower()
    protected_name_present = any(name in lowered for name in PROTECTED_FILENAMES)
    mutation_present = SHELL_MUTATION_RE.search(command) is not None
    if CONTROL_BRANCH_RE.search(command) and mutation_present:
        return _deny(
            "Direct mutation of the aria-control branch is blocked; use the local Coordinator."
        )
    if protected_name_present and mutation_present:
        return _deny(
            "Direct mutation of ARIA control files is blocked; use the aria CLI and Coordinator."
        )
    return None


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            result = _deny("ARIA protection rejected an oversized tool request.")
        else:
            result = evaluate(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
        result = _deny("ARIA protection could not parse the Claude tool request.")
    if result is not None:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
