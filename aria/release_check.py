from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aria import __version__
from aria.errors import ConfigurationError
from aria.integrity import engine_state
from aria.io import atomic_write_bytes, atomic_write_json
from aria.project import _framework_root, git_resolve_commit


def _candidate_state(root: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root.as_posix()}",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ConfigurationError("release-check cannot inventory candidate Git files")
    paths = sorted(
        value
        for value in completed.stdout.decode("utf-8").split("\0")
        if value
    )
    rows = [
        {
            "path": relative,
            "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest(),
        }
        for relative in paths
    ]
    return {
        "count": len(rows),
        "sha256": hashlib.sha256(
            json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "files": rows,
    }


def _candidate_git_identity(root: Path) -> dict[str, object]:
    def run(*arguments: str) -> subprocess.CompletedProcess[bytes]:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={root.as_posix()}",
                *arguments,
            ],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ConfigurationError(
                f"release-check Git inspection failed: {' '.join(arguments)}"
            )
        return completed

    status = run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    changes = [
        row.decode("utf-8", errors="replace")
        for row in status.stdout.split(b"\0")
        if row
    ]
    head = run("rev-parse", "HEAD").stdout.decode("ascii", errors="strict").strip()
    branch = (
        run("branch", "--show-current")
        .stdout.decode("utf-8", errors="strict")
        .strip()
        or None
    )
    commit_timestamp = (
        run("show", "-s", "--format=%ct", "HEAD")
        .stdout.decode("ascii", errors="strict")
        .strip()
    )
    if not commit_timestamp.isdigit():
        raise ConfigurationError("release-check cannot read candidate commit timestamp")
    return {
        "clean": not changes,
        "head": head,
        "branch": branch,
        "commit_timestamp": commit_timestamp,
        "changes": changes,
    }


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _run(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    logs: Path,
    env: dict[str, str] | None = None,
    timeout: int = 900,
) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        output = completed.stdout + ("\n" if completed.stdout and completed.stderr else "") + completed.stderr
        exit_code = completed.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        output = str(error)
        exit_code = 124 if isinstance(error, subprocess.TimeoutExpired) else 127
    log_path = logs / f"{name}.log"
    content = output.encode("utf-8")
    atomic_write_bytes(log_path, content)
    return {
        "id": name,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "command": command,
        "log_path": str(log_path),
        "log_sha256": hashlib.sha256(content).hexdigest(),
        "log_size_bytes": len(content),
        "actual_result": (
            "Command completed successfully"
            if exit_code == 0
            else f"Command failed with exit code {exit_code}"
        ),
        "excerpt": output[-2000:],
    }


def _expect_failure(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    logs: Path,
    env: dict[str, str] | None = None,
    contains: str | None = None,
) -> dict[str, object]:
    check = _run(name, command, cwd=cwd, logs=logs, env=env)
    rejected = check.get("exit_code") not in {0, None}
    expected_text = contains is None or contains.lower() in str(check.get("excerpt", "")).lower()
    check["ok"] = rejected and expected_text
    check["expected_failure"] = True
    check["expected_text"] = contains
    check["actual_result"] = (
        "Command was rejected as required"
        if check["ok"]
        else "Expected rejection was not observed"
    )
    return check


def _run_claim_collision(
    *,
    commands: list[tuple[str, list[str]]],
    cwd: Path,
    logs: Path,
    env: dict[str, str],
) -> dict[str, object]:
    if len(commands) != 2:
        raise ConfigurationError("claim collision requires exactly two actors")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_run, name, command, cwd=cwd, logs=logs, env=env)
            for name, command in commands
        ]
        attempts = [future.result() for future in futures]
    winners = [row for row in attempts if row.get("ok") is True]
    rejected = [
        row
        for row in attempts
        if row.get("exit_code") not in {0, None}
    ]
    return {
        "id": "smoke-v15-mission-claim-collision",
        "ok": len(winners) == 1 and len(rejected) == 1,
        "exit_code": 0 if len(winners) == 1 and len(rejected) == 1 else 1,
        "actual_result": (
            "Exactly one actor acquired the backlog item"
            if len(winners) == 1 and len(rejected) == 1
            else "Backlog claim collision did not produce exactly one winner"
        ),
        "winner_check_id": winners[0]["id"] if len(winners) == 1 else None,
        "attempts": attempts,
    }


def _merge_trust_policy(
    path: Path,
    *,
    keys: list[dict[str, object]],
    policies: dict[str, dict[str, object]],
) -> None:
    try:
        current = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot extend trust policy: {path}") from error
    if (
        not isinstance(current, dict)
        or current.get("schema_version") != 1
        or not isinstance(current.get("keys"), list)
        or not isinstance(current.get("policies"), dict)
    ):
        raise ConfigurationError(f"Cannot extend malformed trust policy: {path}")
    keys_by_id = {
        str(row["id"]): row
        for row in current["keys"]
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    for row in keys:
        keys_by_id[str(row["id"])] = row
    merged_policies = dict(current["policies"])
    merged_policies.update(policies)
    atomic_write_bytes(
        path,
        yaml.safe_dump(
            {
                "schema_version": 1,
                "keys": list(keys_by_id.values()),
                "policies": merged_policies,
            },
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8"),
    )


def _release_environment(
    output: Path,
    *,
    temp_root: Path,
    source_date_epoch: str,
) -> dict[str, str]:
    if not source_date_epoch.isdigit():
        raise ConfigurationError("release SOURCE_DATE_EPOCH must be a Unix timestamp")
    env = dict(os.environ)
    for inherited in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
    ):
        env.pop(inherited, None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    pip_cache = output / "pip-cache"
    pip_cache.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    env["PIP_CACHE_DIR"] = str(pip_cache)
    env["TEMP"] = str(temp_root)
    env["TMP"] = str(temp_root)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INDEX"] = "1"
    env["SOURCE_DATE_EPOCH"] = source_date_epoch
    env["ARIA_RUNTIME_ROOT"] = str(output / "source-runtime")
    return env


def _wheel_reproducibility_check(
    primary: list[Path],
    rebuilt: list[Path],
) -> dict[str, object]:
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    valid_shape = (
        len(primary) == 1
        and len(rebuilt) == 1
        and primary[0].name == rebuilt[0].name
    )
    primary_sha = digest(primary[0]) if len(primary) == 1 else None
    rebuilt_sha = digest(rebuilt[0]) if len(rebuilt) == 1 else None
    ok = valid_shape and primary_sha == rebuilt_sha
    return {
        "id": "wheel-reproducibility",
        "ok": ok,
        "exit_code": 0 if ok else 1,
        "primary": str(primary[0]) if len(primary) == 1 else None,
        "rebuilt": str(rebuilt[0]) if len(rebuilt) == 1 else None,
        "primary_sha256": primary_sha,
        "rebuilt_sha256": rebuilt_sha,
        "actual_result": (
            "Independent wheel rebuild is byte-identical"
            if ok
            else "Independent wheel rebuild differs or is ambiguous"
        ),
    }


def _validate_run_policy(
    check: dict[str, object],
    *,
    mode: str,
    intent: str,
    mechanism: str,
    managed_lifecycle: bool,
) -> None:
    if check.get("ok") is not True:
        return
    try:
        payload = json.loads(Path(str(check["log_path"])).read_text(encoding="utf-8"))
        manifest = json.loads(Path(str(payload["manifest_path"])).read_text(encoding="utf-8"))
        expected_route = {"mode": mode, "intent": intent, "mechanism": mechanism}
        payload_route = payload.get("route")
        manifest_route = manifest.get("route")
        mismatches = [
            key
            for key, value in expected_route.items()
            if not isinstance(payload_route, dict)
            or payload_route.get(key) != value
            or not isinstance(manifest_route, dict)
            or manifest_route.get(key) != value
        ]
        if (manifest.get("managed_lifecycle") is True) != managed_lifecycle:
            mismatches.append("managed_lifecycle")
        if mismatches:
            raise ValueError(f"manifest policy mismatch: {sorted(set(mismatches))}")
        check["verified_policy"] = {
            **expected_route,
            "managed_lifecycle": managed_lifecycle,
            "manifest_path": str(payload["manifest_path"]),
        }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        check["ok"] = False
        check["policy_validation_error"] = str(error)


def _temporary_release_workspace(
    framework_root: Path,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    workspace = tempfile.TemporaryDirectory(prefix="aria-release-")
    path = Path(workspace.name).resolve()
    root = framework_root.resolve()
    if path == root or root in path.parents:
        workspace.cleanup()
        raise ConfigurationError(
            "release-check requires its temporary workspace to be outside the framework root"
        )
    return workspace, path


def run_release_check(
    framework_root: Path | None = None,
    *,
    output_dir: Path | None = None,
) -> dict[str, object]:
    root = _framework_root(framework_root)
    source_git = _candidate_git_identity(root)
    if source_git["clean"] is not True:
        raise ConfigurationError(
            "release-check requires a clean committed Git candidate; "
            f"changes={source_git['changes']}"
        )
    output = (
        output_dir.absolute()
        if output_dir is not None
        else root / ".aria-work" / "release-check" / _stamp()
    )
    if output.exists():
        raise ConfigurationError(f"release-check refuses to overwrite output: {output}")
    logs = output / "logs"
    wheels = output / "wheels"
    rebuilt_wheels = output / "wheels-rebuilt"
    logs.mkdir(parents=True)
    wheels.mkdir()
    rebuilt_wheels.mkdir()
    checks: list[dict[str, object]] = []
    initial_engine = engine_state(root)
    initial_candidate = _candidate_state(root)
    release_workspace, release_scratch = _temporary_release_workspace(root)
    release_temp = release_scratch / "tmp"
    env = _release_environment(
        output,
        temp_root=release_temp,
        source_date_epoch=str(source_git["commit_timestamp"]),
    )
    checks.append(
        {
            "id": "release-environment-isolation",
            "ok": all(
                (
                    env.get("PIP_CACHE_DIR") == str(output / "pip-cache"),
                    env.get("TEMP") == str(release_temp),
                    env.get("TMP") == str(release_temp),
                    env.get("SOURCE_DATE_EPOCH") == source_git["commit_timestamp"],
                    (output / "pip-cache").is_dir(),
                    release_temp.is_dir(),
                    root.resolve() not in release_temp.resolve().parents,
                )
            ),
            "exit_code": 0,
            "pip_cache_dir": env.get("PIP_CACHE_DIR"),
            "temp": env.get("TEMP"),
            "tmp": env.get("TMP"),
            "source_date_epoch": env.get("SOURCE_DATE_EPOCH"),
        }
    )
    checks.append(
        _run(
            "source-framework-doctor",
            [sys.executable, "-B", "-m", "aria", "doctor"],
            cwd=root,
            logs=logs,
            env=env,
        )
    )
    checks.append(
        _run(
            "full-regression",
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=root,
            logs=logs,
            env=env,
            timeout=3600,
        )
    )
    checks.append(
        _run(
            "performance-regression",
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                "tests.test_integrity",
                "-v",
            ],
            cwd=root,
            logs=logs,
            env=env,
            timeout=120,
        )
    )
    venv = output / "venv"
    checks.append(
        _run(
            "create-clean-venv",
            [sys.executable, "-m", "venv", str(venv)],
            cwd=root,
            logs=logs,
            env=env,
        )
    )
    clean_venv_ready = checks[-1]["ok"] is True
    clean_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    clean_aria = venv / ("Scripts/aria.exe" if os.name == "nt" else "bin/aria")
    release_wheelhouse = root / "releases" / __version__
    offline_wheels = (
        sorted(release_wheelhouse.glob("*.whl"))
        if release_wheelhouse.is_dir()
        else []
    )
    dependency_wheels = [
        path for path in offline_wheels if not path.name.startswith("aria_codex-")
    ]
    for dependency_wheel in dependency_wheels:
        shutil.copy2(dependency_wheel, wheels / dependency_wheel.name)
    has_setuptools = any(path.name.startswith("setuptools-") for path in dependency_wheels)
    has_wheel = any(path.name.startswith("wheel-") for path in dependency_wheels)
    checks.append(
        {
            "id": "offline-wheelhouse-seed",
            "ok": bool(dependency_wheels) and has_setuptools and has_wheel,
            "exit_code": 0 if dependency_wheels and has_setuptools and has_wheel else 1,
            "wheelhouse": str(release_wheelhouse),
            "files": [path.name for path in dependency_wheels],
            "actual_result": (
                "Offline runtime and build wheels were seeded"
                if dependency_wheels and has_setuptools and has_wheel
                else "Offline wheelhouse is missing runtime or build tooling wheels"
            ),
        }
    )
    offline_seed_ready = checks[-1]["ok"] is True
    if clean_venv_ready and offline_seed_ready:
        checks.append(
            _run(
                "install-offline-build-tooling",
                [
                    str(clean_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheels),
                    "setuptools>=75",
                    "wheel",
                ],
                cwd=root,
                logs=logs,
                env=env,
            )
        )
    else:
        checks.append(
            {
                "id": "install-offline-build-tooling",
                "ok": False,
                "exit_code": None,
                "skipped": "venv or offline wheelhouse failed",
            }
        )
    if checks[-1]["ok"]:
        checks.append(
            _run(
                "build-wheel-bundle",
                [
                    str(clean_python),
                    "-m",
                    "pip",
                    "wheel",
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    str(root),
                    "--wheel-dir",
                    str(wheels),
                ],
                cwd=root,
                logs=logs,
                env=env,
                timeout=1200,
            )
        )
    else:
        checks.append(
            {
                "id": "build-wheel-bundle",
                "ok": False,
                "exit_code": None,
                "skipped": "venv failed",
            }
        )
    primary_build_ready = checks[-1]["ok"] is True
    if primary_build_ready:
        checks.append(
            _run(
                "rebuild-wheel-bundle",
                [
                    str(clean_python),
                    "-m",
                    "pip",
                    "wheel",
                    "--no-index",
                    "--no-deps",
                    "--no-build-isolation",
                    str(root),
                    "--wheel-dir",
                    str(rebuilt_wheels),
                ],
                cwd=root,
                logs=logs,
                env=env,
                timeout=1200,
            )
        )
    else:
        checks.append(
            {
                "id": "rebuild-wheel-bundle",
                "ok": False,
                "exit_code": None,
                "skipped": "primary wheel build failed",
            }
        )
    wheel_candidates = sorted(wheels.glob("aria_codex-*.whl"))
    rebuilt_candidates = sorted(rebuilt_wheels.glob("aria_codex-*.whl"))
    checks.append(_wheel_reproducibility_check(wheel_candidates, rebuilt_candidates))
    wheel_ready = checks[-1]["ok"] is True
    if wheel_ready:
        checks.append(
            _run(
                "install-wheel-offline",
                [
                    str(clean_python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheels),
                    str(wheel_candidates[-1]),
                ],
                cwd=root,
                logs=logs,
                env=env,
            )
        )
    else:
        checks.append(
            {
                "id": "install-wheel-offline",
                "ok": False,
                "exit_code": None,
                "skipped": "reproducible wheel build failed",
            }
        )
    if checks[-1]["ok"]:
        checks.append(
            _run(
                "installed-pip-check",
                [str(clean_python), "-m", "pip", "check"],
                cwd=root,
                logs=logs,
                env=env,
            )
        )
        checks.append(
            _run(
                "installed-provenance",
                [
                    str(clean_python),
                    "-I",
                    "-c",
                    (
                        "import aria,importlib.metadata,json,pathlib,sys;"
                        "p=pathlib.Path(aria.__file__).resolve();"
                        "v=importlib.metadata.version('aria-codex');"
                        "print(json.dumps({'file':str(p),'version':v,'runtime':aria.__version__,'python':sys.executable}));"
                        f"assert v==aria.__version__=={__version__!r};"
                        "assert pathlib.Path(sys.prefix).resolve() in p.parents"
                    ),
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
        )
        checks.append(
            _run(
                "installed-console-entry",
                [str(clean_aria), "--help"],
                cwd=output,
                logs=logs,
                env=env,
            )
        )
        checks.append(
            _run(
                "installed-framework-doctor",
                [str(clean_aria), "--framework-root", str(root), "doctor"],
                cwd=output,
                logs=logs,
                env=env,
            )
        )
    else:
        checks.extend(
            [
                {"id": "installed-pip-check", "ok": False, "exit_code": None, "skipped": "install failed"},
                {"id": "installed-provenance", "ok": False, "exit_code": None, "skipped": "install failed"},
                {"id": "installed-console-entry", "ok": False, "exit_code": None, "skipped": "install failed"},
                {"id": "installed-framework-doctor", "ok": False, "exit_code": None, "skipped": "install failed"},
            ]
        )
    smoke = release_scratch / "smoke"
    code = smoke / "code"
    docs = smoke / "docs"
    runtime = smoke / "runtime"
    code.mkdir(parents=True)
    atomic_write_bytes(code / "pyproject.toml", b"[project]\nname='aria-release-smoke'\nversion='0.1.0'\n")
    (code / "src").mkdir()
    atomic_write_bytes(code / "src" / "service.py", b"def value():\n    return 1\n")
    atomic_write_bytes(
        code / "release_smoke_test.py",
        (
            "from __future__ import annotations\n"
            "import json\n"
            "import subprocess\n"
            "import sys\n"
            "import tempfile\n"
            "from pathlib import Path\n"
            "from src.service import health, read_records, record\n\n"
            "assert health() == {'status': 'ok'}\n"
            "print('FOCUSED health contract verified')\n"
            "with tempfile.TemporaryDirectory() as directory:\n"
            "    root = Path(directory)\n"
            "    first = record(root, 'mission-1')\n"
            "    second = record(root, 'mission-1')\n"
            "    assert first == second == ['mission-1']\n"
            "    assert read_records(root) == ['mission-1']\n"
            "    print('INTEGRATION durable idempotent state verified')\n"
            "    completed = subprocess.run(\n"
            "        [sys.executable, '-c', "
            "\"import json,sys; from pathlib import Path; from src.service import read_records; \"\n"
            "         \"assert read_records(Path(sys.argv[1])) == ['mission-1']; \"\n"
            "         \"print(json.dumps(read_records(Path(sys.argv[1]))))\", str(root)],\n"
            "        check=False, capture_output=True, text=True,\n"
            "    )\n"
            "    assert completed.returncode == 0, completed.stderr\n"
            "    assert json.loads(completed.stdout) == ['mission-1']\n"
            "    print('E2E subprocess readback verified')\n"
            "    assert (root / 'records.json').is_file()\n"
            "    print('E2E durable side effect verified')\n"
            "    try:\n"
            "        record(root, '../escape')\n"
            "    except ValueError:\n"
            "        print('ADVERSARIAL invalid identifier rejected')\n"
            "    else:\n"
            "        raise AssertionError('invalid identifier accepted')\n"
            "    (root / 'records.json').write_text('{broken', encoding='utf-8')\n"
            "    try:\n"
            "        read_records(root)\n"
            "    except (ValueError, json.JSONDecodeError):\n"
            "        print('ADVERSARIAL corrupt state rejected')\n"
            "    else:\n"
            "        raise AssertionError('corrupt state accepted')\n"
        ).encode("utf-8"),
    )
    smoke_env = dict(env)
    smoke_env["ARIA_RUNTIME_ROOT"] = str(runtime)
    smoke_commands = [
        (
            "smoke-git-init",
            ["git", "init", "-q", "-b", "feature/release-smoke"],
        ),
        ("smoke-git-email", ["git", "config", "user.email", "aria@example.invalid"]),
        ("smoke-git-name", ["git", "config", "user.name", "ARIA Release Check"]),
        ("smoke-git-add", ["git", "add", "."]),
        ("smoke-git-commit", ["git", "commit", "-q", "-m", "fixture"]),
    ]
    for name, command in smoke_commands:
        checks.append(_run(name, command, cwd=code, logs=logs, env=smoke_env))
    installed_ready = any(row.get("id") == "installed-framework-doctor" and row.get("ok") for row in checks)
    if installed_ready and all(row.get("ok") for row in checks[-len(smoke_commands) :]):
        prefix = [str(clean_aria), "--framework-root", str(root)]
        base_prefix = list(prefix)
        checks.append(
            _run(
                "smoke-init",
                prefix
                + [
                    "init",
                    "--project",
                    "release-smoke",
                    "--code-root",
                    str(code),
                    "--docs-root",
                    str(docs),
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        smoke_init_ready = checks[-1].get("ok") is True
        if smoke_init_ready:
            checks.append(
                _run(
                    "smoke-governance-check",
                    prefix + ["governance", "check", "--project", "release-smoke"],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                )
            )
            checks.append(
                _expect_failure(
                    "smoke-governance-control-plane-rejection",
                    prefix
                    + [
                        "feature",
                        "--project",
                        "release-smoke",
                        "--task",
                        "This governed build must not start without a backlog binding",
                    ],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                    contains="Project access bootstrap is incomplete",
                )
            )
        else:
            checks.extend(
                [
                    {
                        "id": "smoke-governance-check",
                        "ok": False,
                        "exit_code": None,
                        "skipped": "smoke init failed",
                    },
                    {
                        "id": "smoke-governance-control-plane-rejection",
                        "ok": False,
                        "exit_code": None,
                        "skipped": "smoke init failed",
                    },
                ]
            )
        try:
            project_path = docs / "PROJECT.yaml"
            project_doc = yaml.safe_load(project_path.read_text(encoding="utf-8"))
            if not isinstance(project_doc, dict) or not isinstance(
                project_doc.get("documents"), dict
            ):
                raise TypeError("generated PROJECT.yaml is malformed")
            project_doc.pop("governance", None)
            project_doc["documents"]["verification"] = "VERIFY.yaml"
            atomic_write_bytes(
                project_path,
                yaml.safe_dump(project_doc, allow_unicode=True, sort_keys=False).encode(
                    "utf-8"
                ),
            )
            atomic_write_bytes(
                docs / "VERIFY.yaml",
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "commands": [
                            {
                                "id": "release-smoke-tests",
                                "adapter": "python",
                                "argv": [
                                    "python",
                                    "release_smoke_test.py",
                                ],
                                "cwd": ".",
                                "classes": [
                                    "focused",
                                    "integration",
                                    "e2e",
                                    "adversarial",
                                ],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ).encode("utf-8"),
            )
            team_path = docs / "ARIA_TEAM.yaml"
            team_doc = yaml.safe_load(team_path.read_text(encoding="utf-8"))
            team_doc["actors"].append(
                {
                    "id": "release-user",
                    "display_name": "Release test user",
                    "type": "human",
                    "roles": ["contributor"],
                }
            )
            atomic_write_bytes(
                team_path,
                yaml.safe_dump(
                    team_doc, allow_unicode=True, sort_keys=False
                ).encode("utf-8"),
            )
            checks.append(
                {
                    "id": "smoke-verification-config",
                    "ok": True,
                    "exit_code": 0,
                    "path": str(docs / "VERIFY.yaml"),
                }
            )
        except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
            checks.append(
                {
                    "id": "smoke-verification-config",
                    "ok": False,
                    "exit_code": 1,
                    "error": str(error),
                }
            )
        owner_request = smoke / "owner-enrollment.json"
        user_request = smoke / "user-enrollment.json"
        checks.append(
            _run(
                "smoke-identity-owner",
                prefix
                + [
                    "identity",
                    "enroll",
                    "--actor",
                    "local-owner",
                    "--device",
                    "release-owner",
                    "--output",
                    str(owner_request),
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        checks.append(
            _run(
                "smoke-access-bootstrap",
                prefix
                + [
                    "--identity-actor",
                    "local-owner",
                    "--identity-device",
                    "release-owner",
                    "access",
                    "bootstrap",
                    "--project",
                    "release-smoke",
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        checks.append(
            _run(
                "smoke-identity-user",
                prefix
                + [
                    "identity",
                    "enroll",
                    "--actor",
                    "release-user",
                    "--device",
                    "release-user-device",
                    "--output",
                    str(user_request),
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        checks.append(
            _run(
                "smoke-access-grant",
                prefix
                + [
                    "--identity-actor",
                    "local-owner",
                    "--identity-device",
                    "release-owner",
                    "access",
                    "grant",
                    "--project",
                    "release-smoke",
                    "--request",
                    str(user_request),
                    "--permission",
                    "project.read",
                    "--permission",
                    "backlog.read",
                    "--permission",
                    "backlog.write",
                    "--permission",
                    "backlog.claim",
                    "--permission",
                    "backlog.close",
                    "--version",
                    "1.5.*",
                    "--branch",
                    "feature/*",
                    "--expected-revision",
                    "1",
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        branch_item_check = _run(
            "smoke-backlog-branch-item",
            base_prefix
            + [
                    "--identity-actor",
                    "local-owner",
                    "--identity-device",
                    "release-owner",
                    "backlog",
                    "add",
                    "--project",
                    "release-smoke",
                    "--title",
                    "Validate branch-scoped collaboration",
                    "--type",
                    "risk",
                    "--priority",
                    "high",
                    "--target-version",
                    __version__,
                    "--acceptance",
                    "A branch-scoped contributor can claim the item",
                    "--source-kind",
                    "release-acceptance",
                    "--source-ref",
                    "branch-scope",
                    "--expected-revision",
                    "0",
            ],
            cwd=output,
            logs=logs,
            env=smoke_env,
        )
        checks.append(branch_item_check)
        mission_item_id: str | None = None
        if branch_item_check.get("ok") is True:
            try:
                branch_item = json.loads(
                    Path(str(branch_item_check["log_path"])).read_text(
                        encoding="utf-8"
                    )
                )
                mission_item_id = str(branch_item["item"]["id"])
            except (KeyError, OSError, TypeError, json.JSONDecodeError):
                mission_item_id = None
        if mission_item_id:
            mission_commit = git_resolve_commit(code, "HEAD")
            collision = _run_claim_collision(
                commands=[
                    (
                        "smoke-v15-mission-owner-claim",
                        base_prefix
                        + [
                            "--identity-actor",
                            "local-owner",
                            "--identity-device",
                            "release-owner",
                            "backlog",
                            "claim",
                            "--project",
                            "release-smoke",
                            "--item",
                            mission_item_id,
                            "--expected-revision",
                            "1",
                        ],
                    ),
                    (
                        "smoke-v15-mission-user-claim",
                        base_prefix
                        + [
                            "--identity-actor",
                            "release-user",
                            "--identity-device",
                            "release-user-device",
                            "backlog",
                            "claim",
                            "--project",
                            "release-smoke",
                            "--item",
                            mission_item_id,
                            "--expected-revision",
                            "1",
                        ],
                    ),
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
            checks.append(collision)
            winner_check = str(collision.get("winner_check_id"))
            winner_identity = (
                ("local-owner", "release-owner")
                if winner_check.endswith("owner-claim")
                else ("release-user", "release-user-device")
            )
            checks.append(
                _run(
                    "smoke-v15-mission-evidence-completion",
                    base_prefix
                    + [
                        "--identity-actor",
                        winner_identity[0],
                        "--identity-device",
                        winner_identity[1],
                        "backlog",
                        "done",
                        "--project",
                        "release-smoke",
                        "--item",
                        mission_item_id,
                        "--evidence",
                        f"git:{mission_commit}",
                        "--expected-revision",
                        "2",
                    ],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                )
            )
            checks.append(
                _expect_failure(
                    "smoke-v15-version-scope-rejection",
                    base_prefix
                    + [
                    "--identity-actor",
                    "release-user",
                    "--identity-device",
                    "release-user-device",
                    "backlog",
                    "audit",
                    "--project",
                    "release-smoke",
                    "--version",
                    "2.0.0",
                    ],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                    contains="is not allowed",
                )
            )
            checks.append(
                _expect_failure(
                    "smoke-v15-branch-scope-rejection",
                    base_prefix
                    + [
                        "--identity-actor",
                        "release-user",
                        "--identity-device",
                        "release-user-device",
                        "backlog",
                        "show",
                        "--project",
                        "release-smoke",
                        "--item",
                        mission_item_id,
                        "--branch",
                        "main",
                    ],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                    contains="does not match",
                )
            )
        else:
            for name in (
                "smoke-v15-mission-claim-collision",
                "smoke-v15-mission-evidence-completion",
                "smoke-v15-version-scope-rejection",
                "smoke-v15-branch-scope-rejection",
            ):
                checks.append(
                    {
                        "id": name,
                        "ok": False,
                        "exit_code": None,
                        "skipped": "backlog mission item id unavailable",
                    }
                )
        prefix = [
            *base_prefix,
            "--identity-actor",
            "local-owner",
            "--identity-device",
            "release-owner",
        ]
        checks.append(
            _run(
                "smoke-project-doctor",
                prefix + ["doctor", "--project", "release-smoke"],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        for name, arguments in (
            ("smoke-status", ["status", "--project", "release-smoke"]),
            ("smoke-history", ["history", "--project", "release-smoke", "--verify"]),
            ("smoke-map-status", ["map-status", "--project", "release-smoke"]),
        ):
            checks.append(
                _run(
                    name,
                    prefix + arguments,
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                )
            )
        checks.append(
            _run(
                "smoke-backlog-readback",
                base_prefix
                + [
                    "--identity-actor",
                    "release-user",
                    "--identity-device",
                    "release-user-device",
                    "backlog",
                    "list",
                    "--project",
                    "release-smoke",
                    "--version",
                    __version__,
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        checks.append(
            _run(
                "smoke-backlog-audit",
                base_prefix
                + [
                    "--identity-actor",
                    "release-user",
                    "--identity-device",
                    "release-user-device",
                    "backlog",
                    "audit",
                    "--project",
                    "release-smoke",
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        deep_spec = docs / "specs" / "active" / "release_health.md"
        deep_spec.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            deep_spec,
            b"# Verified health signal\n\nImplement and verify a durable health signal in src/service.py.\n",
        )
        for name, arguments, policy in (
            (
                "smoke-quick-route",
                [
                    "run",
                    "--project",
                    "release-smoke",
                    "--task",
                    "Read the current service value",
                    "--intent",
                    "build",
                    "--mode",
                    "quick",
                ],
                ("quick", "build", "direct-quick", False),
            ),
            (
                "smoke-deep-design-route",
                [
                    "spec",
                    "--project",
                    "release-smoke",
                    "--task",
                    "Design a durable health reporting contract",
                ],
                ("deep", "design", "spec", False),
            ),
            (
                "smoke-deep-build-route",
                [
                    "feature",
                    "--project",
                    "release-smoke",
                    "--task",
                    "Build the verified health signal from the approved specification",
                    "--mode",
                    "deep",
                    "--spec",
                    "specs/active/release_health.md",
                ],
                ("deep", "build", "next-task-new", True),
            ),
        ):
            check = _run(
                name,
                prefix + arguments,
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
            _validate_run_policy(
                check,
                mode=policy[0],
                intent=policy[1],
                mechanism=policy[2],
                managed_lifecycle=policy[3],
            )
            checks.append(check)
        checks.append(
            _run(
                "smoke-project-canary",
                prefix + ["_project-canary", "--project", "release-smoke"],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
        )
        feature_check = _run(
            "smoke-feature-start",
            prefix
            + [
                "feature",
                "--project",
                "release-smoke",
                "--task",
                "Add a verified health signal",
            ],
            cwd=output,
            logs=logs,
            env=smoke_env,
        )
        _validate_run_policy(
            feature_check,
            mode="standard",
            intent="build",
            mechanism="direct-standard",
            managed_lifecycle=True,
        )
        checks.append(feature_check)
        run_id: str | None = None
        if feature_check.get("ok") is True:
            try:
                feature_output = json.loads(
                    Path(str(feature_check["log_path"])).read_text(encoding="utf-8")
                )
                if isinstance(feature_output, dict) and isinstance(feature_output.get("run_id"), str):
                    run_id = feature_output["run_id"]
            except (OSError, json.JSONDecodeError):
                run_id = None
        if run_id is not None:
            spec_kit = smoke / "spec-kit"
            spec_kit.mkdir()
            atomic_write_bytes(
                spec_kit / "spec.md",
                b"# Release smoke\n\n## Functional Requirements\n\n"
                b"- **FR-001**: Expose a verified health signal\n\n"
                b"## Acceptance Scenarios\n\n"
                b"1. [FR-001] **Given** the fixture, **When** health is read, **Then** a verified signal is returned.\n",
            )
            atomic_write_bytes(
                spec_kit / "plan.md",
                b"# Implementation Plan\n\n## Summary\n\nUse the existing service module.\n\nCoverage: [FR-001]\n",
            )
            atomic_write_bytes(
                spec_kit / "tasks.md",
                b"# Tasks\n\n- [ ] T001 [FR-001] Implement and verify the health signal\n",
            )
            checks.append(
                _run(
                    "smoke-spec-kit-import",
                    prefix
                    + [
                        "contract",
                        "import-spec-kit",
                        "--project",
                        "release-smoke",
                        "--run",
                        run_id,
                        "--spec-dir",
                        str(spec_kit),
                    ],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                )
            )
            checks.append(
                _run(
                    "smoke-implement-lock",
                    prefix
                    + ["implement", "--project", "release-smoke", "--run", run_id],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                )
            )
            atomic_write_bytes(
                code / "src" / "service.py",
                (
                    "from __future__ import annotations\n"
                    "import json\n"
                    "import os\n"
                    "import re\n"
                    "import tempfile\n"
                    "from pathlib import Path\n\n"
                    "_IDENTIFIER = re.compile(r'^[a-z0-9][a-z0-9-]{0,63}$')\n\n"
                    "def value():\n"
                    "    return 1\n\n"
                    "def health():\n"
                    "    return {'status': 'ok'}\n\n"
                    "def read_records(root):\n"
                    "    path = Path(root).resolve() / 'records.json'\n"
                    "    if not path.exists():\n"
                    "        return []\n"
                    "    rows = json.loads(path.read_text(encoding='utf-8'))\n"
                    "    if not isinstance(rows, list) or not all(isinstance(row, str) for row in rows):\n"
                    "        raise ValueError('malformed record state')\n"
                    "    return rows\n\n"
                    "def record(root, identifier):\n"
                    "    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):\n"
                    "        raise ValueError('invalid record identifier')\n"
                    "    root = Path(root).resolve()\n"
                    "    root.mkdir(parents=True, exist_ok=True)\n"
                    "    rows = read_records(root)\n"
                    "    if identifier in rows:\n"
                    "        return rows\n"
                    "    rows.append(identifier)\n"
                    "    handle, temporary = tempfile.mkstemp(prefix='.records-', dir=root)\n"
                    "    try:\n"
                    "        with os.fdopen(handle, 'w', encoding='utf-8') as stream:\n"
                    "            json.dump(rows, stream)\n"
                    "            stream.flush()\n"
                    "            os.fsync(stream.fileno())\n"
                    "        os.replace(temporary, root / 'records.json')\n"
                    "    finally:\n"
                    "        if os.path.exists(temporary):\n"
                    "            os.unlink(temporary)\n"
                    "    return rows\n"
                ).encode("utf-8"),
            )
            links_path = smoke / "verification-links.json"
            atomic_write_json(
                links_path,
                {
                    "schema_version": 1,
                    "commands": {
                        "release-smoke-tests": {
                            "requirement_ids": ["R-001"],
                            "acceptance_ids": ["AC-001"],
                        }
                    },
                },
            )
            verify_check = _run(
                "smoke-trusted-verify",
                prefix
                + [
                    "verify",
                    "--project",
                    "release-smoke",
                    "--run",
                    run_id,
                    "--links",
                    str(links_path),
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
            checks.append(verify_check)
            if verify_check.get("ok") is True:
                try:
                    verify_output = json.loads(
                        Path(str(verify_check["log_path"])).read_text(encoding="utf-8")
                    )
                    manifest_path = Path(str(feature_output["manifest_path"]))
                    run_root = manifest_path.parent
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    template = json.loads(
                        Path(str(verify_output["verification_template_path"])).read_text(
                            encoding="utf-8"
                        )
                    )
                    test_row = template["tests"][0]
                    test_row["actual_result"] = (
                        "Verified health, durable idempotent state, subprocess "
                        "readback, side effect and fail-closed input/state handling"
                    )
                    test_row["scenario"] = {
                        "initial_state": "Installed wheel and disposable project are ready",
                        "actions": [
                            "assert the health contract",
                            "write the same durable record twice",
                            "read the record from a separate process",
                            "reject traversal input and corrupt persisted state",
                        ],
                        "expected_result": (
                            "Health is ok, one durable record exists and both "
                            "adversarial cases are rejected"
                        ),
                        "forbidden_result": (
                            "Duplicate state, missing readback or invalid input/state acceptance"
                        ),
                        "side_effects": (
                            "Only records.json under the disposable temporary directory exists"
                        ),
                        "correlation": run_id,
                        "parallelism_or_load": (
                            "One installed-wheel verification with a child-process readback"
                        ),
                        "actual_result": (
                            "All six concrete assertions and rejection paths completed"
                        ),
                    }
                    test_row["class_evidence"] = {
                        "focused": {
                            "proof_excerpts": ["FOCUSED health contract verified"]
                        },
                        "integration": {
                            "proof_excerpts": [
                                "INTEGRATION durable idempotent state verified"
                            ]
                        },
                        "e2e": {
                            "proof_excerpts": [
                                "E2E subprocess readback verified",
                                "E2E durable side effect verified",
                            ]
                        },
                        "adversarial": {
                            "proof_excerpts": [
                                "ADVERSARIAL invalid identifier rejected",
                                "ADVERSARIAL corrupt state rejected",
                            ]
                        },
                    }
                    contract_relative = str(
                        manifest["feature_contract"]["artifact"]
                    )
                    contract_path = run_root / contract_relative
                    contract = json.loads(contract_path.read_text(encoding="utf-8"))
                    contract_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
                    oracle = str(contract["acceptance"][0]["oracle"])
                    task_id = str(contract["tasks"][0]["id"])
                    convergence_relative = str(
                        manifest["feature_contract"]["convergence"]["artifact"]
                    )
                    convergence_path = run_root / convergence_relative
                    atomic_write_json(
                        convergence_path,
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "feature_contract_sha256": contract_sha,
                            "verdict": "converged",
                            "tasks": [{"id": task_id, "status": "completed"}],
                            "requirements": [
                                {
                                    "id": "R-001",
                                    "status": "proven",
                                    "task_ids": [task_id],
                                    "implementation_paths": ["src/service.py"],
                                    "acceptance_results": [
                                        {
                                            "id": "AC-001",
                                            "status": "proven",
                                            "oracle": oracle,
                                            "evidence_refs": [
                                                {
                                                    "verification_index": 0,
                                                    "classes": ["focused"],
                                                    "proof_excerpts": [
                                                        "FOCUSED health contract verified"
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                    )
                    target_check = _run(
                        "smoke-role-target",
                        prefix
                        + [
                            "_role-target",
                            "--project",
                            "release-smoke",
                            "--run",
                            run_id,
                        ],
                        cwd=output,
                        logs=logs,
                        env=smoke_env,
                    )
                    checks.append(target_check)
                    target = json.loads(
                        Path(str(target_check["log_path"])).read_text(encoding="utf-8")
                    )
                    role_relative = "outputs/roles/independent_reviewer.json"
                    role_path = run_root / role_relative
                    atomic_write_json(
                        role_path,
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "role": "independent_reviewer",
                            "agent_id": "synthetic-role-gate-fixture",
                            "context_sha256": manifest["context"]["context_sha256"],
                            "target_kind": target["kind"],
                            "target_sha256": target["sha256"],
                            "independent": True,
                            "changed_code": False,
                            "changed_documents": False,
                            "git_operations": False,
                            "verdict": "pass",
                            "summary": (
                                "Synthetic installed-wheel fixture validates role-evidence "
                                "schema and gate plumbing only"
                            ),
                            "synthetic_fixture": True,
                            "findings": [],
                        },
                    )
                    result_path = smoke / "converge-result.json"
                    atomic_write_json(
                        result_path,
                        {
                            "status": "completed",
                            "summary": "Installed wheel completed trusted feature smoke",
                            "changed_files": ["src/service.py"],
                            "tests": [test_row],
                            "feature_contract": {
                                "path": contract_relative,
                                "sha256": contract_sha,
                            },
                            "convergence": {
                                "path": convergence_relative,
                                "sha256": hashlib.sha256(
                                    convergence_path.read_bytes()
                                ).hexdigest(),
                            },
                            "role_evidence": [
                                {
                                    "role": "independent_reviewer",
                                    "agent_id": "synthetic-role-gate-fixture",
                                    "artifact_path": role_relative,
                                    "artifact_sha256": hashlib.sha256(
                                        role_path.read_bytes()
                                    ).hexdigest(),
                                }
                            ],
                            "read_back": "Receipt, output, Git delta and closure were read back",
                            "review": (
                                "Synthetic role-evidence gate fixture passed; it is not "
                                "the required semantic release review"
                            ),
                            "closure": "R-001 and AC-001 converged from trusted evidence",
                        },
                    )
                    checks.append(
                        _run(
                            "smoke-converge",
                            prefix
                            + [
                                "converge",
                                "--project",
                                "release-smoke",
                                "--run",
                                run_id,
                                "--result",
                                str(result_path),
                            ],
                            cwd=output,
                            logs=logs,
                            env=smoke_env,
                        )
                    )
                except (KeyError, IndexError, OSError, json.JSONDecodeError) as error:
                    checks.extend(
                        [
                            {
                                "id": "smoke-role-target",
                                "ok": False,
                                "exit_code": None,
                                "error": str(error),
                            },
                            {
                                "id": "smoke-converge",
                                "ok": False,
                                "exit_code": None,
                                "error": str(error),
                            },
                        ]
                    )
            else:
                checks.extend(
                    [
                        {
                            "id": "smoke-role-target",
                            "ok": False,
                            "exit_code": None,
                            "skipped": "trusted verify failed",
                        },
                        {
                            "id": "smoke-converge",
                            "ok": False,
                            "exit_code": None,
                            "skipped": "trusted verify failed",
                        },
                    ]
                )
            checks.append(
                _run(
                    "smoke-lifecycle-readback",
                    prefix
                    + ["lifecycle", "--project", "release-smoke", "--run", run_id],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                )
            )
            checks.extend(
                _v15_installed_smoke(
                    prefix=prefix,
                    framework_root=root,
                    wheels=wheels,
                    code=code,
                    docs=docs,
                    smoke=smoke,
                    output=output,
                    logs=logs,
                    env=smoke_env,
                    completed_run_id=run_id,
                )
            )
            activation_check = _run(
                "smoke-v15-active-cutover",
                prefix
                + [
                    "_project-activate",
                    "--project",
                    "release-smoke",
                ],
                cwd=output,
                logs=logs,
                env=smoke_env,
            )
            if activation_check.get("ok") is True:
                try:
                    activation = json.loads(
                        Path(str(activation_check["log_path"])).read_text(
                            encoding="utf-8"
                        )
                    )
                    verified = (
                        activation.get("mode") == "active"
                        and activation.get("action") == "active-cutover"
                        and activation.get("doctor_ok") is True
                        and activation.get("state_before_sha256")
                        != activation.get("state_after_sha256")
                        and activation.get("history_before_sha256")
                        != activation.get("history_after_sha256")
                    )
                    activation_check["ok"] = verified
                    activation_check["verified_readback"] = {
                        "mode": activation.get("mode"),
                        "action": activation.get("action"),
                        "doctor_ok": activation.get("doctor_ok"),
                        "state_changed": activation.get("state_before_sha256")
                        != activation.get("state_after_sha256"),
                        "history_changed": activation.get("history_before_sha256")
                        != activation.get("history_after_sha256"),
                    }
                    activation_check["actual_result"] = (
                        "Shadow project atomically activated and read back"
                        if verified
                        else "Active cutover output failed read-back validation"
                    )
                except (OSError, TypeError, json.JSONDecodeError) as error:
                    activation_check["ok"] = False
                    activation_check["activation_validation_error"] = str(error)
            checks.append(activation_check)
            checks.append(
                _run(
                    "smoke-v15-active-project-doctor",
                    prefix + ["doctor", "--project", "release-smoke"],
                    cwd=output,
                    logs=logs,
                    env=smoke_env,
                )
            )
        else:
            checks.extend(
                [
                    {"id": "smoke-spec-kit-import", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                    {"id": "smoke-implement-lock", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                    {"id": "smoke-trusted-verify", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                    {"id": "smoke-role-target", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                    {"id": "smoke-converge", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                    {"id": "smoke-lifecycle-readback", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                    {"id": "smoke-v14-installed-flow", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                    {"id": "smoke-v15-active-cutover", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                    {"id": "smoke-v15-active-project-doctor", "ok": False, "exit_code": None, "skipped": "feature run id unavailable"},
                ]
            )
    else:
        checks.extend(
            [
                {"id": "smoke-init", "ok": False, "exit_code": None, "skipped": "installed CLI or Git fixture failed"},
                {"id": "smoke-verification-config", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-project-doctor", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-status", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-history", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-map-status", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-quick-route", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-deep-design-route", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-deep-build-route", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-project-canary", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-feature-start", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-spec-kit-import", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-implement-lock", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-trusted-verify", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-role-target", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-converge", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-lifecycle-readback", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-v14-installed-flow", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-v15-active-cutover", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
                {"id": "smoke-v15-active-project-doctor", "ok": False, "exit_code": None, "skipped": "smoke init failed"},
            ]
        )
    final_engine = engine_state(root)
    final_candidate = _candidate_state(root)
    checks.append(
        {
            "id": "candidate-engine-stability",
            "ok": (
                initial_engine.get("sha256") == final_engine.get("sha256")
                and initial_engine.get("files") == final_engine.get("files")
                and initial_candidate.get("sha256") == final_candidate.get("sha256")
            ),
            "exit_code": 0,
            "initial_sha256": initial_engine.get("sha256"),
            "final_sha256": final_engine.get("sha256"),
            "initial_candidate_sha256": initial_candidate.get("sha256"),
            "final_candidate_sha256": final_candidate.get("sha256"),
            "candidate_file_count": initial_candidate.get("count"),
        }
    )
    report = {
        "schema_version": 1,
        "ok": all(row.get("ok") is True for row in checks),
        "framework_root": str(root),
        "source_git": source_git,
        "candidate_engine": {
            "initial_sha256": initial_engine.get("sha256"),
            "final_sha256": final_engine.get("sha256"),
            "initial_candidate_sha256": initial_candidate.get("sha256"),
            "final_candidate_sha256": final_candidate.get("sha256"),
        },
        "output_dir": str(output),
        "wheel": str(wheel_candidates[-1]) if wheel_candidates else None,
        "wheel_sha256": (
            hashlib.sha256(wheel_candidates[-1].read_bytes()).hexdigest()
            if wheel_candidates
            else None
        ),
        "environment": {
            "pip_cache_dir": env.get("PIP_CACHE_DIR"),
            "temp": env.get("TEMP"),
            "tmp": env.get("TMP"),
            "source_date_epoch": env.get("SOURCE_DATE_EPOCH"),
            "smoke_root": str(smoke),
        },
        "checks": checks,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    report_path = output / "release-acceptance.json"
    atomic_write_json(report_path, report)
    report["report_path"] = str(report_path)
    release_workspace.cleanup()
    return report


def _v15_installed_smoke(
    *,
    prefix: list[str],
    framework_root: Path,
    wheels: Path,
    code: Path,
    docs: Path,
    smoke: Path,
    output: Path,
    logs: Path,
    env: dict[str, str],
    completed_run_id: str,
) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []
    private_key = smoke / "github-actions-private.pem"
    public_key = smoke / "github-actions-public.pem"
    authorizer_private = smoke / "job-authorizer-private.pem"
    authorizer_public = smoke / "job-authorizer-public.pem"
    checks.append(
        _run(
            "smoke-v14-key-generate",
            prefix
            + [
                "key",
                "generate",
                "--private-key",
                str(private_key),
                "--public-key",
                str(public_key),
            ],
            cwd=output,
            logs=logs,
            env=env,
        )
    )
    checks.append(
        _expect_failure(
            "smoke-v15-upgrade-open-run-rejection",
            prefix + ["upgrade-1-5", "--project", "release-smoke"],
            cwd=output,
            logs=logs,
            env=env,
            contains="Close or reconcile open ARIA runs",
        )
    )
    legacy_release = framework_root / "releases" / "1.3.0"
    legacy_manifest_path = legacy_release / "manifest.json"
    legacy_wheel = legacy_release / "aria_codex-1.3.0-py3-none-any.whl"
    legacy_wheel_ready = False
    try:
        legacy_manifest = json.loads(
            legacy_manifest_path.read_text(encoding="utf-8")
        )
        legacy_wheel_sha = hashlib.sha256(legacy_wheel.read_bytes()).hexdigest()
        legacy_wheel_ready = (
            legacy_manifest.get("version") == "1.3.0"
            and legacy_manifest.get("wheel") == legacy_wheel.name
            and legacy_manifest.get("wheel_sha256") == legacy_wheel_sha
        )
        legacy_wheel_check: dict[str, object] = {
            "id": "smoke-v14-legacy-wheel",
            "ok": legacy_wheel_ready,
            "exit_code": 0 if legacy_wheel_ready else 1,
            "wheel": str(legacy_wheel),
            "wheel_sha256": legacy_wheel_sha,
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        legacy_wheel_check = {
            "id": "smoke-v14-legacy-wheel",
            "ok": False,
            "exit_code": None,
            "error": str(error),
        }
    checks.append(legacy_wheel_check)
    legacy_venv = smoke / "legacy-1.3-venv"
    legacy_python = legacy_venv / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    legacy_aria = legacy_venv / (
        "Scripts/aria.exe" if os.name == "nt" else "bin/aria"
    )
    legacy_venv_check = (
        _run(
            "smoke-v14-legacy-venv",
            [sys.executable, "-m", "venv", str(legacy_venv)],
            cwd=output,
            logs=logs,
            env=env,
        )
        if legacy_wheel_ready
        else {
            "id": "smoke-v14-legacy-venv",
            "ok": False,
            "exit_code": None,
            "skipped": "released 1.3 wheel unavailable",
        }
    )
    checks.append(legacy_venv_check)
    legacy_install = (
        _run(
            "smoke-v14-legacy-install",
            [
                str(legacy_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheels),
                str(legacy_wheel),
            ],
            cwd=output,
            logs=logs,
            env=env,
        )
        if legacy_venv_check.get("ok") is True
        else {
            "id": "smoke-v14-legacy-install",
            "ok": False,
            "exit_code": None,
            "skipped": "legacy venv unavailable",
        }
    )
    checks.append(legacy_install)
    legacy_docs = smoke / "legacy-1.3-docs"
    legacy_code = smoke / "legacy-1.3-code"
    legacy_framework = smoke / "legacy-1.3-framework"
    try:
        if legacy_install.get("ok") is not True:
            raise OSError("released 1.3 wheel is not installed")
        installed_aria = subprocess.run(
            [
                str(legacy_python),
                "-I",
                "-c",
                "import aria,pathlib; print(pathlib.Path(aria.__file__).parent)",
            ],
            cwd=output,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        ).stdout.strip()
        shutil.copytree(Path(installed_aria), legacy_framework / "aria")
        atomic_write_bytes(legacy_framework / ".aria-root", b"aria-codex\n")
        atomic_write_bytes(
            legacy_framework / "pyproject.toml",
            b"[project]\nname='aria-codex'\nversion='1.3.0'\n",
        )
        legacy_code.mkdir()
        setup_commands = [
            ["git", "init", "-q"],
            ["git", "config", "user.email", "aria@example.invalid"],
            ["git", "config", "user.name", "ARIA Migration Smoke"],
        ]
        for command in setup_commands:
            subprocess.run(
                command,
                cwd=legacy_code,
                check=True,
                capture_output=True,
                env=env,
                timeout=60,
            )
        atomic_write_bytes(
            legacy_code / "pyproject.toml",
            b"[project]\nname='aria-legacy-smoke'\nversion='0.1.0'\n",
        )
        subprocess.run(
            ["git", "add", "."],
            cwd=legacy_code,
            check=True,
            capture_output=True,
            env=env,
            timeout=60,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "legacy fixture"],
            cwd=legacy_code,
            check=True,
            capture_output=True,
            env=env,
            timeout=60,
        )
        checks.append(
            {
                "id": "smoke-v14-legacy-fixture",
                "ok": True,
                "exit_code": 0,
                "source": "released-wheel",
                "wheel_sha256": legacy_wheel_sha,
                "framework": str(legacy_framework),
                "docs": str(legacy_docs),
                "code": str(legacy_code),
            }
        )
    except (KeyError, OSError, TypeError, yaml.YAMLError, subprocess.SubprocessError) as error:
        checks.append(
            {
                "id": "smoke-v14-legacy-fixture",
                "ok": False,
                "exit_code": None,
                "error": str(error),
            }
        )
    legacy_init = (
        _run(
            "smoke-v14-legacy-init",
            [
                str(legacy_aria),
                "--framework-root",
                str(legacy_framework),
                "init",
                "--project",
                "release-legacy",
                "--code-root",
                str(legacy_code),
                "--docs-root",
                str(legacy_docs),
                "--display-name",
                "Release legacy migration fixture",
            ],
            cwd=output,
            logs=logs,
            env=env,
        )
        if checks[-1].get("ok") is True
        else {
            "id": "smoke-v14-legacy-init",
            "ok": False,
            "exit_code": None,
            "skipped": "legacy fixture unavailable",
        }
    )
    checks.append(legacy_init)
    legacy_upgrade = (
        _run(
            "smoke-v14-upgrade-from-1-3",
            prefix + ["upgrade-1-4", "--project", "release-legacy"],
            cwd=output,
            logs=logs,
            env=env,
        )
        if legacy_init.get("ok") is True
        else {
            "id": "smoke-v14-upgrade-from-1-3",
            "ok": False,
            "exit_code": None,
            "skipped": "legacy 1.3 project unavailable",
        }
    )
    checks.append(legacy_upgrade)
    legacy_upgrade_15 = (
        _run(
            "smoke-v15-upgrade-from-1-4",
            prefix + ["upgrade-1-5", "--project", "release-legacy"],
            cwd=output,
            logs=logs,
            env=env,
        )
        if legacy_upgrade.get("ok") is True
        else {
            "id": "smoke-v15-upgrade-from-1-4",
            "ok": False,
            "exit_code": None,
            "skipped": "legacy 1.4 migration failed",
        }
    )
    checks.append(legacy_upgrade_15)
    legacy_access = (
        _run(
            "smoke-v15-legacy-access-bootstrap",
            prefix + ["access", "bootstrap", "--project", "release-legacy"],
            cwd=output,
            logs=logs,
            env=env,
        )
        if legacy_upgrade_15.get("ok") is True
        else {
            "id": "smoke-v15-legacy-access-bootstrap",
            "ok": False,
            "exit_code": None,
            "skipped": "legacy 1.5 migration failed",
        }
    )
    checks.append(legacy_access)
    checks.append(
        _run(
            "smoke-v14-upgraded-project-doctor",
            prefix + ["doctor", "--project", "release-legacy"],
            cwd=output,
            logs=logs,
            env=env,
        )
        if legacy_access.get("ok") is True
        else {
            "id": "smoke-v14-upgraded-project-doctor",
            "ok": False,
            "exit_code": None,
            "skipped": "legacy migration failed",
        }
    )
    checks.append(
        _run(
            "smoke-v14-upgraded-project-canary",
            prefix + ["_project-canary", "--project", "release-legacy"],
            cwd=output,
            logs=logs,
            env=env,
        )
        if legacy_access.get("ok") is True
        else {
            "id": "smoke-v14-upgraded-project-canary",
            "ok": False,
            "exit_code": None,
            "skipped": "legacy migration failed",
        }
    )
    checks.append(
        _run(
            "smoke-v14-upgraded-team-status",
            prefix + ["team", "status", "--project", "release-legacy"],
            cwd=output,
            logs=logs,
            env=env,
        )
        if legacy_access.get("ok") is True
        else {
            "id": "smoke-v14-upgraded-team-status",
            "ok": False,
            "exit_code": None,
            "skipped": "legacy migration failed",
        }
    )
    exported_run = smoke / "completed-run.aria-evidence"
    export_check = _run(
        "smoke-v14-evidence-export",
        prefix
        + [
            "evidence",
            "export",
            "--project",
            "release-smoke",
            "--run",
            completed_run_id,
            "--output",
            str(exported_run),
            "--private-key",
            str(private_key),
            "--actor",
            "github-actions",
        ],
        cwd=output,
        logs=logs,
        env=env,
    )
    checks.append(export_check)
    checks.append(
        _run(
            "smoke-v14-evidence-inspect",
            prefix + ["evidence", "inspect", "--package", str(exported_run)],
            cwd=output,
            logs=logs,
            env=env,
        )
        if export_check.get("ok") is True
        else {
            "id": "smoke-v14-evidence-inspect",
            "ok": False,
            "exit_code": None,
            "skipped": "run export failed",
        }
    )
    checks.append(
        _run(
            "smoke-v14-job-key-generate",
            prefix
            + [
                "key",
                "generate",
                "--private-key",
                str(authorizer_private),
                "--public-key",
                str(authorizer_public),
            ],
            cwd=output,
            logs=logs,
            env=env,
        )
    )
    team_status_check = _run(
        "smoke-v14-team-status",
        prefix + ["team", "status", "--project", "release-smoke"],
        cwd=output,
        logs=logs,
        env=env,
    )
    checks.append(team_status_check)
    revision: int | None = None
    if team_status_check.get("ok") is True:
        try:
            team_status = json.loads(
                Path(str(team_status_check["log_path"])).read_text(encoding="utf-8")
            )
            revision = int(team_status["revision"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            revision = None
    claim_check: dict[str, object]
    if revision is not None:
        claim_check = _run(
            "smoke-v14-team-claim",
            prefix
            + [
                "team",
                "claim",
                "--project",
                "release-smoke",
                "--task",
                "release-smoke-v14",
                "--actor",
                "local-owner",
                "--expected-revision",
                str(revision),
                "--ttl-seconds",
                "300",
            ],
            cwd=output,
            logs=logs,
            env=env,
        )
    else:
        claim_check = {
            "id": "smoke-v14-team-claim",
            "ok": False,
            "exit_code": None,
            "skipped": "team status unavailable",
        }
    checks.append(claim_check)
    if claim_check.get("ok") is True:
        try:
            claim = json.loads(
                Path(str(claim_check["log_path"])).read_text(encoding="utf-8")
            )
            release_arguments = [
                "team",
                "release",
                "--project",
                "release-smoke",
                "--task",
                "release-smoke-v14",
                "--actor",
                "local-owner",
                "--token",
                str(claim["lease"]["token"]),
                "--expected-revision",
                str(claim["revision"]),
            ]
            checks.append(
                _run(
                    "smoke-v14-team-release",
                    prefix + release_arguments,
                    cwd=output,
                    logs=logs,
                    env=env,
                )
            )
        except (KeyError, OSError, TypeError, json.JSONDecodeError) as error:
            checks.append(
                {
                    "id": "smoke-v14-team-release",
                    "ok": False,
                    "exit_code": None,
                    "error": str(error),
                }
            )
    else:
        checks.append(
            {
                "id": "smoke-v14-team-release",
                "ok": False,
                "exit_code": None,
                "skipped": "task claim failed",
            }
        )
    workflow_path = smoke / "aria-ci.yml"
    checks.append(
        _run(
            "smoke-v14-github-adapter",
            prefix
            + [
                "ci",
                "github",
                "--project",
                "release-smoke",
                "--output",
                str(workflow_path),
            ],
            cwd=output,
            logs=logs,
            env=env,
        )
    )
    checks.append(
        _run(
            "smoke-v14-git-add",
            ["git", "add", "."],
            cwd=code,
            logs=logs,
            env=env,
        )
    )
    checks.append(
        _run(
            "smoke-v14-git-commit",
            ["git", "commit", "-q", "-m", "verified health"],
            cwd=code,
            logs=logs,
            env=env,
        )
    )
    ci_run_check = _run(
        "smoke-v14-ci-run",
        prefix
        + [
            "run",
            "--project",
            "release-smoke",
            "--task",
            "Verify committed health signal in trusted CI",
            "--intent",
            "build",
            "--mode",
            "quick",
        ],
        cwd=output,
        logs=logs,
        env=env,
    )
    checks.append(ci_run_check)
    ci_run_id: str | None = None
    if ci_run_check.get("ok") is True:
        try:
            ci_run = json.loads(
                Path(str(ci_run_check["log_path"])).read_text(encoding="utf-8")
            )
            if isinstance(ci_run.get("run_id"), str):
                ci_run_id = ci_run["run_id"]
        except (OSError, json.JSONDecodeError):
            pass
    job_path = smoke / "aria-ci-job.json"
    unsigned_path = smoke / "aria-ci-unsigned.zip"
    package_path = smoke / "aria-ci-result.aria-evidence"
    job_trust_path = smoke / "JOB_TRUST.yaml"
    if ci_run_id is not None:
        prepare_check = _run(
            "smoke-v14-ci-prepare",
            prefix
            + [
                "ci",
                "prepare",
                "--project",
                "release-smoke",
                "--run",
                ci_run_id,
                "--output",
                str(job_path),
                "--private-key",
                str(authorizer_private),
                "--actor",
                "local-owner",
            ],
            cwd=output,
            logs=logs,
            env=env,
        )
        checks.append(prepare_check)
        job_trust_ready = False
        if prepare_check.get("ok") is True:
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
                authorization = job["authorization"]
                atomic_write_bytes(
                    job_trust_path,
                    yaml.safe_dump(
                        {
                            "schema_version": 1,
                            "keys": [
                                {
                                    "id": authorization["key_id"],
                                    "public_key": authorization["public_key"],
                                    "actor_id": "local-owner",
                                    "status": "trusted",
                                }
                            ],
                            "policies": {
                                "job": {
                                    "minimum_trust_level": "signed",
                                    "trusted_keys": [authorization["key_id"]],
                                }
                            },
                        },
                        sort_keys=False,
                    ).encode("utf-8"),
                )
                job_trust_ready = True
            except (KeyError, OSError, TypeError, json.JSONDecodeError):
                job_trust_ready = False
        execute_check = (
            _run(
                "smoke-v14-ci-execute",
                prefix
                + [
                    "ci",
                    "execute",
                    "--job",
                    str(job_path),
                    "--checkout",
                    str(code),
                    "--output",
                    str(unsigned_path),
                    "--job-trust-policy",
                    str(job_trust_path),
                    "--job-policy",
                    "job",
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if job_trust_ready
            else {
                "id": "smoke-v14-ci-execute",
                "ok": False,
                "exit_code": None,
                "skipped": "CI prepare or key generation failed",
            }
        )
        checks.append(execute_check)
        attest_check = (
            _run(
                "smoke-v14-ci-attest",
                prefix
                + [
                    "ci",
                    "attest",
                    "--job",
                    str(job_path),
                    "--result",
                    str(unsigned_path),
                    "--output",
                    str(package_path),
                    "--private-key",
                    str(private_key),
                    "--actor",
                    "github-actions",
                    "--job-trust-policy",
                    str(job_trust_path),
                    "--job-policy",
                    "job",
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if execute_check.get("ok") is True and checks[0].get("ok") is True
            else {
                "id": "smoke-v14-ci-attest",
                "ok": False,
                "exit_code": None,
                "skipped": "unsigned CI result or attester key unavailable",
            }
        )
        checks.append(attest_check)
        trust_ready = False
        if attest_check.get("ok") is True:
            try:
                with zipfile.ZipFile(package_path) as archive:
                    signature = json.loads(archive.read("signature.json"))
                job = json.loads(job_path.read_text(encoding="utf-8"))
                authorization = job["authorization"]
                _merge_trust_policy(
                    docs / "TRUST.yaml",
                    keys=[
                        {
                            "id": authorization["key_id"],
                            "public_key": authorization["public_key"],
                            "actor_id": "local-owner",
                            "status": "trusted",
                        },
                        {
                            "id": signature["key_id"],
                            "public_key": signature["public_key"],
                            "actor_id": "github-actions",
                            "status": "trusted",
                        },
                    ],
                    policies={
                        "job": {
                            "minimum_trust_level": "signed",
                            "trusted_keys": [authorization["key_id"]],
                        },
                        "ci": {
                            "minimum_trust_level": "ci-signed",
                            "trusted_keys": [signature["key_id"]],
                        },
                    },
                )
                trust_ready = True
            except (KeyError, OSError, zipfile.BadZipFile, json.JSONDecodeError):
                trust_ready = False
        for name, arguments in (
            (
                "smoke-v14-evidence-verify",
                [
                    "evidence",
                    "verify",
                    "--package",
                    str(package_path),
                    "--trust-policy",
                    str(docs / "TRUST.yaml"),
                    "--policy",
                    "ci",
                ],
            ),
            (
                "smoke-v14-ci-import",
                [
                    "ci",
                    "import",
                    "--project",
                    "release-smoke",
                    "--run",
                    ci_run_id,
                    "--job",
                    str(job_path),
                    "--package",
                    str(package_path),
                    "--trust-policy",
                    str(docs / "TRUST.yaml"),
                    "--policy",
                    "ci",
                    "--job-trust-policy",
                    str(docs / "TRUST.yaml"),
                    "--job-policy",
                    "job",
                ],
            ),
        ):
            checks.append(
                _run(
                    name,
                    prefix + arguments,
                    cwd=output,
                    logs=logs,
                    env=env,
                )
                if trust_ready
                else {
                    "id": name,
                    "ok": False,
                    "exit_code": None,
                    "skipped": "signed CI package or trust policy unavailable",
                }
            )
        gate_prerequisites = (
            trust_ready
            and export_check.get("ok") is True
            and execute_check.get("ok") is True
            and attest_check.get("ok") is True
        )
        source_two_job = smoke / "aria-ci-source-two-job.json"
        source_two_unsigned = smoke / "aria-ci-source-two-unsigned.zip"
        source_two_package = smoke / "aria-ci-source-two.aria-evidence"
        source_two_prepare = (
            _run(
                "smoke-v14-source-two-prepare",
                prefix
                + [
                    "ci",
                    "prepare",
                    "--project",
                    "release-smoke",
                    "--run",
                    ci_run_id,
                    "--output",
                    str(source_two_job),
                    "--private-key",
                    str(authorizer_private),
                    "--actor",
                    "local-owner",
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if gate_prerequisites
            else {
                "id": "smoke-v14-source-two-prepare",
                "ok": False,
                "exit_code": None,
                "skipped": "primary CI evidence unavailable",
            }
        )
        checks.append(source_two_prepare)
        source_two_execute = (
            _run(
                "smoke-v14-source-two-execute",
                prefix
                + [
                    "ci",
                    "execute",
                    "--job",
                    str(source_two_job),
                    "--checkout",
                    str(code),
                    "--output",
                    str(source_two_unsigned),
                    "--job-trust-policy",
                    str(job_trust_path),
                    "--job-policy",
                    "job",
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if source_two_prepare.get("ok") is True
            else {
                "id": "smoke-v14-source-two-execute",
                "ok": False,
                "exit_code": None,
                "skipped": "second source job unavailable",
            }
        )
        checks.append(source_two_execute)
        source_two_attest = (
            _run(
                "smoke-v14-source-two-attest",
                prefix
                + [
                    "ci",
                    "attest",
                    "--job",
                    str(source_two_job),
                    "--result",
                    str(source_two_unsigned),
                    "--output",
                    str(source_two_package),
                    "--private-key",
                    str(private_key),
                    "--actor",
                    "github-actions",
                    "--job-trust-policy",
                    str(job_trust_path),
                    "--job-policy",
                    "job",
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if source_two_execute.get("ok") is True
            else {
                "id": "smoke-v14-source-two-attest",
                "ok": False,
                "exit_code": None,
                "skipped": "second source result unavailable",
            }
        )
        checks.append(source_two_attest)
        gate_prerequisites = (
            gate_prerequisites and source_two_attest.get("ok") is True
        )
        reviewer_private = smoke / "release-reviewer-private.pem"
        reviewer_public = smoke / "release-reviewer-public.pem"
        reviewer_key_check = (
            _run(
                "smoke-v14-reviewer-key-generate",
                prefix
                + [
                    "key",
                    "generate",
                    "--private-key",
                    str(reviewer_private),
                    "--public-key",
                    str(reviewer_public),
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if gate_prerequisites
            else {
                "id": "smoke-v14-reviewer-key-generate",
                "ok": False,
                "exit_code": None,
                "skipped": "source CI or export evidence unavailable",
            }
        )
        checks.append(reviewer_key_check)
        team_ready = False
        if reviewer_key_check.get("ok") is True:
            try:
                team_path = docs / "ARIA_TEAM.yaml"
                team = yaml.safe_load(team_path.read_text(encoding="utf-8"))
                actors = team["actors"]
                if not any(
                    isinstance(row, dict)
                    and row.get("id") == "release-reviewer"
                    for row in actors
                ):
                    actors.append(
                        {
                            "id": "release-reviewer",
                            "display_name": "Release reviewer",
                            "type": "human",
                            "roles": ["reviewer"],
                        }
                    )
                atomic_write_bytes(
                    team_path,
                    yaml.safe_dump(team, sort_keys=False).encode("utf-8"),
                )
                team_ready = True
            except (KeyError, OSError, TypeError, yaml.YAMLError):
                team_ready = False
        integration_job = smoke / "aria-integration-job.json"
        integration_unsigned = smoke / "aria-integration-unsigned.zip"
        integration_package = smoke / "aria-integration.aria-evidence"
        integration_prepare = (
            _run(
                "smoke-v14-integration-prepare",
                prefix
                + [
                    "ci",
                    "prepare",
                    "--project",
                    "release-smoke",
                    "--run",
                    ci_run_id,
                    "--output",
                    str(integration_job),
                    "--private-key",
                    str(authorizer_private),
                    "--actor",
                    "local-owner",
                    "--integration-source",
                    str(source_two_package),
                    "--integration-source",
                    str(package_path),
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if team_ready
            else {
                "id": "smoke-v14-integration-prepare",
                "ok": False,
                "exit_code": None,
                "skipped": "reviewer identity unavailable",
            }
        )
        checks.append(integration_prepare)
        integration_execute = (
            _run(
                "smoke-v14-integration-execute",
                prefix
                + [
                    "ci",
                    "execute",
                    "--job",
                    str(integration_job),
                    "--checkout",
                    str(code),
                    "--output",
                    str(integration_unsigned),
                    "--job-trust-policy",
                    str(job_trust_path),
                    "--job-policy",
                    "job",
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if integration_prepare.get("ok") is True
            else {
                "id": "smoke-v14-integration-execute",
                "ok": False,
                "exit_code": None,
                "skipped": "integration job unavailable",
            }
        )
        checks.append(integration_execute)
        integration_attest = (
            _run(
                "smoke-v14-integration-attest",
                prefix
                + [
                    "ci",
                    "attest",
                    "--job",
                    str(integration_job),
                    "--result",
                    str(integration_unsigned),
                    "--output",
                    str(integration_package),
                    "--private-key",
                    str(private_key),
                    "--actor",
                    "github-actions",
                    "--job-trust-policy",
                    str(job_trust_path),
                    "--job-policy",
                    "job",
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if integration_execute.get("ok") is True
            else {
                "id": "smoke-v14-integration-attest",
                "ok": False,
                "exit_code": None,
                "skipped": "integration result unavailable",
            }
        )
        checks.append(integration_attest)
        review_package = smoke / "aria-review.aria-evidence"
        target_commit = ""
        target_check: dict[str, object]
        if integration_attest.get("ok") is True:
            target_check = _run(
                "smoke-v14-gate-target",
                ["git", "rev-parse", "HEAD"],
                cwd=code,
                logs=logs,
                env=env,
            )
            if target_check.get("ok") is True:
                target_commit = str(target_check.get("excerpt", "")).strip()
        else:
            target_check = {
                "id": "smoke-v14-gate-target",
                "ok": False,
                "exit_code": None,
                "skipped": "integration result unavailable",
            }
        checks.append(target_check)
        review_attest = (
            _run(
                "smoke-v14-review-attest",
                prefix
                + [
                    "evidence",
                    "review-attest",
                    "--project",
                    "release-smoke",
                    "--target-commit",
                    target_commit,
                    "--source-package",
                    str(source_two_package),
                    "--source-package",
                    str(package_path),
                    "--integration-package",
                    str(integration_package),
                    "--output",
                    str(review_package),
                    "--private-key",
                    str(reviewer_private),
                    "--reviewer",
                    "release-reviewer",
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if target_commit
            else {
                "id": "smoke-v14-review-attest",
                "ok": False,
                "exit_code": None,
                "skipped": "integration target unavailable",
            }
        )
        checks.append(review_attest)
        gate_trust_ready = False
        if review_attest.get("ok") is True:
            try:
                with zipfile.ZipFile(source_two_package) as archive:
                    source_signature = json.loads(archive.read("signature.json"))
                with zipfile.ZipFile(integration_package) as archive:
                    integration_signature = json.loads(
                        archive.read("signature.json")
                    )
                with zipfile.ZipFile(review_package) as archive:
                    review_signature = json.loads(archive.read("signature.json"))
                integration_job_payload = json.loads(
                    integration_job.read_text(encoding="utf-8")
                )
                authorization = integration_job_payload["authorization"]
                keys_by_id = {
                    row["id"]: row
                    for row in [
                        {
                            "id": authorization["key_id"],
                            "public_key": authorization["public_key"],
                            "actor_id": "local-owner",
                            "status": "trusted",
                        },
                        {
                            "id": source_signature["key_id"],
                            "public_key": source_signature["public_key"],
                            "actor_id": "github-actions",
                            "status": "trusted",
                        },
                        {
                            "id": integration_signature["key_id"],
                            "public_key": integration_signature["public_key"],
                            "actor_id": "github-actions",
                            "status": "trusted",
                        },
                        {
                            "id": review_signature["key_id"],
                            "public_key": review_signature["public_key"],
                            "actor_id": "release-reviewer",
                            "status": "trusted",
                        },
                    ]
                }
                _merge_trust_policy(
                    docs / "TRUST.yaml",
                    keys=list(keys_by_id.values()),
                    policies={
                        "job": {
                            "minimum_trust_level": "signed",
                            "trusted_keys": [authorization["key_id"]],
                        },
                        "contributor": {
                            "minimum_trust_level": "signed",
                            "trusted_keys": [source_signature["key_id"]],
                            "allowed_actor_roles": ["ci"],
                        },
                        "release": {
                            "minimum_trust_level": "ci-signed",
                            "trusted_keys": [integration_signature["key_id"]],
                            "allowed_actor_roles": ["ci"],
                            "required_approvals": 1,
                        },
                        "reviewer": {
                            "minimum_trust_level": "signed",
                            "trusted_keys": [review_signature["key_id"]],
                            "allowed_actor_roles": ["reviewer"],
                        },
                    },
                )
                gate_trust_ready = True
            except (
                KeyError,
                OSError,
                TypeError,
                zipfile.BadZipFile,
                json.JSONDecodeError,
            ):
                gate_trust_ready = False
        checks.append(
            _run(
                "smoke-v14-integration-gate",
                prefix
                + [
                    "gate",
                    "integration",
                    "--project",
                    "release-smoke",
                    "--source-package",
                    str(source_two_package),
                    "--source-package",
                    str(package_path),
                    "--integration-package",
                    str(integration_package),
                    "--target-commit",
                    target_commit,
                    "--trust-policy",
                    str(docs / "TRUST.yaml"),
                    "--source-policy",
                    "contributor",
                    "--integration-policy",
                    "release",
                    "--review-package",
                    str(review_package),
                    "--review-policy",
                    "reviewer",
                    "--output",
                    str(smoke / "integration-verdict.json"),
                ],
                cwd=output,
                logs=logs,
                env=env,
            )
            if gate_trust_ready
            else {
                "id": "smoke-v14-integration-gate",
                "ok": False,
                "exit_code": None,
                "skipped": "signed integration or review evidence unavailable",
            }
        )
    else:
        for name in (
            "smoke-v14-ci-prepare",
            "smoke-v14-ci-execute",
            "smoke-v14-ci-attest",
            "smoke-v14-evidence-verify",
            "smoke-v14-ci-import",
            "smoke-v14-source-two-prepare",
            "smoke-v14-source-two-execute",
            "smoke-v14-source-two-attest",
            "smoke-v14-reviewer-key-generate",
            "smoke-v14-integration-prepare",
            "smoke-v14-integration-execute",
            "smoke-v14-integration-attest",
            "smoke-v14-gate-target",
            "smoke-v14-review-attest",
            "smoke-v14-integration-gate",
        ):
            checks.append(
                {
                    "id": name,
                    "ok": False,
                    "exit_code": None,
                    "skipped": "CI smoke run unavailable",
                }
            )
    return checks
