from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

from aria.errors import ConfigurationError
from aria.io import atomic_write_bytes, exclusive_lock
from aria.project import PROJECT_ID_RE, default_runtime_root


def registry_path(runtime_root: Path | None = None) -> Path:
    root = runtime_root.absolute() if runtime_root is not None else default_runtime_root()
    return root / "projects.toml"


def read_registry(path: Path | None = None) -> dict[str, object]:
    target = path or registry_path()
    if not target.is_file():
        return {"schema_version": 1, "projects": {}}
    try:
        with target.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"Project registry is unreadable: {target}: {error}") from error
    if payload.get("schema_version") != 1:
        raise ConfigurationError(
            f"Unsupported project registry schema_version: {payload.get('schema_version')!r}"
        )
    projects = payload.get("projects", {})
    if not isinstance(projects, dict) or not all(
        isinstance(key, str) and isinstance(value, dict)
        for key, value in projects.items()
    ):
        raise ConfigurationError("Project registry [projects] table is invalid")
    return {"schema_version": 1, "projects": projects}


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_registry(payload: dict[str, object]) -> bytes:
    projects = payload.get("projects", {})
    if not isinstance(projects, dict):
        raise ConfigurationError("Project registry projects must be a mapping")
    lines = ["schema_version = 1", ""]
    for project_id in sorted(projects):
        entry = projects[project_id]
        if not isinstance(entry, dict):
            raise ConfigurationError(f"Registry project {project_id!r} must be a mapping")
        lines.append(f"[projects.{project_id}]")
        for key in ("docs_root", "code_root", "mode", "engine_sha256"):
            value = entry.get(key)
            if isinstance(value, str) and value:
                lines.append(f"{key} = {_toml_string(value)}")
        lines.append("")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _assert_no_pending_cutover(runtime_root: Path, project_id: str) -> None:
    cutovers = runtime_root / "projects" / project_id / "cutovers"
    if not cutovers.is_dir():
        return
    for journal_path in sorted(cutovers.glob("*/cutover-journal.json")):
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError(
                f"Pending cutover journal is unreadable: {journal_path}"
            ) from error
        if not isinstance(journal, dict):
            raise ConfigurationError(
                f"Pending cutover journal is malformed: {journal_path}"
            )
        if journal.get("phase") == "prepared":
            raise ConfigurationError(
                f"Cannot re-register {project_id} while active cutover recovery is pending"
            )


def register_project(
    project_id: str,
    *,
    docs_root: Path,
    code_root: Path,
    mode: str = "shadow",
    runtime_root: Path | None = None,
    engine_sha256: str | None = None,
) -> dict[str, object]:
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    if mode != "shadow" or engine_sha256 is not None:
        raise ConfigurationError(
            "Registration is shadow-only; active mode requires _project-activate "
            "with doctor and isolated canary"
        )
    docs = docs_root.resolve(strict=True)
    code = code_root.resolve(strict=True)
    if docs == code or docs.is_relative_to(code) or code.is_relative_to(docs):
        raise ConfigurationError("Project docs and code roots must be separate")
    project_path = docs / "PROJECT.yaml"
    if not project_path.is_file():
        raise ConfigurationError(f"PROJECT.yaml not found: {project_path}")
    try:
        project_doc = yaml.safe_load(project_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"PROJECT.yaml is unreadable: {project_path}: {error}") from error
    if not isinstance(project_doc, dict) or project_doc.get("project_id") != project_id:
        raise ConfigurationError(
            f"Project identity mismatch: requested={project_id!r}, "
            f"document={project_doc.get('project_id') if isinstance(project_doc, dict) else None!r}"
        )
    target = registry_path(runtime_root)
    lock = target.with_suffix(".lock")
    with exclusive_lock(lock, timeout_seconds=120.0):
        _assert_no_pending_cutover(target.parent, project_id)
        payload = read_registry(target)
        projects = dict(payload.get("projects", {}))
        existing = projects.get(project_id)
        if isinstance(existing, dict) and existing.get("mode") == "active":
            raise ConfigurationError(
                f"Refusing to overwrite active registration: {project_id}"
            )
        entry: dict[str, str] = {
            "docs_root": docs.as_posix(),
            "code_root": code.as_posix(),
            "mode": mode,
        }
        projects[project_id] = entry
        updated = {"schema_version": 1, "projects": projects}
        atomic_write_bytes(target, render_registry(updated))
    verified = read_registry(target)
    if verified.get("projects", {}).get(project_id) != entry:
        raise ConfigurationError(f"Project registry read-back failed: {project_id}")
    return {
        "ok": True,
        "project": project_id,
        "mode": mode,
        "registry_path": str(target),
        "docs_root": str(docs),
        "code_root": str(code),
    }


def list_registered_projects(
    *, runtime_root: Path | None = None
) -> dict[str, object]:
    target = registry_path(runtime_root)
    payload = read_registry(target)
    rows = []
    projects = payload.get("projects", {})
    if isinstance(projects, dict):
        for project_id in sorted(projects):
            entry = projects[project_id]
            if not isinstance(entry, dict):
                continue
            docs = Path(str(entry.get("docs_root", "")))
            code = Path(str(entry.get("code_root", "")))
            rows.append(
                {
                    "project": project_id,
                    "mode": entry.get("mode", "shadow"),
                    "docs_root": str(docs),
                    "code_root": str(code),
                    "docs_present": docs.is_dir(),
                    "code_present": code.is_dir(),
                }
            )
    return {
        "schema_version": 1,
        "ok": all(row["docs_present"] and row["code_present"] for row in rows),
        "registry_path": str(target),
        "projects": rows,
    }
