from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aria.codex_integration import _load_profile, _profile_value, _skill_bytes
from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import default_runtime_root


CLAUDE_SKILL_NAME = "aria-project"
SETTINGS_LIMIT = 256 * 1024
HOOK_MATCHER = "Edit|Write|NotebookEdit|Bash|PowerShell"


@dataclass(frozen=True)
class ClaudeIntegrationPaths:
    source_skill: Path
    config_root: Path
    installed_skill: Path
    settings: Path
    provider_profile: Path
    receipt: Path
    lock: Path


def _user_home(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get("USERPROFILE", "").strip() or env.get("HOME", "").strip()
    if not raw:
        raise ConfigurationError("Claude config home is unknown: USERPROFILE or HOME is required")
    home = Path(raw)
    if not home.is_absolute():
        raise ConfigurationError("Claude config home must be an absolute path")
    return home


def claude_integration_paths(
    *,
    environ: dict[str, str] | None = None,
    runtime_root: Path | None = None,
) -> ClaudeIntegrationPaths:
    env = os.environ if environ is None else environ
    home = _user_home(environ)
    configured = env.get("CLAUDE_CONFIG_DIR", "").strip()
    config_root = Path(configured) if configured else home / ".claude"
    if not config_root.is_absolute():
        raise ConfigurationError("CLAUDE_CONFIG_DIR must be an absolute path")
    resolved_config = config_root.resolve(strict=False)
    if not configured and not resolved_config.is_relative_to(home.resolve(strict=False)):
        raise WorkflowError("Claude config root resolves outside the user home")
    runtime = runtime_root or default_runtime_root(environ)
    source = Path(__file__).resolve().parent / "claude_skills" / CLAUDE_SKILL_NAME / "SKILL.md"
    return ClaudeIntegrationPaths(
        source_skill=source,
        config_root=config_root,
        installed_skill=config_root / "skills" / CLAUDE_SKILL_NAME / "SKILL.md",
        settings=config_root / "settings.json",
        provider_profile=runtime / "claude" / "github-provider.json",
        receipt=runtime / "claude" / "integration-receipt.json",
        lock=runtime / "locks" / "claude-integration.lock",
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _bounded_bytes(path: Path, label: str, limit: int = SETTINGS_LIMIT) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"{label} is unreadable") from error
    if len(content) > limit:
        raise ConfigurationError(f"{label} exceeds the safety limit")
    return content


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise WorkflowError("Claude settings must be a regular file")
    try:
        value = json.loads(_bounded_bytes(path, "Claude settings").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Claude settings are not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ConfigurationError("Claude settings root must be a JSON object")
    hooks = value.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise ConfigurationError("Claude settings hooks must be a JSON object")
    if isinstance(hooks, dict):
        pre = hooks.get("PreToolUse")
        if pre is not None and not isinstance(pre, list):
            raise ConfigurationError("Claude PreToolUse hooks must be a JSON array")
    return value


def _hook_entry(python_executable: Path) -> dict[str, object]:
    executable = python_executable.resolve(strict=False)
    if not executable.is_absolute():
        raise ConfigurationError("Claude hook Python executable must be an absolute path")
    return {
        "matcher": HOOK_MATCHER,
        "hooks": [
            {
                "type": "command",
                "command": str(executable),
                "args": ["-I", "-m", "aria.claude_hook"],
                "timeout": 30,
            }
        ],
    }


def _is_aria_hook_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    handlers = entry.get("hooks")
    if not isinstance(handlers, list):
        return False
    for handler in handlers:
        if not isinstance(handler, dict):
            continue
        args = handler.get("args")
        if isinstance(args, list) and args == ["-I", "-m", "aria.claude_hook"]:
            return True
    return False


def _merge_hook(settings: dict[str, Any], entry: dict[str, object], *, replace: bool) -> bool:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ConfigurationError("Claude settings hooks must be a JSON object")
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        raise ConfigurationError("Claude PreToolUse hooks must be a JSON array")
    owned = [item for item in pre if _is_aria_hook_entry(item)]
    if owned == [entry] and len(owned) == 1:
        return False
    if owned and not replace:
        raise WorkflowError("installed ARIA Claude hook differs; rerun with --replace")
    pre[:] = [item for item in pre if not _is_aria_hook_entry(item)]
    pre.append(entry)
    return True


def _remove_hook(settings: dict[str, Any]) -> bool:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        return False
    filtered = [item for item in pre if not _is_aria_hook_entry(item)]
    if len(filtered) == len(pre):
        return False
    if filtered:
        hooks["PreToolUse"] = filtered
    else:
        hooks.pop("PreToolUse", None)
    if not hooks:
        settings.pop("hooks", None)
    return True


def _check_destinations(paths: ClaudeIntegrationPaths) -> None:
    candidates = (
        paths.config_root,
        paths.config_root / "skills",
        paths.installed_skill.parent,
        paths.installed_skill,
        paths.settings,
    )
    if any(path.is_symlink() for path in candidates):
        raise WorkflowError("Claude integration destination must not be a symbolic link")


def install_claude_integration(
    *,
    github_client_id: str | None = None,
    coordinator_integration_id: int | None = None,
    python_executable: Path | None = None,
    replace: bool = False,
    environ: dict[str, str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    if (github_client_id is None) != (coordinator_integration_id is None):
        raise ConfigurationError(
            "GitHub client id and coordinator integration id must be supplied together"
        )
    paths = claude_integration_paths(environ=environ, runtime_root=runtime_root)
    source = _skill_bytes(paths.source_skill, "bundled ARIA Claude skill")
    expected_sha = _sha256(source)
    hook_entry = _hook_entry(python_executable or Path(sys.executable))
    with exclusive_lock(paths.lock):
        _check_destinations(paths)
        current = (
            _bounded_bytes(paths.installed_skill, "installed ARIA Claude skill", 64 * 1024)
            if paths.installed_skill.is_file()
            else None
        )
        if current is not None and _sha256(current) != expected_sha and not replace:
            raise WorkflowError(
                "installed ARIA Claude skill differs; rerun with --replace to overwrite it"
            )
        settings = _load_settings(paths.settings)
        settings_changed = _merge_hook(settings, hook_entry, replace=replace)
        existing_profile = _load_profile(paths.provider_profile)
        profile: dict[str, object] | None = None
        if github_client_id is not None and coordinator_integration_id is not None:
            profile = _profile_value(github_client_id, coordinator_integration_id)
            if existing_profile is not None and existing_profile != profile and not replace:
                raise WorkflowError(
                    "Claude GitHub provider profile differs; rerun with --replace to update it"
                )

        atomic_write_bytes(paths.installed_skill, source)
        if paths.installed_skill.read_bytes() != source:
            raise WorkflowError("ARIA Claude skill read-back failed")
        if settings_changed or not paths.settings.exists():
            atomic_write_bytes(paths.settings, json_bytes(settings))
        verified_settings = _load_settings(paths.settings)
        pre = verified_settings.get("hooks", {}).get("PreToolUse", [])
        if sum(item == hook_entry for item in pre) != 1:
            raise WorkflowError("ARIA Claude hook settings read-back failed")
        profile_updated = False
        if profile is not None:
            atomic_write_bytes(paths.provider_profile, json_bytes(profile))
            if _load_profile(paths.provider_profile) != profile:
                raise WorkflowError("Claude GitHub provider profile read-back failed")
            profile_updated = existing_profile != profile
        receipt = {
            "schema_version": 1,
            "skill_sha256": expected_sha,
            "hook_entry": hook_entry,
            "settings_path": str(paths.settings.resolve(strict=False)),
        }
        atomic_write_bytes(paths.receipt, json_bytes(receipt))
        return {
            "ok": True,
            "skill": CLAUDE_SKILL_NAME,
            "path": str(paths.installed_skill),
            "settings_path": str(paths.settings),
            "sha256": expected_sha,
            "installed": current is None,
            "updated": current is not None and _sha256(current) != expected_sha,
            "hook_installed": True,
            "settings_updated": settings_changed,
            "provider_profile_configured": profile is not None or existing_profile is not None,
            "provider_profile_updated": profile_updated,
            "restart_may_be_required": True,
            "mcp_server": False,
        }


def claude_integration_status(
    *,
    environ: dict[str, str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    paths = claude_integration_paths(environ=environ, runtime_root=runtime_root)
    source = _skill_bytes(paths.source_skill, "bundled ARIA Claude skill")
    expected_sha = _sha256(source)
    installed_sha: str | None = None
    if paths.installed_skill.is_file() and not paths.installed_skill.is_symlink():
        installed_sha = _sha256(
            _bounded_bytes(paths.installed_skill, "installed ARIA Claude skill", 64 * 1024)
        )
    settings = _load_settings(paths.settings)
    pre = settings.get("hooks", {}).get("PreToolUse", [])
    aria_hooks = [item for item in pre if _is_aria_hook_entry(item)]
    receipt = None
    if paths.receipt.is_file():
        try:
            receipt = json.loads(_bounded_bytes(paths.receipt, "Claude integration receipt"))
        except json.JSONDecodeError:
            receipt = None
    expected_entry = receipt.get("hook_entry") if isinstance(receipt, dict) else None
    hook_ok = len(aria_hooks) == 1 and aria_hooks[0] == expected_entry
    profile = _load_profile(paths.provider_profile)
    return {
        "ok": installed_sha == expected_sha and hook_ok,
        "skill": CLAUDE_SKILL_NAME,
        "path": str(paths.installed_skill),
        "settings_path": str(paths.settings),
        "installed": installed_sha is not None,
        "matches_release": installed_sha == expected_sha,
        "expected_sha256": expected_sha,
        "installed_sha256": installed_sha,
        "hook_installed": bool(aria_hooks),
        "hook_matches_receipt": hook_ok,
        "provider_profile_configured": profile is not None,
        "provider": profile,
        "mcp_server": False,
    }


def remove_claude_integration(
    *,
    force: bool = False,
    environ: dict[str, str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    paths = claude_integration_paths(environ=environ, runtime_root=runtime_root)
    source_sha = _sha256(_skill_bytes(paths.source_skill, "bundled ARIA Claude skill"))
    with exclusive_lock(paths.lock):
        _check_destinations(paths)
        if paths.installed_skill.exists():
            if paths.installed_skill.is_symlink() or not paths.installed_skill.is_file():
                raise WorkflowError("ARIA Claude skill destination is not a regular file")
            current_sha = _sha256(
                _bounded_bytes(paths.installed_skill, "installed ARIA Claude skill", 64 * 1024)
            )
            if current_sha != source_sha and not force:
                raise WorkflowError(
                    "installed ARIA Claude skill was modified; use --force to remove it"
                )
        settings = _load_settings(paths.settings)
        hook_removed = _remove_hook(settings)
        if hook_removed:
            atomic_write_bytes(paths.settings, json_bytes(settings))
        removed = False
        if paths.installed_skill.is_file():
            paths.installed_skill.unlink()
            removed = True
            skill_dir = paths.installed_skill.parent
            if skill_dir.is_dir() and not any(skill_dir.iterdir()):
                skill_dir.rmdir()
        if paths.receipt.is_file():
            paths.receipt.unlink()
        return {
            "ok": True,
            "removed": removed,
            "hook_removed": hook_removed,
            "provider_profile_preserved": paths.provider_profile.is_file(),
        }
