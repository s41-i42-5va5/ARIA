from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

from aria.assurance import TEST_CLASSES
from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes, atomic_write_json, exclusive_lock
from aria.project import ProjectConfig, canonical_sha, git_snapshot, safe_relative_path

COMMAND_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
SECRET_NAME_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|ACCESS_KEY|KEY_ID|CREDENTIAL|AUTH|DSN|URL)",
    re.IGNORECASE,
)
MAX_ARGUMENTS = 64
MAX_ARGUMENT_LENGTH = 4096
MAX_TIMEOUT_SECONDS = 3600
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
ADAPTER_EXECUTABLES = {
    "python": {"python", "python.exe", "python3", "python3.exe", "py", "py.exe", "pytest", "pytest.exe"},
    "node": {"node", "node.exe", "npm", "npm.cmd", "pnpm", "pnpm.cmd", "yarn", "yarn.cmd"},
    "rust": {"cargo", "cargo.exe"},
    "go": {"go", "go.exe"},
}
SAFE_ENVIRONMENT_NAMES = {
    "CI",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}


def _stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_mapping(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"{label} is not readable UTF-8 JSON: {path}") from error
    if not isinstance(payload, dict):
        raise WorkflowError(f"{label} must contain a JSON object")
    return payload


def _runtime_path(run_root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str):
        raise WorkflowError(f"{label} path must be a string")
    normalized = safe_relative_path(relative)
    path = run_root.joinpath(*PurePosixPath(normalized).parts)
    resolved = path.resolve(strict=False)
    if path.is_symlink() or not resolved.is_relative_to(run_root.resolve(strict=True)):
        raise WorkflowError(f"{label} escapes the run directory")
    if not path.is_file():
        raise WorkflowError(f"{label} is missing: {normalized}")
    return path


def _normalized_command(raw: object, index: int) -> dict[str, object]:
    label = f"Verification command {index}"
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must be a mapping")
    command_id = raw.get("id")
    if not isinstance(command_id, str) or COMMAND_ID_RE.fullmatch(command_id) is None:
        raise ConfigurationError(f"{label} has invalid id")
    adapter = raw.get("adapter")
    if not isinstance(adapter, str) or adapter not in ADAPTER_EXECUTABLES:
        raise ConfigurationError(
            f"{label} adapter must be one of {sorted(ADAPTER_EXECUTABLES)}"
        )
    argv = raw.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > MAX_ARGUMENTS
        or not all(isinstance(value, str) and value for value in argv)
    ):
        raise ConfigurationError(f"{label} argv must be a non-empty string list")
    if any(
        len(value) > MAX_ARGUMENT_LENGTH or "\x00" in value or "\r" in value or "\n" in value
        for value in argv
    ):
        raise ConfigurationError(f"{label} argv contains an unsafe argument")
    executable = Path(argv[0]).name.lower()
    if executable != argv[0].lower() or executable not in ADAPTER_EXECUTABLES[adapter]:
        raise ConfigurationError(
            f"{label} executable {argv[0]!r} is not allowed for adapter {adapter}"
        )
    cwd = safe_relative_path(str(raw.get("cwd", ".")))
    classes = raw.get("classes")
    if (
        not isinstance(classes, list)
        or not classes
        or not all(isinstance(value, str) and value in TEST_CLASSES for value in classes)
        or len(classes) != len(set(classes))
    ):
        raise ConfigurationError(f"{label} classes are invalid")
    timeout = raw.get("timeout_seconds", 900)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout < 1
        or timeout > MAX_TIMEOUT_SECONDS
    ):
        raise ConfigurationError(
            f"{label} timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}"
        )
    return {
        "id": command_id,
        "adapter": adapter,
        "argv": argv,
        "cwd": cwd,
        "classes": classes,
        "timeout_seconds": timeout,
    }


def load_execution_config(project: ProjectConfig) -> dict[str, object] | None:
    relative = project.files.verification
    if relative is None:
        return None
    path = project.document_path(relative)
    if not path.is_file():
        raise ConfigurationError(f"Configured verification contract is missing: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Verification contract is unreadable: {path}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ConfigurationError("VERIFY.yaml schema_version must be 1")
    raw_commands = payload.get("commands")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ConfigurationError("VERIFY.yaml commands must be a non-empty list")
    commands = [_normalized_command(raw, index) for index, raw in enumerate(raw_commands)]
    ids = [str(row["id"]) for row in commands]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("VERIFY.yaml command ids must be unique")
    return {
        "schema_version": 1,
        "source_path": relative,
        "source_sha256": _sha256(path.read_bytes()),
        "commands": commands,
    }


def prepare_execution_contract(
    project: ProjectConfig, run_root: Path
) -> dict[str, object] | None:
    config = load_execution_config(project)
    if config is None:
        return None
    relative = "execution/contract.json"
    path = run_root / relative
    atomic_write_json(path, config)
    content = path.read_bytes()
    return {
        "schema_version": 1,
        "path": relative,
        "sha256": _sha256(content),
        "command_ids": [
            str(row["id"])
            for row in config["commands"]
            if isinstance(row, dict)
        ],
    }


def _load_contract(
    run_root: Path, manifest: dict[str, object]
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    descriptor = manifest.get("execution_contract")
    if not isinstance(descriptor, dict):
        raise WorkflowError(
            "This run has no immutable execution contract; start a new run after configuring VERIFY.yaml"
        )
    path = _runtime_path(run_root, descriptor.get("path"), "Execution contract")
    content = path.read_bytes()
    if descriptor.get("sha256") != _sha256(content):
        raise WorkflowError("Execution contract SHA mismatch")
    contract = _json_mapping(path, "Execution contract")
    commands = contract.get("commands")
    if not isinstance(commands, list):
        raise WorkflowError("Execution contract commands are malformed")
    indexed = {
        str(row.get("id")): row
        for row in commands
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    if len(indexed) != len(commands):
        raise WorkflowError("Execution contract command identity is malformed")
    return contract, indexed


def _feature_links(
    run_root: Path,
    manifest: dict[str, object],
    links_path: Path | None,
    commands: dict[str, dict[str, object]],
) -> tuple[dict[str, dict[str, list[str]]], str | None]:
    policy = manifest.get("feature_contract")
    locked_feature_path: Path | None = None
    if isinstance(policy, dict) and policy.get("required") is True:
        if manifest.get("contract_phase") != "feature_contract_locked":
            raise WorkflowError("Verification requires a locked Feature Contract")
        locked_feature_path = _runtime_path(
            run_root, policy.get("artifact"), "Feature Contract"
        )
        lock = manifest.get("feature_contract_lock")
        if (
            not isinstance(lock, dict)
            or lock.get("sha256") != _sha256(locked_feature_path.read_bytes())
        ):
            raise WorkflowError("Feature Contract artifact no longer matches its lock")
    if links_path is None:
        return {command_id: {"requirement_ids": [], "acceptance_ids": []} for command_id in commands}, None
    try:
        payload = json.loads(links_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Verification links are not readable UTF-8 JSON: {links_path}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise WorkflowError("Verification links schema_version must be 1")
    raw_links = payload.get("commands")
    if not isinstance(raw_links, dict):
        raise WorkflowError("Verification links commands must be a mapping")
    unknown_commands = sorted(set(map(str, raw_links)) - set(commands))
    if unknown_commands:
        raise WorkflowError(f"Verification links contain unknown commands: {unknown_commands}")
    known_requirements: set[str] = set()
    acceptance_requirements: dict[str, set[str]] = {}
    if isinstance(policy, dict) and policy.get("required") is True:
        assert locked_feature_path is not None
        feature = _json_mapping(locked_feature_path, "Feature Contract")
        known_requirements = {
            str(row.get("id"))
            for row in feature.get("requirements", [])
            if isinstance(row, dict)
        }
        acceptance_requirements = {
            str(row.get("id")): {
                str(value) for value in row.get("requirement_ids", []) if isinstance(value, str)
            }
            for row in feature.get("acceptance", [])
            if isinstance(row, dict)
        }
    elif raw_links:
        raise WorkflowError("Requirement links are allowed only for a locked Feature Contract")
    normalized: dict[str, dict[str, list[str]]] = {}
    for command_id in commands:
        raw = raw_links.get(command_id, {})
        if not isinstance(raw, dict):
            raise WorkflowError(f"Verification links for {command_id} must be a mapping")
        requirements = raw.get("requirement_ids", [])
        acceptance = raw.get("acceptance_ids", [])
        if (
            not isinstance(requirements, list)
            or not all(isinstance(value, str) and value for value in requirements)
            or len(requirements) != len(set(requirements))
            or not isinstance(acceptance, list)
            or not all(isinstance(value, str) and value for value in acceptance)
            or len(acceptance) != len(set(acceptance))
        ):
            raise WorkflowError(f"Verification links for {command_id} are invalid")
        unknown_requirements = sorted(set(requirements) - known_requirements)
        unknown_acceptance = sorted(set(acceptance) - set(acceptance_requirements))
        if unknown_requirements or unknown_acceptance:
            raise WorkflowError(
                f"Verification links for {command_id} reference unknown ids; "
                f"requirements={unknown_requirements}, acceptance={unknown_acceptance}"
            )
        required_by_acceptance = set().union(
            *(acceptance_requirements[value] for value in acceptance)
        ) if acceptance else set()
        if not required_by_acceptance.issubset(set(requirements)):
            raise WorkflowError(
                f"Verification links for {command_id} omit requirements linked to acceptance ids"
            )
        normalized[command_id] = {
            "requirement_ids": list(requirements),
            "acceptance_ids": list(acceptance),
        }
    return normalized, canonical_sha(payload)


def _git_evidence(project: ProjectConfig) -> dict[str, object]:
    snapshot = git_snapshot(project.code_root, project.git_ignore_prefixes)
    changes = snapshot.get("changes")
    paths = changes.get("paths") if isinstance(changes, dict) else None
    if not isinstance(paths, dict):
        raise WorkflowError("Git working tree evidence is malformed")
    return {
        "head": snapshot.get("head"),
        "working_tree_sha256": canonical_sha(paths),
        "changed_count": len(paths),
    }


def _execution_environment() -> tuple[dict[str, str], list[tuple[str, bytes]]]:
    inherited = dict(os.environ)
    env = {
        name: value
        for name, value in inherited.items()
        if name.upper() in SAFE_ENVIRONMENT_NAMES and not SECRET_NAME_RE.search(name)
    }
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    secrets: list[tuple[str, bytes]] = []
    for name, value in inherited.items():
        if SECRET_NAME_RE.search(name) and len(value) >= 4:
            secrets.append((name, value.encode("utf-8", errors="ignore")))
    return env, secrets


def _resolve_executable(
    project: ProjectConfig, command: dict[str, object], env: dict[str, str]
) -> Path:
    argv = command["argv"]
    if not isinstance(argv, list) or not argv:
        raise WorkflowError("Execution command argv is malformed")
    name = str(argv[0])
    if str(command.get("adapter")) == "python" and name.lower() in {"python", "python.exe", "python3", "python3.exe"}:
        local_candidates = [
            project.code_root / ".venv" / "Scripts" / "python.exe",
            project.code_root / ".venv" / "bin" / "python",
        ]
        for candidate in local_candidates:
            if candidate.is_file():
                return candidate.resolve(strict=True)
        current = Path(sys.executable)
        if current.is_file():
            return current.resolve(strict=True)
    resolved = shutil.which(name, path=env.get("PATH"))
    if resolved is None:
        raise WorkflowError(f"Verification executable is unavailable: {name}")
    return Path(resolved).resolve(strict=True)


def _display_command(executable: Path, arguments: list[str]) -> str:
    return subprocess.list2cmdline([str(executable), *arguments])


def _windows_job(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(information), ctypes.sizeof(information)
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _close_windows_job(job: int | None, *, terminate: bool) -> None:
    if job is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if terminate:
        kernel32.TerminateJobObject(wintypes.HANDLE(job), 1)
    kernel32.CloseHandle(wintypes.HANDLE(job))


def _resume_windows_process(process_id: int) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenThread.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == invalid:
        raise OSError("Cannot enumerate suspended verification process threads")
    entry = ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
    resumed = False
    try:
        while found:
            if entry.th32OwnerProcessID == process_id:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if thread:
                    kernel32.ResumeThread(thread)
                    kernel32.CloseHandle(thread)
                    resumed = True
                    break
            found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise OSError("Cannot resume suspended verification process")


def _terminate_process_tree(
    process: subprocess.Popen[bytes], windows_job: int | None = None
) -> None:
    if process.poll() is not None:
        if os.name == "nt":
            _close_windows_job(windows_job, terminate=True)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=30,
        )
        _close_windows_job(windows_job, terminate=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def _run_command(
    project: ProjectConfig,
    run_root: Path,
    command: dict[str, object],
    *,
    git: dict[str, object],
    links: dict[str, list[str]],
    contract_sha256: str,
    links_sha256: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    command_id = str(command["id"])
    execution_id = f"{command_id}-{uuid.uuid4().hex[:12]}"
    relative_output = f"execution/logs/{execution_id}.log"
    output = run_root / relative_output
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(".partial")
    cwd_relative = safe_relative_path(str(command["cwd"]))
    cwd = project.code_root.joinpath(*PurePosixPath(cwd_relative).parts).resolve(strict=True)
    if not cwd.is_dir() or not cwd.is_relative_to(project.code_root.resolve(strict=True)):
        raise WorkflowError(f"Verification cwd is outside the project: {cwd_relative}")
    env, secrets = _execution_environment()
    executable = _resolve_executable(project, command, env)
    executable_sha256 = _sha256(executable.read_bytes())
    git_before = _git_evidence(project)
    if git_before != git:
        raise WorkflowError(
            f"Product Git state changed before verification command: {command_id}"
        )
    argv = [str(value) for value in command["argv"]]
    if executable.suffix.lower() in {".cmd", ".bat"} and any(
        re.search(r"[&|<>^%!()]", value) for value in argv[1:]
    ):
        raise WorkflowError(f"Verification command {command_id} has unsafe batch arguments")
    started_at = _stamp()
    started = time.monotonic()
    status = "failed"
    exit_code: int | None = None
    overflow = threading.Event()
    launch_error: str | None = None
    process: subprocess.Popen[bytes] | None = None
    windows_job: int | None = None

    def drain() -> None:
        assert process is not None and process.stdout is not None
        written = 0
        with partial.open("wb") as stream:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                remaining = MAX_OUTPUT_BYTES - written
                if remaining > 0:
                    stream.write(chunk[:remaining])
                    written += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break

    try:
        process = subprocess.Popen(
            [str(executable), *argv[1:]],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            creationflags=(
                (subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004)
                if os.name == "nt"
                else 0
            ),
            start_new_session=os.name != "nt",
        )
        windows_job = _windows_job(process)
        if os.name == "nt" and windows_job is None:
            process.kill()
            process.wait(timeout=30)
            raise OSError(
                "Verification process could not be assigned to a Windows Job Object"
            )
        _resume_windows_process(process.pid)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            exit_code = process.wait(timeout=int(command["timeout_seconds"]))
            status = "passed" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timed_out"
            _terminate_process_tree(process, windows_job)
            windows_job = None
            exit_code = process.wait(timeout=30)
        else:
            _terminate_process_tree(process, windows_job)
            windows_job = None
        reader.join(timeout=30)
        if reader.is_alive():
            status = "output_reader_failed"
            _terminate_process_tree(process, windows_job)
            windows_job = None
        if process.stdout is not None:
            process.stdout.close()
        if overflow.is_set():
            status = "output_limit_exceeded"
        _terminate_process_tree(process, windows_job)
        windows_job = None
    except OSError as error:
        if process is not None and process.poll() is None:
            _terminate_process_tree(process, windows_job)
            windows_job = None
        else:
            _close_windows_job(windows_job, terminate=True)
        launch_error = str(error)
        status = "launch_error"
        atomic_write_bytes(partial, launch_error.encode("utf-8", errors="replace"))
    duration = round(time.monotonic() - started, 6)
    try:
        content = partial.read_bytes()
    except OSError:
        content = b""
    redacted_names: list[str] = []
    for name, secret in secrets:
        if secret and secret in content:
            content = content.replace(secret, f"[REDACTED:{name}]".encode())
            redacted_names.append(name)
    atomic_write_bytes(output, content)
    try:
        partial.unlink()
    except FileNotFoundError:
        pass
    excerpt = content.decode("utf-8", errors="replace")[-2000:].strip()
    safe_argv = [str(executable), *argv[1:]]
    safe_launch_error = launch_error
    for name, secret in secrets:
        if not secret:
            continue
        secret_text = secret.decode("utf-8", errors="ignore")
        replacement = f"[REDACTED:{name}]"
        argv_before = list(safe_argv)
        safe_argv = [value.replace(secret_text, replacement) for value in safe_argv]
        if safe_argv != argv_before and name not in redacted_names:
            redacted_names.append(name)
        if safe_launch_error is not None:
            safe_launch_error = safe_launch_error.replace(secret_text, replacement)
    git_after = _git_evidence(project)
    if git_after != git and status == "passed":
        status = "git_state_changed"
    executable_sha256_after = _sha256(executable.read_bytes())
    if executable_sha256_after != executable_sha256 and status == "passed":
        status = "executable_changed"
    receipt = {
        "schema_version": 1,
        "execution_id": execution_id,
        "run_id": run_root.name,
        "command_id": command_id,
        "adapter": command["adapter"],
        "command_contract_sha256": canonical_sha(command),
        "execution_contract_sha256": contract_sha256,
        "links_sha256": links_sha256,
        "argv": safe_argv,
        "command": subprocess.list2cmdline(safe_argv),
        "resolved_executable": str(executable),
        "resolved_executable_sha256": executable_sha256,
        "resolved_executable_sha256_after": executable_sha256_after,
        "cwd": cwd_relative,
        "classes": command["classes"],
        "requirement_ids": links["requirement_ids"],
        "acceptance_ids": links["acceptance_ids"],
        "git": git,
        "git_after": git_after,
        "started_at": started_at,
        "finished_at": _stamp(),
        "duration_seconds": duration,
        "timeout_seconds": command["timeout_seconds"],
        "status": status,
        "exit_code": exit_code,
        "output_path": relative_output,
        "output_sha256": _sha256(content),
        "output_size": len(content),
        "output_excerpt": excerpt,
        "redacted_environment_names": sorted(redacted_names),
        "launch_error": safe_launch_error,
    }
    receipt_relative = f"execution/receipts/{execution_id}.json"
    receipt_path = run_root / receipt_relative
    atomic_write_json(receipt_path, receipt)
    descriptor = {
        "execution_id": execution_id,
        "command_id": command_id,
        "status": status,
        "receipt_path": receipt_relative,
        "receipt_sha256": _sha256(receipt_path.read_bytes()),
    }
    return receipt, descriptor


def _read_receipt(
    run_root: Path, descriptor: dict[str, object]
) -> dict[str, object]:
    path = _runtime_path(run_root, descriptor.get("receipt_path"), "Execution receipt")
    content = path.read_bytes()
    if descriptor.get("receipt_sha256") != _sha256(content):
        raise WorkflowError("Execution receipt SHA mismatch")
    receipt = _json_mapping(path, "Execution receipt")
    if receipt.get("execution_id") != descriptor.get("execution_id"):
        raise WorkflowError("Execution receipt identity mismatch")
    output = _runtime_path(run_root, receipt.get("output_path"), "Execution output")
    if receipt.get("output_sha256") != _sha256(output.read_bytes()):
        raise WorkflowError("Execution output SHA mismatch")
    return receipt


def _resume_receipt(
    run_root: Path,
    command: dict[str, object],
    *,
    git: dict[str, object],
    links: dict[str, list[str]],
    contract_sha256: str,
    links_sha256: str | None,
) -> tuple[dict[str, object], dict[str, object]] | None:
    index_path = run_root / "execution" / "index.json"
    if not index_path.is_file():
        return None
    index = _json_mapping(index_path, "Execution index")
    entries = index.get("executions")
    if not isinstance(entries, list):
        raise WorkflowError("Execution index is malformed")
    for raw in reversed(entries):
        if not isinstance(raw, dict) or raw.get("command_id") != command.get("id"):
            continue
        receipt = _read_receipt(run_root, raw)
        if (
            receipt.get("status") == "passed"
            and receipt.get("git") == git
            and receipt.get("execution_contract_sha256") == contract_sha256
            and receipt.get("command_contract_sha256") == canonical_sha(command)
            and receipt.get("links_sha256") == links_sha256
            and receipt.get("requirement_ids") == links["requirement_ids"]
            and receipt.get("acceptance_ids") == links["acceptance_ids"]
        ):
            return receipt, raw
    return None


def _verification_template(receipts: list[dict[str, object]]) -> dict[str, object]:
    tests: list[dict[str, object]] = []
    for receipt in receipts:
        if receipt.get("status") != "passed":
            continue
        classes = [str(value) for value in receipt.get("classes", [])]
        class_evidence: dict[str, object] = {}
        for test_class in classes:
            row: dict[str, object] = {
                "proof_excerpts": [f"<exact {test_class} proof excerpt from output>"],
            }
            if test_class in {"e2e", "adversarial", "concurrency", "load", "stress", "soak", "recovery", "chaos"}:
                row["proof_excerpts"].append(
                    f"<second distinct {test_class} proof excerpt from output>"
                )  # type: ignore[union-attr]
            if test_class in {"concurrency", "load", "stress", "soak"}:
                row["metrics_excerpt"] = "<exact JSON metrics excerpt from output>"
            class_evidence[test_class] = row
        tests.append(
            {
                "execution_id": receipt["execution_id"],
                "classes": classes,
                "status": "passed",
                "command": receipt["command"],
                "exit_code": receipt["exit_code"],
                "output_path": receipt["output_path"],
                "output_sha256": receipt["output_sha256"],
                "output_excerpt": receipt["output_excerpt"],
                "actual_result": "<describe the observed product result>",
                "scenario": None,
                "class_evidence": class_evidence,
            }
        )
    return {
        "schema_version": 1,
        "instructions": (
            "Complete semantic actual_result, scenario and exact proof excerpts. "
            "Do not change execution_id, command, exit_code, output_path or output_sha256."
        ),
        "tests": tests,
    }


def verify_project_run(
    project: ProjectConfig,
    *,
    run_id: str,
    command_ids: list[str] | None = None,
    links_path: Path | None = None,
    resume: bool = True,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    from aria.simple_run import _run_root, _validate_run_identity, read_project_run

    run_root = _run_root(project, run_id)
    lock_path = project.runtime_root / "locks" / f"{run_id}.lock"
    with exclusive_lock(lock_path, timeout_seconds=120.0):
        manifest = read_project_run(project, run_id)
        if manifest.get("status") != "started":
            raise WorkflowError(f"Run is not open for verification: {run_id}")
        _validate_run_identity(project, manifest)
        route = manifest.get("route")
        if not isinstance(route, dict) or route.get("intent") not in {"build", "review"}:
            raise WorkflowError("aria verify supports build and review runs only")
        feature_policy = manifest.get("feature_contract")
        if (
            isinstance(feature_policy, dict)
            and feature_policy.get("required") is True
            and manifest.get("contract_phase") != "feature_contract_locked"
        ):
            raise WorkflowError("Feature Contract must be locked before aria verify")
        if manifest.get("managed_lifecycle") is True:
            lifecycle = _json_mapping(run_root / "lifecycle.json", "Lifecycle state")
            if lifecycle.get("phase") != "converge":
                raise WorkflowError("Managed lifecycle must be in converge before aria verify")
        _contract, commands = _load_contract(run_root, manifest)
        selected = list(dict.fromkeys(command_ids or commands.keys()))
        unknown = sorted(set(selected) - set(commands))
        if unknown:
            raise WorkflowError(f"Unknown verification command ids: {unknown}")
        if not selected:
            raise WorkflowError("No verification commands selected")
        links, links_sha256 = _feature_links(
            run_root, manifest, links_path, commands
        )
        git = _git_evidence(project)
        contract_descriptor = manifest["execution_contract"]
        assert isinstance(contract_descriptor, dict)
        contract_sha256 = str(contract_descriptor["sha256"])
        index_path = run_root / "execution" / "index.json"
        if index_path.is_file():
            index = _json_mapping(index_path, "Execution index")
            raw_entries = index.get("executions", [])
            if not isinstance(raw_entries, list):
                raise WorkflowError("Execution index is malformed")
            index_entries = list(raw_entries)
        else:
            index_entries = []
        receipts: list[dict[str, object]] = []
        descriptors: list[dict[str, object]] = []
        reused: list[str] = []
        for command_id in selected:
            command = commands[command_id]
            prior = (
                _resume_receipt(
                    run_root,
                    command,
                    git=git,
                    links=links[command_id],
                    contract_sha256=contract_sha256,
                    links_sha256=links_sha256,
                )
                if resume
                else None
            )
            if prior is not None:
                receipt, descriptor = prior
                reused.append(str(receipt["execution_id"]))
            else:
                receipt, descriptor = _run_command(
                    project,
                    run_root,
                    command,
                    git=git,
                    links=links[command_id],
                    contract_sha256=contract_sha256,
                    links_sha256=links_sha256,
                )
                index_entries.append(descriptor)
                atomic_write_json(
                    index_path,
                    {
                        "schema_version": 1,
                        "run_id": run_id,
                        "executions": index_entries,
                    },
                )
            receipts.append(receipt)
            descriptors.append(descriptor)
        required = manifest.get("assurance_plan", {}).get("required_execution_classes", [])
        required_classes = {
            str(value) for value in required if isinstance(value, str)
        } if isinstance(required, list) else set()
        passed_classes = {
            str(value)
            for receipt in receipts
            if receipt.get("status") == "passed"
            for value in receipt.get("classes", [])
            if isinstance(value, str)
        }
        missing_classes = sorted(required_classes - passed_classes)
        failed_commands = [
            str(receipt["command_id"])
            for receipt in receipts
            if receipt.get("status") != "passed"
        ]
        bundle = {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": _stamp(),
            "execution_contract_sha256": contract_sha256,
            "links_sha256": links_sha256,
            "git": git,
            "executions": descriptors,
            "selected_command_ids": selected,
            "required_execution_classes": sorted(required_classes),
            "passed_execution_classes": sorted(passed_classes),
            "missing_execution_classes": missing_classes,
            "failed_command_ids": failed_commands,
            "ok": not missing_classes and not failed_commands,
        }
        bundle_relative = "execution/bundle.json"
        bundle_path = run_root / bundle_relative
        atomic_write_json(bundle_path, bundle)
        template_relative = "execution/verification-template.json"
        template_path = run_root / template_relative
        atomic_write_json(template_path, _verification_template(receipts))
        manifest["execution_bundle"] = {
            "schema_version": 1,
            "path": bundle_relative,
            "sha256": _sha256(bundle_path.read_bytes()),
            "ok": bundle["ok"],
            "created_at": bundle["created_at"],
        }
        atomic_write_json(run_root / "manifest.json", manifest)
        result = {
            "ok": bundle["ok"],
            "project": project.project_id,
            "run_id": run_id,
            "bundle_path": str(bundle_path),
            "bundle_sha256": manifest["execution_bundle"]["sha256"],
            "verification_template_path": str(template_path),
            "executions": [
                {
                    "execution_id": receipt["execution_id"],
                    "command_id": receipt["command_id"],
                    "status": receipt["status"],
                    "exit_code": receipt["exit_code"],
                    "output_path": receipt["output_path"],
                    "requirement_ids": receipt["requirement_ids"],
                    "acceptance_ids": receipt["acceptance_ids"],
                }
                for receipt in receipts
            ],
            "reused_execution_ids": reused,
            "missing_execution_classes": missing_classes,
            "failed_command_ids": failed_commands,
            "next_action": (
                "Complete the semantic verification template, independent review and convergence evidence."
                if bundle["ok"]
                else "Fix failed commands or add commands for missing assurance classes, then rerun aria verify."
            ),
        }
        if project.framework_version in {"1.5.0", "1.5.1", "1.5.2", "1.5.3", "1.5.4", "1.5.5"}:
            from aria.access import load_access_policy
            from aria.backlog import load_backlog, sync_backlog

            if load_access_policy(project).get("status") == "active":
                snapshot = git_snapshot(
                    project.code_root, project.git_ignore_prefixes
                )
                sync_backlog(
                    project,
                    expected_revision=int(load_backlog(project)["revision"]),
                    actor_id=actor_id,
                    device_id=device_id,
                    version=project.framework_version,
                    branch=snapshot.get("branch"),
                )
        return result


def validate_execution_bundle(
    project: ProjectConfig,
    run_root: Path,
    manifest: dict[str, object],
) -> dict[str, dict[str, object]] | None:
    contract_descriptor = manifest.get("execution_contract")
    if not isinstance(contract_descriptor, dict):
        return None
    bundle_descriptor = manifest.get("execution_bundle")
    if not isinstance(bundle_descriptor, dict):
        raise WorkflowError("Run requires an aria verify Evidence Bundle before closure")
    bundle_path = _runtime_path(run_root, bundle_descriptor.get("path"), "Evidence Bundle")
    content = bundle_path.read_bytes()
    if bundle_descriptor.get("sha256") != _sha256(content):
        raise WorkflowError("Evidence Bundle SHA mismatch")
    bundle = _json_mapping(bundle_path, "Evidence Bundle")
    if (
        bundle.get("schema_version") != 1
        or bundle.get("run_id") != manifest.get("run_id")
        or bundle.get("execution_contract_sha256") != contract_descriptor.get("sha256")
        or bundle.get("ok") is not True
    ):
        raise WorkflowError("Evidence Bundle identity or status is invalid")
    if bundle.get("git") != _git_evidence(project):
        raise WorkflowError("Product Git state changed after aria verify")
    _contract, commands = _load_contract(run_root, manifest)
    executions = bundle.get("executions")
    if not isinstance(executions, list) or not executions:
        raise WorkflowError("Evidence Bundle has no executions")
    receipts: dict[str, dict[str, object]] = {}
    covered_classes: set[str] = set()
    for raw in executions:
        if not isinstance(raw, dict):
            raise WorkflowError("Evidence Bundle execution descriptor is malformed")
        receipt = _read_receipt(run_root, raw)
        execution_id = receipt.get("execution_id")
        if not isinstance(execution_id, str) or execution_id in receipts:
            raise WorkflowError("Evidence Bundle execution ids are invalid")
        if receipt.get("status") != "passed" or receipt.get("exit_code") != 0:
            raise WorkflowError(f"Evidence Bundle execution did not pass: {execution_id}")
        command_id = receipt.get("command_id")
        command = commands.get(str(command_id))
        if (
            command is None
            or receipt.get("command_contract_sha256") != canonical_sha(command)
            or receipt.get("execution_contract_sha256")
            != contract_descriptor.get("sha256")
            or receipt.get("git") != bundle.get("git")
            or receipt.get("git_after") != bundle.get("git")
            or receipt.get("resolved_executable_sha256_after")
            != receipt.get("resolved_executable_sha256")
            or raw.get("status") != "passed"
            or raw.get("command_id") != command_id
            or receipt.get("classes") != command.get("classes")
        ):
            raise WorkflowError(
                f"Evidence Bundle receipt contract mismatch: {execution_id}"
            )
        executable = Path(str(receipt.get("resolved_executable")))
        try:
            executable_sha256 = _sha256(executable.read_bytes())
        except OSError as error:
            raise WorkflowError(
                f"Evidence Bundle executable is unreadable: {execution_id}"
            ) from error
        if receipt.get("resolved_executable_sha256") != executable_sha256:
            raise WorkflowError(
                f"Evidence Bundle executable SHA mismatch: {execution_id}"
            )
        covered_classes.update(
            str(value)
            for value in receipt.get("classes", [])
            if isinstance(value, str)
        )
        receipts[execution_id] = receipt
    assurance = manifest.get("assurance_plan")
    required = (
        assurance.get("required_execution_classes", [])
        if isinstance(assurance, dict)
        else []
    )
    missing = sorted(
        {str(value) for value in required if isinstance(value, str)}
        - covered_classes
    )
    if missing:
        raise WorkflowError(
            f"Evidence Bundle misses required execution classes: {missing}"
        )
    return receipts
