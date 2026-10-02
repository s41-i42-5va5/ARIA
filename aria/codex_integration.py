from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from aria.errors import ConfigurationError, WorkflowError
from aria.github_auth import CLIENT_ID_RE
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import default_runtime_root


CODEX_SKILL_NAME = "aria-project"
CODEX_PROFILE_SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CodexIntegrationPaths:
    source_skill: Path
    user_skill_root: Path
    installed_skill: Path
    provider_profile: Path
    lock: Path


def _user_home(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get("USERPROFILE", "").strip() or env.get("HOME", "").strip()
    if not raw:
        raise ConfigurationError("Codex skill home is unknown: USERPROFILE or HOME is required")
    path = Path(raw)
    if not path.is_absolute():
        raise ConfigurationError("Codex skill home must be an absolute path")
    return path


def codex_integration_paths(
    *,
    environ: dict[str, str] | None = None,
    runtime_root: Path | None = None,
) -> CodexIntegrationPaths:
    home = _user_home(environ)
    user_skill_root = home / ".agents" / "skills"
    source = Path(__file__).resolve().parent / "codex_skills" / CODEX_SKILL_NAME / "SKILL.md"
    runtime = runtime_root or default_runtime_root(environ)
    resolved_home = home.resolve(strict=False)
    resolved_skill_root = user_skill_root.resolve(strict=False)
    if not resolved_skill_root.is_relative_to(resolved_home):
        raise WorkflowError("Codex skill root resolves outside the user home")
    return CodexIntegrationPaths(
        source_skill=source,
        user_skill_root=user_skill_root,
        installed_skill=user_skill_root / CODEX_SKILL_NAME / "SKILL.md",
        provider_profile=runtime / "codex" / "github-provider.json",
        lock=runtime / "locks" / "codex-integration.lock",
    )


def _bounded_bytes(path: Path, label: str) -> bytes:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"{label} is unreadable") from error
    if len(content) > 64 * 1024:
        raise ConfigurationError(f"{label} exceeds the safety limit")
    return content


def _skill_bytes(path: Path, label: str) -> bytes:
    content = _bounded_bytes(path, label)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"{label} is not UTF-8") from error
    metadata_text = text.replace("\r\n", "\n")
    if not metadata_text.startswith("---\n") or "\nname: aria-project\n" not in metadata_text:
        raise ConfigurationError(f"{label} metadata is invalid")
    return content


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _profile_value(client_id: str, integration_id: int) -> dict[str, object]:
    if CLIENT_ID_RE.fullmatch(client_id) is None:
        raise ConfigurationError("GitHub App client id is invalid")
    if type(integration_id) is not int or not 1 <= integration_id <= 2**63 - 1:
        raise ConfigurationError("GitHub App integration id is invalid")
    return {
        "schema_version": CODEX_PROFILE_SCHEMA_VERSION,
        "provider": "github",
        "github_client_id": client_id,
        "coordinator_integration_id": integration_id,
    }


def _load_profile(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Codex GitHub provider profile is unreadable") from error
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "provider",
        "github_client_id",
        "coordinator_integration_id",
    }:
        raise ConfigurationError("Codex GitHub provider profile is invalid")
    expected = _profile_value(
        str(value.get("github_client_id")),
        value.get("coordinator_integration_id"),
    )
    if value != expected:
        raise ConfigurationError("Codex GitHub provider profile is invalid")
    return value


def install_codex_integration(
    *,
    github_client_id: str | None = None,
    coordinator_integration_id: int | None = None,
    replace: bool = False,
    environ: dict[str, str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    if (github_client_id is None) != (coordinator_integration_id is None):
        raise ConfigurationError(
            "GitHub client id and coordinator integration id must be supplied together"
        )
    paths = codex_integration_paths(environ=environ, runtime_root=runtime_root)
    source = _skill_bytes(paths.source_skill, "bundled ARIA Codex skill")
    expected_sha = _sha256(source)
    with exclusive_lock(paths.lock):
        skill_parents = (
            paths.user_skill_root.parent,
            paths.user_skill_root,
            paths.installed_skill.parent,
        )
        if any(path.is_symlink() for path in skill_parents) or paths.installed_skill.is_symlink():
            raise WorkflowError("Codex skill destination must not be a symbolic link")
        current = (
            _bounded_bytes(paths.installed_skill, "installed ARIA Codex skill")
            if paths.installed_skill.is_file()
            else None
        )
        if current is not None and _sha256(current) != expected_sha and not replace:
            raise WorkflowError(
                "installed ARIA Codex skill differs; rerun with --replace to overwrite it"
            )
        profile: dict[str, object] | None = None
        existing_profile = _load_profile(paths.provider_profile)
        if github_client_id is not None and coordinator_integration_id is not None:
            profile = _profile_value(github_client_id, coordinator_integration_id)
            if existing_profile is not None and existing_profile != profile and not replace:
                raise WorkflowError(
                    "Codex GitHub provider profile differs; rerun with --replace to update it"
                )
        atomic_write_bytes(paths.installed_skill, source)
        if paths.installed_skill.read_bytes() != source:
            raise WorkflowError("ARIA Codex skill read-back failed")
        profile_updated = False
        if profile is not None:
            atomic_write_bytes(paths.provider_profile, json_bytes(profile))
            if _load_profile(paths.provider_profile) != profile:
                raise WorkflowError("Codex GitHub provider profile read-back failed")
            profile_updated = existing_profile != profile
        return {
            "ok": True,
            "skill": CODEX_SKILL_NAME,
            "path": str(paths.installed_skill),
            "sha256": expected_sha,
            "installed": current is None,
            "updated": current is not None and _sha256(current) != expected_sha,
            "provider_profile_configured": (
                profile is not None or existing_profile is not None
            ),
            "provider_profile_updated": profile_updated,
            "restart_may_be_required": True,
        }


def codex_integration_status(
    *,
    environ: dict[str, str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    paths = codex_integration_paths(environ=environ, runtime_root=runtime_root)
    source = _skill_bytes(paths.source_skill, "bundled ARIA Codex skill")
    expected_sha = _sha256(source)
    installed_sha: str | None = None
    if paths.installed_skill.is_file():
        installed_sha = _sha256(
            _bounded_bytes(paths.installed_skill, "installed ARIA Codex skill")
        )
    profile = _load_profile(paths.provider_profile)
    return {
        "ok": installed_sha == expected_sha,
        "skill": CODEX_SKILL_NAME,
        "path": str(paths.installed_skill),
        "installed": installed_sha is not None,
        "matches_release": installed_sha == expected_sha,
        "expected_sha256": expected_sha,
        "installed_sha256": installed_sha,
        "provider_profile_configured": profile is not None,
        "provider": profile,
    }


def remove_codex_integration(
    *,
    force: bool = False,
    environ: dict[str, str] | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    paths = codex_integration_paths(environ=environ, runtime_root=runtime_root)
    source_sha = _sha256(_skill_bytes(paths.source_skill, "bundled ARIA Codex skill"))
    with exclusive_lock(paths.lock):
        if not paths.installed_skill.exists():
            return {"ok": True, "removed": False, "reason": "not_installed"}
        if paths.installed_skill.is_symlink() or not paths.installed_skill.is_file():
            raise WorkflowError("ARIA Codex skill destination is not a regular file")
        current_sha = _sha256(
            _bounded_bytes(paths.installed_skill, "installed ARIA Codex skill")
        )
        if current_sha != source_sha and not force:
            raise WorkflowError(
                "installed ARIA Codex skill was modified; use --force to remove it"
            )
        paths.installed_skill.unlink()
        skill_dir = paths.installed_skill.parent
        if skill_dir.is_dir() and not any(skill_dir.iterdir()):
            skill_dir.rmdir()
        return {
            "ok": True,
            "removed": True,
            "provider_profile_preserved": paths.provider_profile.is_file(),
        }
