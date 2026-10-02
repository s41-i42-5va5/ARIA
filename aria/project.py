from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from aria import __version__
from aria.errors import ConfigurationError, WorkflowError
from aria.integrity import engine_state
from aria.lessons import LessonStore
from aria.project_state import (
    STATE_PROFILES,
    iter_stage_tasks,
    parse_project_state,
    projection_yaml,
    select_next_task,
    validate_state_model,
)

PROJECT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
DEFAULT_GIT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class ProjectFiles:
    state: str
    stack: str
    history: str
    system_map: str
    specs: str
    adr: str
    knowledge: str
    verification: str | None
    team: str | None
    trust: str | None
    access: str | None
    backlog: str | None


@dataclass(frozen=True)
class ProjectGovernance:
    status_authority: str
    require_active_run_for_writes: bool
    decision_registry: str | None


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    display_name: str
    framework_version: str
    mode: str
    activation_engine_sha256: str | None
    framework_root: Path
    docs_root: Path
    code_root: Path
    runtime_root: Path
    registry_path: Path
    project_path: Path
    files: ProjectFiles
    governance: ProjectGovernance | None
    state_profile: str
    stack_manifests: tuple[str, ...]
    git_ignore_prefixes: tuple[str, ...]
    context_budget_bytes: int
    state_budget_bytes: int
    state_projection_budget_bytes: int
    collaboration_mode: str = "offline"

    def document_path(self, relative: str) -> Path:
        normalized = safe_relative_path(relative)
        candidate = self.docs_root.joinpath(*PurePosixPath(normalized).parts)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.docs_root.resolve(strict=True)):
            raise ConfigurationError(f"Project document escapes docs root: {relative}")
        return candidate

    def code_path(self, relative: str) -> Path:
        normalized = safe_relative_path(relative)
        candidate = self.code_root.joinpath(*PurePosixPath(normalized).parts)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.code_root.resolve(strict=True)):
            raise ConfigurationError(f"Project code path escapes code root: {relative}")
        return candidate


def safe_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    pure = PurePosixPath(normalized)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    invalid_part = any(
        ":" in part
        or part.endswith((".", " "))
        or part.split(".", 1)[0].upper() in reserved
        for part in pure.parts
    )
    if not normalized or pure.is_absolute() or ".." in pure.parts or invalid_part:
        raise ConfigurationError(f"Unsafe project-relative path: {value!r}")
    return pure.as_posix()


def _normalized_python_source(path: Path) -> bytes:
    """Compare source across Git/archive/Windows checkouts without weakening content checks."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def _framework_root(project_root: Path | None) -> Path:
    actual = Path(__file__).resolve().parents[1]
    if project_root is None:
        return actual
    try:
        requested = project_root.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            f"Framework root is unreadable: {project_root}"
        ) from error
    actual = actual.resolve(strict=True)
    if requested == actual:
        return actual
    # A wheel executes from site-packages while the operational framework root
    # still owns .aria-root, skills, policy and engine identity. Permit that
    # split only when every installed Python module exactly matches the
    # requested framework source. Source/editable execution keeps the stricter
    # single-root rule.
    if not (actual / ".aria-root").is_file() and (requested / ".aria-root").is_file():
        installed_package = actual / "aria"
        requested_package = requested / "aria"
        try:
            installed_files = {
                path.relative_to(installed_package).as_posix(): path
                for path in installed_package.rglob("*.py")
                if path.is_file() and "__pycache__" not in path.parts
            }
            requested_files = {
                path.relative_to(requested_package).as_posix(): path
                for path in requested_package.rglob("*.py")
                if path.is_file() and "__pycache__" not in path.parts
            }
            if installed_files and installed_files.keys() == requested_files.keys() and all(
                _normalized_python_source(installed_files[relative])
                == _normalized_python_source(requested_files[relative])
                for relative in installed_files
            ):
                return requested
        except OSError as error:
            raise ConfigurationError(
                f"Cannot verify installed ARIA modules against framework root: {requested}"
            ) from error
    if requested != actual:
        raise ConfigurationError(
            f"Project-aware commands must use the running ARIA framework: {actual}"
        )
    return requested


def default_runtime_root(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("ARIA_RUNTIME_ROOT", "").strip()
    if explicit:
        explicit_path = Path(explicit)
        if not explicit_path.is_absolute():
            raise ConfigurationError("ARIA_RUNTIME_ROOT must be an absolute path")
        return explicit_path
    user_profile = env.get("USERPROFILE", "").strip()
    if user_profile:
        return Path(user_profile) / "AppData" / "Local" / "ARIA-Codex"
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "ARIA-Codex"
    raise ConfigurationError(
        "ARIA runtime is unknown: set ARIA_RUNTIME_ROOT or USERPROFILE"
    )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"{label} contains a non-string key")
    return value


def _required_string(table: dict[str, object], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label}.{key} is missing")
    return value.strip()


def _read_yaml_mapping(path: Path, label: str) -> dict[str, object]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot read {label}: {path}: {error}") from error
    return _mapping(value, label)


def load_project(
    project_id: str,
    *,
    framework_root: Path | None = None,
    runtime_root: Path | None = None,
    registry_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProjectConfig:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    root = _framework_root(framework_root)
    machine_runtime = (
        runtime_root.absolute()
        if runtime_root is not None
        else default_runtime_root(environ)
    )
    registry = registry_path or machine_runtime / "projects.toml"
    if not registry.is_file():
        raise ConfigurationError(f"Project registry not found: {registry}")
    try:
        with registry.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(
            f"Project registry is unreadable: {registry}"
        ) from error
    if raw.get("schema_version") != 1:
        raise ConfigurationError(
            f"Unsupported project registry schema_version: {raw.get('schema_version')!r}"
        )
    projects = _mapping(raw.get("projects"), "projects")
    entry = _mapping(projects.get(project_id), f"projects.{project_id}")
    docs_root = Path(_required_string(entry, "docs_root", f"projects.{project_id}"))
    code_root = Path(_required_string(entry, "code_root", f"projects.{project_id}"))
    if not docs_root.is_absolute() or not code_root.is_absolute():
        raise ConfigurationError(
            f"Project roots must be absolute: projects.{project_id}"
        )
    mode = str(entry.get("mode", "shadow")).strip().lower()
    if mode not in {"shadow", "active"}:
        raise ConfigurationError(f"Unsupported project mode: {mode!r}")
    activation_engine_sha256 = entry.get("engine_sha256")
    if activation_engine_sha256 is not None and (
        not isinstance(activation_engine_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", activation_engine_sha256) is None
    ):
        raise ConfigurationError(
            f"projects.{project_id}.engine_sha256 must be a lowercase SHA-256"
        )
    project_path = docs_root / "PROJECT.yaml"
    if not project_path.is_file():
        raise ConfigurationError(f"PROJECT.yaml not found: {project_path}")
    project = _read_yaml_mapping(project_path, "PROJECT.yaml")
    if project.get("schema_version") != 1:
        raise ConfigurationError(
            f"Unsupported PROJECT.yaml schema_version: {project.get('schema_version')!r}"
        )
    if project.get("project_id") != project_id:
        raise ConfigurationError(
            f"Project identity mismatch: registry={project_id!r}, document={project.get('project_id')!r}"
        )
    documents = _mapping(project.get("documents"), "PROJECT.yaml.documents")
    context = _mapping(project.get("context", {}), "PROJECT.yaml.context")

    def document(key: str) -> str:
        return safe_relative_path(
            _required_string(documents, key, "PROJECT.yaml.documents")
        )

    try:
        context_budget = int(context.get("default_budget_bytes", 131072))
        state_budget = int(context.get("state_budget_bytes", 32768))
        state_projection_budget = int(
            context.get("state_projection_budget_bytes", min(state_budget, 32768))
        )
    except (TypeError, ValueError) as error:
        raise ConfigurationError(
            "PROJECT.yaml context budgets must be integers"
        ) from error
    if context_budget <= 0 or state_budget <= 0 or state_projection_budget <= 0:
        raise ConfigurationError("PROJECT.yaml context budgets must be positive")
    state_profile = str(project.get("state_profile", "frontier")).strip().lower()
    if state_profile not in STATE_PROFILES:
        raise ConfigurationError(
            f"PROJECT.yaml state_profile must be one of {sorted(STATE_PROFILES)}"
        )
    raw_manifests = context.get("stack_manifests")
    if (
        not isinstance(raw_manifests, list)
        or not raw_manifests
        or not all(isinstance(item, str) and item.strip() for item in raw_manifests)
    ):
        raise ConfigurationError(
            "PROJECT.yaml context.stack_manifests must be a non-empty string list"
        )
    stack_manifests = tuple(
        dict.fromkeys(safe_relative_path(item) for item in raw_manifests)
    )
    raw_git_ignores = context.get("git_ignore_prefixes", [])
    if not isinstance(raw_git_ignores, list) or not all(
        isinstance(item, str) and item.strip() for item in raw_git_ignores
    ):
        raise ConfigurationError(
            "PROJECT.yaml context.git_ignore_prefixes must be a string list"
        )
    normalized_git_ignores = [
        safe_relative_path(item).rstrip("/") for item in raw_git_ignores
    ]
    unsupported_git_ignores = sorted(
        set(normalized_git_ignores) - {"project-docs"}
    )
    if unsupported_git_ignores:
        raise ConfigurationError(
            "PROJECT.yaml context.git_ignore_prefixes may contain only the "
            f"ARIA docs boundary 'project-docs'; unsupported={unsupported_git_ignores}"
        )
    git_ignore_prefixes = tuple(
        value + "/" for value in dict.fromkeys(normalized_git_ignores)
    )

    backlog_relative = (
        safe_relative_path(str(documents["backlog"]))
        if isinstance(documents.get("backlog"), str)
        and str(documents["backlog"]).strip()
        else None
    )
    raw_governance = project.get("governance")
    governance: ProjectGovernance | None = None
    if raw_governance is not None:
        governance_table = _mapping(raw_governance, "PROJECT.yaml.governance")
        unknown_governance = set(governance_table) - {
            "status_authority",
            "require_active_run_for_writes",
            "decision_registry",
        }
        if unknown_governance:
            raise ConfigurationError(
                "PROJECT.yaml.governance contains unknown fields: "
                f"{sorted(unknown_governance)}"
            )
        if backlog_relative is None:
            raise ConfigurationError(
                "PROJECT.yaml.governance requires documents.backlog"
            )
        status_authority = safe_relative_path(
            _required_string(
                governance_table,
                "status_authority",
                "PROJECT.yaml.governance",
            )
        )
        if status_authority != backlog_relative:
            raise ConfigurationError(
                "PROJECT.yaml.governance.status_authority must equal "
                f"documents.backlog ({backlog_relative})"
            )
        require_active = governance_table.get("require_active_run_for_writes")
        if not isinstance(require_active, bool):
            raise ConfigurationError(
                "PROJECT.yaml.governance.require_active_run_for_writes must be boolean"
            )
        raw_registry = governance_table.get("decision_registry")
        if raw_registry is not None and (
            not isinstance(raw_registry, str) or not raw_registry.strip()
        ):
            raise ConfigurationError(
                "PROJECT.yaml.governance.decision_registry must be a non-empty path"
            )
        governance = ProjectGovernance(
            status_authority=status_authority,
            require_active_run_for_writes=require_active,
            decision_registry=(
                safe_relative_path(raw_registry) if isinstance(raw_registry, str) else None
            ),
        )

    collaboration_mode = str(project.get("collaboration_mode", "offline"))
    control_path = docs_root / "CONTROL.yaml"
    if collaboration_mode == "collaborative":
        if not control_path.is_file():
            raise ConfigurationError("Collaborative PROJECT.yaml requires CONTROL.yaml")
        from aria.collaboration import load_control_contract

        control = load_control_contract(control_path)
        if control.project_id != project_id:
            raise ConfigurationError("CONTROL.yaml project identity mismatch")
    elif collaboration_mode != "offline" or control_path.exists():
        raise ConfigurationError("Mixed offline/collaborative project mode is forbidden")

    return ProjectConfig(
        project_id=project_id,
        display_name=str(project.get("display_name", project_id)),
        # Projects created before versioned metadata existed are legacy 1.4
        # inputs. Never silently grant them 1.5 access/backlog semantics.
        framework_version=str(project.get("framework_version", "1.4.0")),
        mode=mode,
        activation_engine_sha256=activation_engine_sha256,
        framework_root=root,
        docs_root=docs_root.absolute(),
        code_root=code_root.absolute(),
        runtime_root=machine_runtime / "projects" / project_id,
        registry_path=registry,
        project_path=project_path,
        files=ProjectFiles(
            state=document("state"),
            stack=document("stack"),
            history=document("history"),
            system_map=safe_relative_path(str(documents.get("system_map", "SYSTEM_MAP.yaml"))),
            specs=document("specs"),
            adr=document("adr"),
            knowledge=document("knowledge"),
            verification=(
                safe_relative_path(str(documents["verification"]))
                if isinstance(documents.get("verification"), str)
                and str(documents["verification"]).strip()
                else None
            ),
            team=(
                safe_relative_path(str(documents["team"]))
                if isinstance(documents.get("team"), str)
                and str(documents["team"]).strip()
                else None
            ),
            trust=(
                safe_relative_path(str(documents["trust"]))
                if isinstance(documents.get("trust"), str)
                and str(documents["trust"]).strip()
                else None
            ),
            access=(
                safe_relative_path(str(documents["access"]))
                if isinstance(documents.get("access"), str)
                and str(documents["access"]).strip()
                else None
            ),
            backlog=(
                backlog_relative
            ),
        ),
        governance=governance,
        state_profile=state_profile,
        stack_manifests=stack_manifests,
        git_ignore_prefixes=git_ignore_prefixes,
        context_budget_bytes=context_budget,
        state_budget_bytes=state_budget,
        state_projection_budget_bytes=state_projection_budget,
        collaboration_mode=collaboration_mode,
    )


def read_project_text(project: ProjectConfig, relative: str) -> str:
    path = project.document_path(relative)
    if not path.is_file():
        raise ConfigurationError(f"Required project document not found: {path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError(
            f"Project document is not readable UTF-8: {path}"
        ) from error


def file_fingerprint(
    path: Path, *, relative_to: Path | None = None
) -> dict[str, object]:
    content = path.read_bytes()
    return {
        "path": (
            path.relative_to(relative_to).as_posix()
            if relative_to is not None
            else str(path)
        ),
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def canonical_sha(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify_history(project: ProjectConfig) -> dict[str, object]:
    path = project.document_path(project.files.history)
    if not path.is_file():
        return {"ok": False, "path": str(path), "events": 0, "error": "missing"}
    previous: str | None = None
    expected_sequence = 1
    count = 0
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line.strip():
                    continue
                event = json.loads(raw_line)
                if not isinstance(event, dict):
                    raise WorkflowError(f"History line {line_number} is not an object")
                if event.get("schema_version") != 1:
                    raise WorkflowError(
                        f"History line {line_number} schema_version mismatch"
                    )
                if event.get("project_id") != project.project_id:
                    raise WorkflowError(
                        f"History line {line_number} project identity mismatch"
                    )
                actual_sha = event.get("event_sha256")
                unsigned = {
                    key: value for key, value in event.items() if key != "event_sha256"
                }
                expected_sha = canonical_sha(unsigned)
                if actual_sha != expected_sha:
                    raise WorkflowError(
                        f"History line {line_number} SHA mismatch: {actual_sha!r}!={expected_sha}"
                    )
                if event.get("sequence") != expected_sequence:
                    raise WorkflowError(f"History line {line_number} sequence mismatch")
                if event.get("previous_event_sha256") != previous:
                    raise WorkflowError(f"History line {line_number} chain mismatch")
                previous = str(actual_sha)
                expected_sequence += 1
                count += 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, WorkflowError) as error:
        return {
            "ok": False,
            "path": str(path),
            "events": count,
            "head_sha256": previous,
            "error": str(error),
        }
    return {
        "ok": True,
        "path": str(path),
        "events": count,
        "head_sha256": previous,
    }


def history_events(project: ProjectConfig) -> list[dict[str, object]]:
    verified = verify_history(project)
    if verified.get("ok") is not True:
        raise WorkflowError(f"History chain is invalid: {verified.get('error')}")
    path = project.document_path(project.files.history)
    events: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for raw_line in stream:
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            if not isinstance(event, dict):
                raise WorkflowError("Verified history yielded a non-object event")
            events.append(event)
    return events


def _git_bytes(code_root: Path, *arguments: str) -> bytes:
    raw_timeout = os.environ.get("ARIA_GIT_TIMEOUT_SECONDS")
    try:
        timeout = (
            DEFAULT_GIT_TIMEOUT_SECONDS
            if raw_timeout is None
            else float(raw_timeout)
        )
    except ValueError as error:
        raise ConfigurationError(
            "ARIA_GIT_TIMEOUT_SECONDS must be a number between 1 and 1800"
        ) from error
    if not 1 <= timeout <= 1800:
        raise ConfigurationError(
            "ARIA_GIT_TIMEOUT_SECONDS must be between 1 and 1800 seconds"
        )
    try:
        completed = subprocess.run(
            ["git", "-C", str(code_root), *arguments],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigurationError(
            f"Git read failed: {' '.join(arguments)}: {error}"
        ) from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        if not message:
            message = completed.stdout.decode("utf-8", errors="replace").strip()
        raise ConfigurationError(f"Git read failed: {' '.join(arguments)}: {message}")
    return completed.stdout


def _git_text(code_root: Path, *arguments: str) -> str:
    return _git_bytes(code_root, *arguments).decode("utf-8", errors="replace").strip()


def _zero_paths(content: bytes) -> list[str]:
    return [
        item.decode("utf-8", errors="replace").replace("\\", "/")
        for item in content.split(b"\0")
        if item
    ]


def _git_path_ignored(relative: str, ignored_prefixes: tuple[str, ...]) -> bool:
    normalized = relative.replace("\\", "/")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in ignored_prefixes
    )


def git_inventory(
    code_root: Path, ignored_prefixes: tuple[str, ...] = ()
) -> list[str]:
    return sorted(
        dict.fromkeys(
            relative
            for relative in _zero_paths(
                _git_bytes(
                    code_root,
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                )
            )
            if not _git_path_ignored(relative, ignored_prefixes)
        )
    )


def git_change_snapshot(
    code_root: Path, ignored_prefixes: tuple[str, ...] = ()
) -> dict[str, object]:
    paths: set[str] = set()
    for arguments in (
        ("diff", "--name-only", "-z"),
        ("diff", "--cached", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        paths.update(
            relative
            for relative in _zero_paths(_git_bytes(code_root, *arguments))
            if not _git_path_ignored(relative, ignored_prefixes)
        )
    fingerprints: dict[str, str | None] = {}
    root = code_root.resolve(strict=True)
    for relative in sorted(paths):
        normalized = safe_relative_path(relative)
        candidate = code_root.joinpath(*PurePosixPath(normalized).parts)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ConfigurationError(f"Git path escapes code root: {relative}")
        fingerprints[normalized] = (
            hashlib.sha256(candidate.read_bytes()).hexdigest()
            if candidate.is_file()
            else None
        )
    return {"paths": fingerprints}


def git_diff_names(
    code_root: Path,
    before_head: str,
    after_head: str,
    ignored_prefixes: tuple[str, ...] = (),
) -> list[str]:
    if before_head == after_head:
        return []
    return sorted(
        dict.fromkeys(
            relative
            for relative in _zero_paths(
                _git_bytes(
                    code_root,
                    "diff",
                    "--name-only",
                    "-z",
                    f"{before_head}..{after_head}",
                )
            )
            if not _git_path_ignored(relative, ignored_prefixes)
        )
    )


def git_is_ancestor(code_root: Path, before_head: str, after_head: str) -> bool:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(code_root),
                "merge-base",
                "--is-ancestor",
                before_head,
                after_head,
            ],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigurationError(
            f"Git ancestry check failed: {before_head}..{after_head}: {error}"
        ) from error
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    message = completed.stderr.decode("utf-8", errors="replace").strip()
    raise ConfigurationError(
        f"Git ancestry check failed: {before_head}..{after_head}: {message}"
    )


def git_commit_range(
    code_root: Path, before_head: str, after_head: str
) -> list[dict[str, object]]:
    if before_head == after_head:
        return []
    if not git_is_ancestor(code_root, before_head, after_head):
        raise WorkflowError(
            f"Git HEAD is not a descendant of the run baseline: "
            f"{before_head}..{after_head}"
        )
    commits = _git_text(
        code_root,
        "rev-list",
        "--reverse",
        f"{before_head}..{after_head}",
    ).splitlines()
    rows: list[dict[str, object]] = []
    trailer_pattern = re.compile(r"^ARIA-([A-Za-z][A-Za-z-]*):\s*(.+)$")
    for commit in commits:
        sha = _git_text(code_root, "rev-parse", f"{commit}^{{commit}}")
        subject = _git_text(code_root, "show", "-s", "--format=%s", sha)
        body = _git_text(code_root, "show", "-s", "--format=%B", sha)
        trailers: dict[str, list[str]] = {}
        for line in body.splitlines():
            match = trailer_pattern.match(line.strip())
            if match is None:
                continue
            key = match.group(1).lower().replace("-", "_")
            trailers.setdefault(key, []).append(match.group(2).strip())
        rows.append({"sha": sha, "subject": subject, "trailers": trailers})
    return rows


def git_resolve_commit(code_root: Path, value: str) -> str:
    return _git_text(code_root, "rev-parse", f"{value}^{{commit}}")


def git_snapshot(
    code_root: Path, ignored_prefixes: tuple[str, ...] = ()
) -> dict[str, object]:
    root = Path(_git_text(code_root, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    head = _git_text(code_root, "rev-parse", "HEAD")
    branch = _git_text(code_root, "branch", "--show-current") or None
    changes = git_change_snapshot(code_root, ignored_prefixes)
    changed_paths = changes["paths"]
    if not isinstance(changed_paths, dict):
        raise ConfigurationError("Git change snapshot is invalid")
    return {
        "root": str(root),
        "head": head,
        "branch": branch,
        "dirty": bool(changed_paths),
        "changed_count": len(changed_paths),
        "changes": changes,
    }


def validate_task_trace(
    project: ProjectConfig,
    task_id: str,
    trace: dict[str, object],
    history_by_sequence: dict[int, dict[str, object]],
) -> list[str]:
    errors: list[str] = []

    def artifact(value: object, root: str, label: str) -> None:
        if value is None:
            return
        if not isinstance(value, dict):
            errors.append(f"{task_id}:{label}:not-a-mapping")
            return
        relative = value.get("path")
        expected_sha = value.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_sha, str):
            errors.append(f"{task_id}:{label}:missing-path-or-sha")
            return
        try:
            normalized = safe_relative_path(relative)
            candidate = PurePosixPath(normalized)
            allowed = PurePosixPath(safe_relative_path(root))
            path = project.document_path(normalized)
            if not candidate.is_relative_to(allowed) or not path.is_file():
                errors.append(f"{task_id}:{label}:missing-or-outside:{relative}")
                return
            actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_sha != expected_sha:
                errors.append(f"{task_id}:{label}:sha-drift:{relative}")
        except (ConfigurationError, OSError):
            errors.append(f"{task_id}:{label}:invalid-path:{relative}")

    artifact(trace.get("spec"), project.files.specs, "spec")
    for index, value in enumerate(trace.get("adrs", [])):
        artifact(value, project.files.adr, f"adr[{index}]")
    for index, value in enumerate(trace.get("research", [])):
        artifact(value, project.files.knowledge, f"research[{index}]")

    implementation = trace.get("implementation")
    if isinstance(implementation, dict):
        base = implementation.get("base_commit")
        head = implementation.get("head_commit")
        expected_commits = implementation.get("commits")
        if (
            isinstance(base, str)
            and isinstance(head, str)
            and isinstance(expected_commits, list)
        ):
            try:
                actual = [
                    str(row["sha"])
                    for row in git_commit_range(project.code_root, base, head)
                ]
                if actual != expected_commits:
                    errors.append(f"{task_id}:implementation:commit-range-drift")
            except (ConfigurationError, WorkflowError):
                errors.append(f"{task_id}:implementation:invalid-commit-range")

    legacy_implementation = trace.get("legacy_implementation")
    if isinstance(legacy_implementation, dict):
        commits = legacy_implementation.get("commits")
        portable = (
            legacy_implementation.get("source") != "imported STATE.commit"
            and legacy_implementation.get("completeness") != "pointer_only"
        )
        if portable and isinstance(commits, list):
            for commit in commits:
                if not isinstance(commit, str):
                    continue
                try:
                    if git_resolve_commit(project.code_root, commit) != commit:
                        errors.append(
                            f"{task_id}:legacy-implementation:non-canonical:{commit}"
                        )
                except ConfigurationError:
                    errors.append(
                        f"{task_id}:legacy-implementation:missing-commit:{commit}"
                    )

    history_ref = trace.get("history")
    if isinstance(history_ref, dict):
        sequence = history_ref.get("sequence")
        expected_sha = history_ref.get("event_sha256")
        event = history_by_sequence.get(sequence) if isinstance(sequence, int) else None
        if event is None or event.get("event_sha256") != expected_sha:
            errors.append(f"{task_id}:history:event-mismatch")
    return errors


def run_project_doctor(project: ProjectConfig) -> dict[str, object]:
    if project.collaboration_mode == "collaborative":
        from aria.collaborative_doctor import run_collaborative_project_doctor

        return run_collaborative_project_doctor(project)
    checks: list[dict[str, object]] = []

    def add(
        check_id: str, ok: bool, detail: str, *, blocking: bool = True
    ) -> None:
        checks.append(
            {"id": check_id, "ok": ok, "blocking": blocking, "detail": detail}
        )

    add("registry", project.registry_path.is_file(), str(project.registry_path))
    add(
        "framework_version",
        project.framework_version
        in {__version__, "1.5.4", "1.5.3", "1.5.2", "1.5.1", "1.5.0", "1.4.0"},
        f"project={project.framework_version}; runtime={__version__}",
    )
    current_engine_sha = str(engine_state(project.framework_root)["sha256"])
    activation_ok = (
        project.mode == "shadow"
        or project.activation_engine_sha256 == current_engine_sha
    )
    add(
        "active_engine_integrity",
        activation_ok,
        (
            "shadow mode; activation marker is not required"
            if project.mode == "shadow"
            else f"marker={project.activation_engine_sha256}; current={current_engine_sha}"
        ),
    )
    add("framework_root", project.framework_root.is_dir(), str(project.framework_root))
    add("project_identity", project.project_path.is_file(), str(project.project_path))
    add("docs_root", project.docs_root.is_dir(), str(project.docs_root))
    add("code_root", project.code_root.is_dir(), str(project.code_root))
    separated = False
    if (
        project.framework_root.is_dir()
        and project.docs_root.is_dir()
        and project.code_root.is_dir()
    ):
        resolved_roots = [
            project.framework_root.resolve(strict=True),
            project.docs_root.resolve(strict=True),
            project.code_root.resolve(strict=True),
        ]
        separated = all(
            left != right
            and not left.is_relative_to(right)
            and not right.is_relative_to(left)
            for index, left in enumerate(resolved_roots)
            for right in resolved_roots[index + 1 :]
        )
    add(
        "framework_docs_code_separation",
        separated,
        f"framework={project.framework_root}; docs={project.docs_root}; code={project.code_root}",
    )
    for prefix in project.git_ignore_prefixes:
        relative = prefix.rstrip("/")
        boundary = project.code_root.joinpath(*PurePosixPath(relative).parts)
        exists = boundary.is_dir()
        resolved = boundary.resolve(strict=False)
        outside = not resolved.is_relative_to(project.code_root.resolve(strict=True))
        add(
            f"git_ignore_boundary:{relative}",
            exists,
            (
                f"{boundary}; kind={'external-junction' if outside else 'legacy-in-repo-docs'}"
            ),
        )
    for relative in (
        project.files.state,
        project.files.stack,
        project.files.history,
        project.files.system_map,
    ):
        path = project.document_path(relative)
        add(f"required:{relative}", path.is_file(), str(path))
    for relative in (
        project.files.specs,
        project.files.adr,
        project.files.knowledge,
    ):
        path = project.document_path(relative)
        add(f"directory:{relative}", path.is_dir(), str(path))
    if project.framework_version in {"1.4.0", "1.5.0", "1.5.1", "1.5.2", "1.5.3", "1.5.4", "1.5.5"}:
        for key, relative in (
            ("team", project.files.team),
            ("trust", project.files.trust),
        ):
            configured = isinstance(relative, str) and bool(relative)
            add(
                f"required_pointer:{key}",
                configured,
                str(relative),
            )
            if configured:
                path = project.document_path(relative)
                add(f"required:{relative}", path.is_file(), str(path))
        if project.files.team:
            try:
                from aria.team import load_team

                load_team(project)
            except (ConfigurationError, WorkflowError) as error:
                add("team_configuration", False, str(error))
            else:
                add("team_configuration", True, project.files.team)
        if project.files.trust:
            try:
                from aria.trust import load_trust_policy

                load_trust_policy(project.document_path(project.files.trust))
            except (ConfigurationError, WorkflowError) as error:
                add("trust_configuration", False, str(error))
            else:
                add("trust_configuration", True, project.files.trust)
    if project.framework_version in {"1.5.0", "1.5.1", "1.5.2", "1.5.3", "1.5.4", "1.5.5"}:
        for key, relative in (
            ("access", project.files.access),
            ("backlog", project.files.backlog),
        ):
            configured = isinstance(relative, str) and bool(relative)
            add(f"required_pointer:{key}", configured, str(relative))
            if configured:
                path = project.document_path(relative)
                add(f"required:{relative}", path.is_file(), str(path))
        if project.files.access:
            try:
                from aria.access import load_access_policy, verify_access_audit

                access = load_access_policy(project)
                access_active = access.get("status") == "active"
                add(
                    "access_configuration",
                    access_active or project.mode == "shadow",
                    f"status={access.get('status')}; revision={access.get('revision')}",
                )
                if access_active:
                    audit = verify_access_audit(project)
                    add(
                        "access_audit",
                        audit.get("ok") is True,
                        json.dumps(audit, ensure_ascii=False),
                    )
                else:
                    add(
                        "access_audit",
                        True,
                        "bootstrap pending; no signed access event exists",
                        blocking=False,
                    )
            except (ConfigurationError, WorkflowError) as error:
                add("access_configuration", False, str(error))
        if project.files.backlog:
            try:
                from aria.backlog import load_backlog

                backlog = load_backlog(project)
                add(
                    "backlog_configuration",
                    True,
                    f"revision={backlog.get('revision')}; items={len(backlog.get('items', []))}",
                )
            except (ConfigurationError, WorkflowError) as error:
                add("backlog_configuration", False, str(error))

    if project.governance is None:
        add(
            "governance_contract",
            False,
            "PROJECT.yaml has no executable governance contract; legacy behavior remains available",
            blocking=False,
        )
    else:
        try:
            from aria.governance import governance_diagnostics

            governance = governance_diagnostics(project)
            add(
                "governance_contract",
                governance.get("ok") is True,
                json.dumps(governance, ensure_ascii=False),
            )
        except (ConfigurationError, WorkflowError) as error:
            add("governance_contract", False, str(error))

    state_path = project.document_path(project.files.state)
    state_size = state_path.stat().st_size if state_path.is_file() else -1
    add(
        "compact_state",
        0 <= state_size <= project.state_budget_bytes,
        f"size={state_size}; budget={project.state_budget_bytes}",
    )
    stack_path = project.document_path(project.files.stack)
    stack_size = stack_path.stat().st_size if stack_path.is_file() else 0
    add("working_stack", stack_size > 0, f"size={stack_size}")
    history = verify_history(project)
    add(
        "history_chain",
        history.get("ok") is True,
        json.dumps(history, ensure_ascii=False),
    )
    state_identity = False
    checkpoint_matches = False
    state_model: dict[str, object] = {
        "ok": False,
        "errors": ["STATE.yaml is missing"],
        "warnings": [],
    }
    projection_size = -1
    state_specs_ok = False
    state_specs_detail = "STATE.yaml is missing"
    state_trace_ok = False
    state_trace_detail = "STATE.yaml is missing"
    state_git_refs_ok = False
    state_git_refs_detail = "STATE.yaml is missing"
    if state_path.is_file():
        try:
            state_text = state_path.read_text(encoding="utf-8-sig")
            state = parse_project_state(
                state_text,
                project_id=project.project_id,
                expected_profile=project.state_profile,
            )
            state_model = validate_state_model(
                state,
                project_id=project.project_id,
                expected_profile=project.state_profile,
            )
            state_identity = state_model.get("ok") is True
            projection_size = len(
                projection_yaml(state, selected=select_next_task(state)).encode("utf-8")
            )
            checkpoint = state.get("history_checkpoint")
            checkpoint_matches = (
                isinstance(checkpoint, dict)
                and checkpoint.get("sequence") == history.get("events")
                and checkpoint.get("event_sha256") == history.get("head_sha256")
            )
            specs_root = PurePosixPath(project.files.specs)
            bad_specs: list[str] = []
            spec_count = 0
            spec_refs: list[tuple[str, object]] = []
            for _stage_index, _stage_id, _stage, task in iter_stage_tasks(state):
                spec_refs.append((f"task:{task.get('id')}", task.get("spec")))
            focus = state.get("focus")
            current = state.get("current")
            if isinstance(focus, dict):
                spec_refs.append(("focus", focus.get("spec")))
            if isinstance(current, dict):
                spec_refs.append(("current", current.get("spec")))
            for owner, spec in spec_refs:
                if not isinstance(spec, str):
                    continue
                spec_count += 1
                try:
                    normalized = safe_relative_path(spec)
                    candidate = PurePosixPath(normalized)
                    if (
                        candidate.suffix.lower() != ".md"
                        or not candidate.is_relative_to(specs_root)
                        or not project.document_path(normalized).is_file()
                    ):
                        bad_specs.append(f"{owner}:{spec}")
                except ConfigurationError:
                    bad_specs.append(f"{owner}:{spec}")
            state_specs_ok = not bad_specs
            state_specs_detail = f"linked={spec_count}; missing_or_invalid={bad_specs}"
            history_rows = history_events(project) if history.get("ok") is True else []
            history_by_sequence = {
                int(event["sequence"]): event
                for event in history_rows
                if isinstance(event.get("sequence"), int)
            }
            trace_errors: list[str] = []
            trace_count = 0
            for _stage_index, _stage_id, _stage, task in iter_stage_tasks(state):
                trace = task.get("trace")
                if not isinstance(trace, dict):
                    continue
                trace_count += 1
                trace_errors.extend(
                    validate_task_trace(
                        project,
                        str(task.get("id")),
                        trace,
                        history_by_sequence,
                    )
                )
            state_trace_ok = not trace_errors
            state_trace_detail = (
                f"linked_tasks={trace_count}; missing_or_invalid={trace_errors}"
            )
            git_ref_errors: list[str] = []
            git_ref_count = 0
            for owner in ("current", "last_completed", "last_verified"):
                row = state.get(owner)
                value = row.get("git_head") if isinstance(row, dict) else None
                if not isinstance(value, str) or not value:
                    continue
                git_ref_count += 1
                try:
                    if git_resolve_commit(project.code_root, value) != value:
                        git_ref_errors.append(f"{owner}:non-canonical:{value}")
                except ConfigurationError:
                    git_ref_errors.append(f"{owner}:missing:{value}")
            state_git_refs_ok = not git_ref_errors
            state_git_refs_detail = (
                f"linked={git_ref_count}; missing_or_invalid={git_ref_errors}"
            )
        except (
            ConfigurationError,
            WorkflowError,
            OSError,
            UnicodeDecodeError,
        ) as error:
            state_identity = False
            checkpoint_matches = False
            state_model = {"ok": False, "errors": [str(error)], "warnings": []}
            state_specs_detail = str(error)
            state_trace_detail = str(error)
            state_git_refs_detail = str(error)
    add("state_identity", state_identity, str(state_path))
    add(
        "state_model",
        state_model.get("ok") is True,
        json.dumps(state_model, ensure_ascii=False),
    )
    add(
        "state_projection_budget",
        0 <= projection_size <= project.state_projection_budget_bytes,
        f"size={projection_size}; budget={project.state_projection_budget_bytes}",
    )
    add("state_task_specs", state_specs_ok, state_specs_detail)
    add("state_traceability", state_trace_ok, state_trace_detail)
    add("state_git_refs", state_git_refs_ok, state_git_refs_detail)
    add(
        "state_history_checkpoint",
        checkpoint_matches,
        f"events={history.get('events')}; head={history.get('head_sha256')}",
    )
    roadmap_path = project.docs_root / "ROADMAP.md"
    add(
        "no_roadmap_duplicate",
        not roadmap_path.exists(),
        f"STATE.yaml is the only live project map; duplicate={roadmap_path}",
    )
    manifest_checks: list[str] = []
    manifests_ok = True
    for relative in project.stack_manifests:
        path = project.code_root.joinpath(*PurePosixPath(relative).parts)
        manifests_ok = manifests_ok and path.is_file()
        manifest_checks.append(f"{relative}:{path.is_file()}")
    add("stack_manifests", manifests_ok, "; ".join(manifest_checks))

    if project.files.verification is None:
        add(
            "verification_contract",
            True,
            "not configured; legacy/manual verification remains available",
            blocking=False,
        )
    else:
        try:
            from aria.execution import load_execution_config

            verification = load_execution_config(project)
            command_count = (
                len(verification.get("commands", []))
                if isinstance(verification, dict)
                else 0
            )
            add(
                "verification_contract",
                verification is not None and command_count > 0,
                f"path={project.files.verification}; commands={command_count}",
            )
        except ConfigurationError as error:
            add("verification_contract", False, str(error))

    lessons = LessonStore(project.framework_root, project.runtime_root).verify()
    add(
        "behavior_lessons_chain",
        lessons.get("ok") is True,
        json.dumps(lessons, ensure_ascii=False),
    )

    runtime_resolved = project.runtime_root.resolve(strict=False)
    outside = all(
        not runtime_resolved.is_relative_to(root.resolve(strict=True))
        for root in (project.framework_root, project.docs_root, project.code_root)
        if root.is_dir()
    )
    add("runtime_outside_project_roots", outside, str(project.runtime_root))
    if outside:
        probe_path: Path | None = None
        token = os.urandom(16).hex()
        try:
            project.runtime_root.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                dir=project.runtime_root, prefix=".doctor-", suffix=".probe"
            )
            probe_path = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(token)
                stream.flush()
                os.fsync(stream.fileno())
            actual = probe_path.read_text(encoding="utf-8")
            add(
                "runtime_write_readback", actual == token, f"token_length={len(actual)}"
            )
        except OSError as error:
            add("runtime_write_readback", False, str(error))
        finally:
            if probe_path is not None and probe_path.exists():
                probe_path.unlink()
    else:
        add("runtime_write_readback", False, "runtime boundary failed")

    try:
        git = git_snapshot(project.code_root, project.git_ignore_prefixes)
        expected_root = project.code_root.resolve(strict=True)
        actual_root = Path(str(git["root"])).resolve(strict=True)
        add(
            "git_root_identity",
            actual_root == expected_root,
            f"configured={expected_root}; actual={actual_root}",
        )
        from aria.assurance import load_system_map

        system_map = load_system_map(project, git)
        add(
            "system_map_valid",
            system_map.get("valid") is True,
            json.dumps(
                {
                    "path": system_map.get("path"),
                    "errors": system_map.get("errors"),
                    "summary": system_map.get("summary"),
                },
                ensure_ascii=False,
            ),
        )
        add(
            "system_map_freshness",
            system_map.get("fresh") is True,
            f"mapped={system_map.get('mapped_git_head')}; current={system_map.get('current_git_head')}",
            blocking=False,
        )
    except (ConfigurationError, OSError) as error:
        add("git_root_identity", False, str(error))
        add("system_map_valid", False, str(error))

    return {
        "schema_version": 1,
        "ok": all(
            bool(check["ok"]) for check in checks if check.get("blocking", True)
        ),
        "project": project.project_id,
        "mode": project.mode,
        "checks": checks,
    }
