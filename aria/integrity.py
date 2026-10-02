from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


CACHE_DIRECTORIES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "htmlcov",
}
CACHE_FILES = {".coverage", "coverage.xml"}


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root):
        directories[:] = [
            name
            for name in directories
            if name not in CACHE_DIRECTORIES
            and name not in {".git", ".aria-state", ".venv", "venv", "node_modules"}
        ]
        current_path = Path(current)
        files.extend(current_path / name for name in names)
    return files


def engine_state(project_root: Path) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    framework_layout = (project_root / "aria").is_dir()
    if framework_layout:
        candidates = _walk_files(project_root / "aria")
        skills = project_root / ".agents" / "skills"
        if skills.is_dir():
            candidates.extend(_walk_files(skills))
        candidates.extend(
            path
            for path in (
                project_root / "pyproject.toml",
                project_root / ".aria-root",
                project_root / "AGENTS.md",
            )
            if path.is_file()
        )
    else:
        candidates = _walk_files(project_root)
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(project_root).as_posix()
        operational = (
            relative.startswith("aria/")
            or relative.startswith(".agents/skills/")
            or relative in {"pyproject.toml", ".aria-root", "AGENTS.md"}
        )
        if (
            (framework_layout and not operational)
            or CACHE_DIRECTORIES.intersection(path.parts)
            or ".aria-state" in path.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name in CACHE_FILES
            or ".git" in path.parts
        ):
            continue
        content = path.read_bytes()
        rows.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    rows.sort(key=lambda row: str(row["path"]))
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "count": len(rows),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "files": rows,
    }
