from __future__ import annotations

import os
import sys
import tempfile
import tomllib
from importlib import metadata
from pathlib import Path

from aria import __version__
from aria.integrity import engine_state
from aria.project import _framework_root, default_runtime_root
from aria.registry import list_registered_projects


def run_framework_doctor(framework_root: Path | None = None) -> dict[str, object]:
    root = _framework_root(framework_root)
    checks: list[dict[str, object]] = []

    def add(check_id: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append(
            {"id": check_id, "ok": ok, "blocking": blocking, "detail": detail}
        )

    add(
        "python_version",
        sys.version_info >= (3, 12),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    add("framework_marker", (root / ".aria-root").is_file(), str(root / ".aria-root"))
    package_version: str | None = None
    pyproject = root / "pyproject.toml"
    try:
        with pyproject.open("rb") as stream:
            package_version = str(tomllib.load(stream).get("project", {}).get("version"))
    except (OSError, tomllib.TOMLDecodeError):
        package_version = None
    add(
        "version_identity",
        package_version == __version__,
        f"package={package_version}; runtime={__version__}",
    )
    try:
        distribution_version = metadata.version("aria-codex")
    except metadata.PackageNotFoundError:
        distribution_version = None
    add(
        "distribution_version",
        distribution_version in {None, __version__},
        f"distribution={distribution_version}; runtime={__version__}",
    )
    core = (
        "aria/cli.py",
        "aria/project.py",
        "aria/project_state.py",
        "aria/simple_run.py",
        "aria/assurance.py",
        "aria/execution.py",
        "aria/registry.py",
    )
    missing = [relative for relative in core if not (root / relative).is_file()]
    add("core_modules", not missing, f"missing={missing}")
    engine = engine_state(root)
    add(
        "engine_identity",
        bool(engine.get("files")) and isinstance(engine.get("sha256"), str),
        f"files={engine.get('count')}; sha256={engine.get('sha256')}",
    )
    runtime = default_runtime_root()
    probe: Path | None = None
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=runtime, prefix=".doctor-", suffix=".probe")
        probe = Path(name)
        token = os.urandom(24)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        add("runtime_write_readback", probe.read_bytes() == token, str(runtime))
    except OSError as error:
        add("runtime_write_readback", False, str(error))
    finally:
        if probe is not None and probe.exists():
            probe.unlink()
    projects = list_registered_projects(runtime_root=runtime)
    add(
        "project_registry",
        projects.get("ok") is True,
        f"path={projects.get('registry_path')}; projects={len(projects.get('projects', []))}",
    )
    return {
        "schema_version": 1,
        "ok": all(check["ok"] for check in checks if check["blocking"]),
        "framework": str(root),
        "version": __version__,
        "engine_sha256": engine.get("sha256"),
        "checks": checks,
    }
