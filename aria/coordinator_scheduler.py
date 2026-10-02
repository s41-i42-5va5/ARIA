from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

from aria.collaboration import load_control_contract
from aria.errors import AriaError, ConfigurationError, WorkflowError
from aria.github_auth import CLIENT_ID_RE
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE, ProjectConfig, default_runtime_root, load_project


CONFIG_SCHEMA_VERSION = 1
TASK_PREFIX = "ARIA-Codex-Coordinator-"
SID_RE = re.compile(r"S-1-(?:\d+-){1,14}\d+")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ConfigurationError(f"Cannot hash coordinator executable: {path}") from error
    return digest.hexdigest()


def _task_name(project_id: str) -> str:
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError("Coordinator project id is invalid")
    return TASK_PREFIX + project_id


def _validated_task_name(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(TASK_PREFIX):
        raise ConfigurationError("Coordinator scheduled task name is invalid")
    project_id = value[len(TASK_PREFIX) :]
    if value != _task_name(project_id):
        raise ConfigurationError("Coordinator scheduled task name is invalid")
    return value


def coordinator_config_path(*, runtime_root: Path, project_id: str) -> Path:
    _task_name(project_id)
    if not isinstance(runtime_root, Path) or not runtime_root.is_absolute():
        raise ConfigurationError("Coordinator runtime root must be absolute")
    return runtime_root / "coordinator" / f"{project_id}.json"


def coordinator_receipt_path(*, runtime_root: Path, project_id: str) -> Path:
    return coordinator_config_path(
        runtime_root=runtime_root, project_id=project_id
    ).with_suffix(".last-run.json")


def resolve_aria_executable(value: Path | None = None) -> Path:
    candidate: Path
    if value is not None:
        candidate = value
    else:
        located = shutil.which("aria")
        if located is None:
            raise ConfigurationError("Installed aria executable was not found")
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError("ARIA executable is unavailable") from error
    if not resolved.is_file():
        raise ConfigurationError("ARIA executable is unavailable")
    return resolved


@dataclass(frozen=True)
class ScheduledTaskInspection:
    task_name: str
    command: str
    arguments: str
    interval_minutes: int
    logon_type: str
    multiple_instances: str


class TaskScheduler(Protocol):
    def install(
        self,
        *,
        task_name: str,
        command: Path,
        arguments: str,
        interval_minutes: int,
    ) -> ScheduledTaskInspection: ...

    def inspect(self, *, task_name: str) -> ScheduledTaskInspection | None: ...

    def remove(self, *, task_name: str) -> bool: ...

    def run(self, *, task_name: str) -> None: ...


def _current_user_sid() -> str:
    try:
        completed = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkflowError("Cannot resolve the current Windows user SID") from error
    if completed.returncode != 0:
        raise WorkflowError("Cannot resolve the current Windows user SID")
    matches = re.findall(rb"S-1-(?:\d+-){1,14}\d+", completed.stdout)
    if len(matches) != 1:
        raise WorkflowError("Current Windows user SID response is invalid")
    return matches[0].decode("ascii")


def _task_xml(
    *,
    sid: str,
    command: Path,
    arguments: str,
    interval_minutes: int,
    start_at: datetime,
) -> bytes:
    if SID_RE.fullmatch(sid) is None:
        raise ConfigurationError("Windows task SID is invalid")
    if type(interval_minutes) is not int or not 1 <= interval_minutes <= 60:
        raise ConfigurationError("Coordinator interval must be between 1 and 60 minutes")
    ET.register_namespace("", TASK_NS)
    task = ET.Element(f"{{{TASK_NS}}}Task", {"version": "1.3"})
    registration = ET.SubElement(task, f"{{{TASK_NS}}}RegistrationInfo")
    ET.SubElement(registration, f"{{{TASK_NS}}}Description").text = (
        "ARIA 1.5.5 collaborative coordinator polling worker"
    )
    triggers = ET.SubElement(task, f"{{{TASK_NS}}}Triggers")
    trigger = ET.SubElement(triggers, f"{{{TASK_NS}}}TimeTrigger")
    ET.SubElement(trigger, f"{{{TASK_NS}}}StartBoundary").text = start_at.astimezone().isoformat(
        timespec="seconds"
    )
    ET.SubElement(trigger, f"{{{TASK_NS}}}Enabled").text = "true"
    repetition = ET.SubElement(trigger, f"{{{TASK_NS}}}Repetition")
    ET.SubElement(repetition, f"{{{TASK_NS}}}Interval").text = (
        f"PT{interval_minutes}M"
    )
    ET.SubElement(repetition, f"{{{TASK_NS}}}StopAtDurationEnd").text = "false"
    principals = ET.SubElement(task, f"{{{TASK_NS}}}Principals")
    principal = ET.SubElement(principals, f"{{{TASK_NS}}}Principal", {"id": "Author"})
    ET.SubElement(principal, f"{{{TASK_NS}}}UserId").text = sid
    ET.SubElement(principal, f"{{{TASK_NS}}}LogonType").text = "InteractiveToken"
    ET.SubElement(principal, f"{{{TASK_NS}}}RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, f"{{{TASK_NS}}}Settings")
    values = {
        "MultipleInstancesPolicy": "IgnoreNew",
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "AllowHardTerminate": "true",
        "StartWhenAvailable": "true",
        "RunOnlyIfNetworkAvailable": "true",
        "Enabled": "true",
        "Hidden": "true",
        "ExecutionTimeLimit": "PT10M",
        "Priority": "7",
    }
    for name, text in values.items():
        ET.SubElement(settings, f"{{{TASK_NS}}}{name}").text = text
    actions = ET.SubElement(task, f"{{{TASK_NS}}}Actions", {"Context": "Author"})
    execute = ET.SubElement(actions, f"{{{TASK_NS}}}Exec")
    ET.SubElement(execute, f"{{{TASK_NS}}}Command").text = str(command)
    ET.SubElement(execute, f"{{{TASK_NS}}}Arguments").text = arguments
    buffer = io.BytesIO()
    ET.ElementTree(task).write(buffer, encoding="utf-16", xml_declaration=True)
    return buffer.getvalue()


def _xml_text(root: ET.Element, path: str, label: str) -> str:
    node = root.find(path)
    if node is None or node.text is None or not node.text.strip():
        raise WorkflowError(f"Scheduled task {label} is missing")
    return node.text.strip()


def _parse_schtasks_xml(content: bytes) -> ET.Element:
    if content.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")) or b"\x00" in content[:100]:
        try:
            return ET.fromstring(content)
        except ET.ParseError as error:
            raise WorkflowError("Coordinator scheduled task XML is invalid") from error
    try:
        text = content.decode("mbcs" if os.name == "nt" else "utf-8")
    except UnicodeDecodeError as error:
        raise WorkflowError("Coordinator scheduled task XML encoding is invalid") from error
    text = re.sub(r"^\s*<\?xml[^?]*\?>", "", text, count=1)
    try:
        return ET.fromstring(text)
    except ET.ParseError as error:
        raise WorkflowError("Coordinator scheduled task XML is invalid") from error


class WindowsTaskScheduler:
    def __init__(
        self,
        *,
        sid_resolver: Callable[[], str] = _current_user_sid,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if os.name != "nt":
            raise ConfigurationError("Coordinator scheduling is supported on Windows only")
        self._sid_resolver = sid_resolver
        self._now = now

    @staticmethod
    def _run(
        arguments: list[str], *, missing_task_name: str | None = None
    ) -> bytes | None:
        try:
            completed = subprocess.run(
                ["schtasks.exe", *arguments],
                check=False,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise WorkflowError("Windows Task Scheduler operation failed") from error
        if completed.returncode != 0:
            if missing_task_name is not None:
                try:
                    listing = subprocess.run(
                        ["schtasks.exe", "/Query", "/FO", "CSV", "/NH"],
                        check=False,
                        capture_output=True,
                        timeout=60,
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    raise WorkflowError(
                        "Windows Task Scheduler inventory failed"
                    ) from error
                if listing.returncode != 0:
                    raise WorkflowError("Windows Task Scheduler inventory failed")
                try:
                    text = listing.stdout.decode("mbcs")
                    names = {
                        row[0].lstrip("\\")
                        for row in csv.reader(io.StringIO(text))
                        if row
                    }
                except (UnicodeDecodeError, csv.Error) as error:
                    raise WorkflowError(
                        "Windows Task Scheduler inventory is invalid"
                    ) from error
                if missing_task_name not in names:
                    return None
            raise WorkflowError("Windows Task Scheduler operation failed")
        return completed.stdout

    def install(
        self,
        *,
        task_name: str,
        command: Path,
        arguments: str,
        interval_minutes: int,
    ) -> ScheduledTaskInspection:
        _validated_task_name(task_name)
        payload = _task_xml(
            sid=self._sid_resolver(),
            command=command,
            arguments=arguments,
            interval_minutes=interval_minutes,
            start_at=self._now() + timedelta(minutes=1),
        )
        with tempfile.TemporaryDirectory(prefix="aria-coordinator-task-") as temporary:
            task_file = Path(temporary) / "task.xml"
            task_file.write_bytes(payload)
            self._run(
                ["/Create", "/TN", task_name, "/XML", str(task_file), "/F"]
            )
        try:
            inspection = self.inspect(task_name=task_name)
        except Exception as error:
            try:
                self._run(["/Delete", "/TN", task_name, "/F"])
            except Exception as cleanup_error:
                raise WorkflowError(
                    "Coordinator scheduled task read-back failed and cleanup was incomplete"
                ) from error
            raise
        if inspection is None:
            self._run(["/Delete", "/TN", task_name, "/F"])
            raise WorkflowError("Coordinator scheduled task read-back failed")
        expected = ScheduledTaskInspection(
            task_name=task_name,
            command=str(command),
            arguments=arguments,
            interval_minutes=interval_minutes,
            logon_type="InteractiveToken",
            multiple_instances="IgnoreNew",
        )
        if inspection != expected:
            self.remove(task_name=task_name)
            raise WorkflowError("Coordinator scheduled task read-back mismatch")
        return inspection

    def inspect(self, *, task_name: str) -> ScheduledTaskInspection | None:
        _validated_task_name(task_name)
        output = self._run(
            ["/Query", "/TN", task_name, "/XML"],
            missing_task_name=task_name,
        )
        if output is None:
            return None
        root = _parse_schtasks_xml(output)
        interval = _xml_text(root, ".//{*}Repetition/{*}Interval", "interval")
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", interval)
        if match is None or (match.group(1) is None and match.group(2) is None):
            raise WorkflowError("Coordinator scheduled task interval is invalid")
        interval_minutes = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
        if not 1 <= interval_minutes <= 60:
            raise WorkflowError("Coordinator scheduled task interval is invalid")
        return ScheduledTaskInspection(
            task_name=task_name,
            command=_xml_text(root, ".//{*}Actions/{*}Exec/{*}Command", "command"),
            arguments=_xml_text(
                root, ".//{*}Actions/{*}Exec/{*}Arguments", "arguments"
            ),
            interval_minutes=interval_minutes,
            logon_type=_xml_text(
                root, ".//{*}Principals/{*}Principal/{*}LogonType", "logon type"
            ),
            multiple_instances=_xml_text(
                root, ".//{*}Settings/{*}MultipleInstancesPolicy", "instance policy"
            ),
        )

    def remove(self, *, task_name: str) -> bool:
        if self.inspect(task_name=task_name) is None:
            return False
        self._run(["/Delete", "/TN", task_name, "/F"])
        if self.inspect(task_name=task_name) is not None:
            raise WorkflowError("Coordinator scheduled task removal read-back failed")
        return True

    def run(self, *, task_name: str) -> None:
        if self.inspect(task_name=task_name) is None:
            raise WorkflowError("Coordinator scheduled task is not installed")
        self._run(["/Run", "/TN", task_name])


def _configuration(
    project: ProjectConfig,
    *,
    client_id: str,
    coordinator_integration_id: int,
    aria_executable: Path,
    interval_minutes: int,
    max_requests: int,
    max_pull_requests: int,
    installed_at: str,
) -> dict[str, object]:
    if project.collaboration_mode != "collaborative":
        raise WorkflowError("Coordinator scheduling requires a collaborative project")
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    if contract.provider != "github":
        raise WorkflowError("Coordinator scheduling currently requires GitHub")
    if CLIENT_ID_RE.fullmatch(client_id) is None:
        raise ConfigurationError("Coordinator GitHub client id is invalid")
    if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
        raise ConfigurationError("Coordinator integration id must be positive")
    if type(interval_minutes) is not int or not 1 <= interval_minutes <= 60:
        raise ConfigurationError("Coordinator interval must be between 1 and 60 minutes")
    if type(max_requests) is not int or not 1 <= max_requests <= 100:
        raise ConfigurationError("Coordinator request limit must be between 1 and 100")
    if type(max_pull_requests) is not int or not 1 <= max_pull_requests <= 100:
        raise ConfigurationError("Coordinator pull request limit must be between 1 and 100")
    executable = resolve_aria_executable(aria_executable)
    machine_runtime_root = project.registry_path.parent.resolve(strict=False)
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "project_id": project.project_id,
        "repository_id": contract.repository_id,
        "runtime_root": str(machine_runtime_root),
        "framework_root": str(project.framework_root.resolve(strict=True)),
        "aria_executable": str(executable),
        "aria_executable_sha256": _file_sha256(executable),
        "github_client_id": client_id,
        "coordinator_integration_id": coordinator_integration_id,
        "interval_minutes": interval_minutes,
        "max_requests": max_requests,
        "max_pull_requests": max_pull_requests,
        "task_name": _task_name(project.project_id),
        "installed_at": installed_at,
    }


CONFIG_KEYS = {
    "schema_version",
    "project_id",
    "repository_id",
    "runtime_root",
    "framework_root",
    "aria_executable",
    "aria_executable_sha256",
    "github_client_id",
    "coordinator_integration_id",
    "interval_minutes",
    "max_requests",
    "max_pull_requests",
    "task_name",
    "installed_at",
}


def _load_configuration_bytes(content: bytes) -> dict[str, object]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Coordinator configuration is invalid") from error
    if not isinstance(value, dict) or set(value) != CONFIG_KEYS:
        raise ConfigurationError("Coordinator configuration schema is invalid")
    if value.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigurationError("Coordinator configuration version is invalid")
    project_id = value.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError("Coordinator configuration project is invalid")
    if value.get("task_name") != _task_name(project_id):
        raise ConfigurationError("Coordinator configuration task is invalid")
    if not isinstance(value.get("repository_id"), str) or not value["repository_id"].isdecimal():
        raise ConfigurationError("Coordinator configuration repository is invalid")
    if (
        not isinstance(value.get("github_client_id"), str)
        or CLIENT_ID_RE.fullmatch(value["github_client_id"]) is None
    ):
        raise ConfigurationError("Coordinator configuration client id is invalid")
    for key in ("runtime_root", "framework_root", "aria_executable"):
        raw = value.get(key)
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise ConfigurationError(f"Coordinator configuration {key} is invalid")
    if (
        not isinstance(value.get("aria_executable_sha256"), str)
        or SHA256_RE.fullmatch(value["aria_executable_sha256"]) is None
    ):
        raise ConfigurationError("Coordinator executable hash is invalid")
    for key, maximum in (
        ("coordinator_integration_id", 2**63 - 1),
        ("interval_minutes", 60),
        ("max_requests", 100),
        ("max_pull_requests", 100),
    ):
        number = value.get(key)
        if type(number) is not int or not 1 <= number <= maximum:
            raise ConfigurationError(f"Coordinator configuration {key} is invalid")
    installed_at = value.get("installed_at")
    if not isinstance(installed_at, str) or not installed_at.endswith("Z"):
        raise ConfigurationError("Coordinator configuration timestamp is invalid")
    try:
        datetime.fromisoformat(installed_at[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError("Coordinator configuration timestamp is invalid") from error
    return value


def load_coordinator_configuration(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ConfigurationError("Coordinator configuration was not found") from error
    value = _load_configuration_bytes(content)
    expected_path = coordinator_config_path(
        runtime_root=Path(str(value["runtime_root"])),
        project_id=str(value["project_id"]),
    ).resolve(strict=False)
    if path.resolve(strict=True) != expected_path:
        raise ConfigurationError("Coordinator configuration path is invalid")
    return value, content


def _task_arguments(config_path: Path, config_sha256: str) -> str:
    return subprocess.list2cmdline(
        [
            "coordinator",
            "run",
            "--config",
            str(config_path),
            "--expected-config-sha256",
            config_sha256,
        ]
    )


def install_coordinator_schedule(
    project: ProjectConfig,
    *,
    client_id: str,
    coordinator_integration_id: int,
    aria_executable: Path,
    interval_minutes: int = 1,
    max_requests: int = 20,
    max_pull_requests: int = 20,
    scheduler: TaskScheduler | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    backend = scheduler or WindowsTaskScheduler()
    path = coordinator_config_path(
        runtime_root=project.registry_path.parent.resolve(strict=False),
        project_id=project.project_id,
    )
    previous_configuration: dict[str, object] | None = None
    previous: bytes | None = None
    if path.is_file():
        previous_configuration, previous = load_coordinator_configuration(path)
    configuration = _configuration(
        project,
        client_id=client_id,
        coordinator_integration_id=coordinator_integration_id,
        aria_executable=aria_executable,
        interval_minutes=interval_minutes,
        max_requests=max_requests,
        max_pull_requests=max_pull_requests,
        installed_at=_stamp(now),
    )
    content = json_bytes(configuration)
    digest = _sha256(content)
    arguments = _task_arguments(path.resolve(strict=False), digest)
    atomic_write_bytes(path, content)
    try:
        inspection = backend.install(
            task_name=str(configuration["task_name"]),
            command=Path(str(configuration["aria_executable"])),
            arguments=arguments,
            interval_minutes=interval_minutes,
        )
    except Exception as error:
        rollback_error: Exception | None = None
        if previous is None:
            path.unlink(missing_ok=True)
            try:
                backend.remove(task_name=str(configuration["task_name"]))
            except Exception as cleanup_error:
                rollback_error = cleanup_error
        else:
            atomic_write_bytes(path, previous)
            assert previous_configuration is not None
            try:
                backend.install(
                    task_name=str(previous_configuration["task_name"]),
                    command=Path(str(previous_configuration["aria_executable"])),
                    arguments=_task_arguments(
                        path.resolve(strict=True), _sha256(previous)
                    ),
                    interval_minutes=int(previous_configuration["interval_minutes"]),
                )
            except Exception as cleanup_error:
                rollback_error = cleanup_error
        if rollback_error is not None:
            raise WorkflowError(
                "Coordinator task installation failed and rollback was incomplete"
            ) from error
        raise
    return {
        "ok": True,
        "project": project.project_id,
        "installed": True,
        "task_name": inspection.task_name,
        "interval_minutes": inspection.interval_minutes,
        "config_sha256": digest,
        "credential_store": "windows-credential-manager",
        "secrets_in_task": False,
    }


def coordinator_schedule_status(
    *,
    project_id: str,
    runtime_root: Path | None = None,
    scheduler: TaskScheduler | None = None,
) -> dict[str, object]:
    runtime = (runtime_root or default_runtime_root()).resolve(strict=False)
    path = coordinator_config_path(runtime_root=runtime, project_id=project_id)
    backend = scheduler or WindowsTaskScheduler()
    inspection = backend.inspect(task_name=_task_name(project_id))
    if not path.is_file():
        return {
            "ok": inspection is None,
            "project": project_id,
            "configured": False,
            "installed": inspection is not None,
            "healthy": False,
        }
    configuration, content = load_coordinator_configuration(path)
    digest = _sha256(content)
    executable = Path(str(configuration["aria_executable"]))
    executable_ok = (
        executable.is_file()
        and _file_sha256(executable) == configuration["aria_executable_sha256"]
    )
    expected = ScheduledTaskInspection(
        task_name=str(configuration["task_name"]),
        command=str(executable),
        arguments=_task_arguments(path.resolve(strict=True), digest),
        interval_minutes=int(configuration["interval_minutes"]),
        logon_type="InteractiveToken",
        multiple_instances="IgnoreNew",
    )
    task_ok = inspection == expected
    receipt_path = coordinator_receipt_path(
        runtime_root=runtime, project_id=project_id
    )
    last_run = None
    if receipt_path.is_file():
        try:
            last_run = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            last_run = {"valid": False}
    return {
        "ok": task_ok and executable_ok,
        "project": project_id,
        "configured": True,
        "installed": inspection is not None,
        "healthy": task_ok and executable_ok,
        "task_name": configuration["task_name"],
        "interval_minutes": configuration["interval_minutes"],
        "config_sha256": digest,
        "executable_integrity": executable_ok,
        "task_read_back": task_ok,
        "last_run": last_run,
    }


def remove_coordinator_schedule(
    *,
    project_id: str,
    runtime_root: Path | None = None,
    scheduler: TaskScheduler | None = None,
) -> dict[str, object]:
    runtime = (runtime_root or default_runtime_root()).resolve(strict=False)
    path = coordinator_config_path(runtime_root=runtime, project_id=project_id)
    backend = scheduler or WindowsTaskScheduler()
    removed_task = backend.remove(task_name=_task_name(project_id))
    removed_config = path.is_file()
    path.unlink(missing_ok=True)
    return {
        "ok": True,
        "project": project_id,
        "installed": False,
        "removed_task": removed_task,
        "removed_config": removed_config,
        "runtime_preserved": True,
    }


def trigger_coordinator_schedule(
    *, project_id: str, scheduler: TaskScheduler | None = None
) -> dict[str, object]:
    backend = scheduler or WindowsTaskScheduler()
    task_name = _task_name(project_id)
    backend.run(task_name=task_name)
    return {
        "ok": True,
        "project": project_id,
        "task_name": task_name,
        "triggered": True,
    }


def run_configured_coordinator(
    *, config_path: Path, expected_config_sha256: str
) -> dict[str, object]:
    if SHA256_RE.fullmatch(expected_config_sha256) is None:
        raise ConfigurationError("Coordinator expected configuration hash is invalid")
    configuration, content = load_coordinator_configuration(config_path)
    if _sha256(content) != expected_config_sha256:
        raise WorkflowError("Coordinator configuration hash mismatch")
    executable = Path(str(configuration["aria_executable"]))
    if (
        not executable.is_file()
        or _file_sha256(executable) != configuration["aria_executable_sha256"]
    ):
        raise WorkflowError("Coordinator executable integrity check failed")
    runtime = Path(str(configuration["runtime_root"]))
    project_id = str(configuration["project_id"])
    receipt = coordinator_receipt_path(runtime_root=runtime, project_id=project_id)
    started_at = _stamp()
    lock = runtime / "locks" / f"coordinator-worker-{project_id}.lock"
    try:
        with exclusive_lock(lock, timeout_seconds=0):
            project = load_project(
                project_id,
                framework_root=Path(str(configuration["framework_root"])),
                runtime_root=runtime,
            )
            contract = load_control_contract(project.docs_root / "CONTROL.yaml")
            if contract.repository_id != configuration["repository_id"]:
                raise WorkflowError("Coordinator repository identity changed")
            from aria.collaborative_worker import run_collaborative_coordinator_once
            from aria.github_runtime import (
                build_authenticated_github_adapter,
                build_github_app_integration_verifier,
                build_github_app_request_queue,
                build_github_control_writer,
                build_github_git_environment,
            )

            client_id = str(configuration["github_client_id"])
            app_id = int(configuration["coordinator_integration_id"])
            adapter = build_authenticated_github_adapter(
                code_root=project.code_root,
                remote=contract.remote,
                client_id=client_id,
                coordinator_integration_id=app_id,
            )
            queue = build_github_app_request_queue(
                code_root=project.code_root,
                remote=contract.remote,
                repository_id=contract.repository_id,
                app_id=app_id,
            )
            writer = build_github_control_writer(
                code_root=project.code_root,
                remote=contract.remote,
                repository_id=contract.repository_id,
                app_id=app_id,
                control_branch=contract.control_branch,
            )
            verifier = build_github_app_integration_verifier(
                code_root=project.code_root,
                remote=contract.remote,
                repository_id=contract.repository_id,
                app_id=app_id,
            )
            result = run_collaborative_coordinator_once(
                project,
                coordinator_adapter=adapter,
                request_queue=queue,
                control_writer=writer,
                integration_verifier=verifier,
                coordinator_integration_id=app_id,
                max_requests=int(configuration["max_requests"]),
                max_pull_requests=int(configuration["max_pull_requests"]),
                git_environment=build_github_git_environment(client_id=client_id),
            )
    except AriaError as error:
        if str(error).startswith("Timed out waiting for lock:"):
            result = {
                "ok": True,
                "project": project_id,
                "skipped": True,
                "reason": "coordinator-already-running",
            }
        else:
            atomic_write_bytes(
                receipt,
                json_bytes(
                    {
                        "schema_version": 1,
                        "project_id": project_id,
                        "started_at": started_at,
                        "finished_at": _stamp(),
                        "ok": False,
                        "error": type(error).__name__,
                    }
                ),
            )
            raise
    atomic_write_bytes(
        receipt,
        json_bytes(
            {
                "schema_version": 1,
                "project_id": project_id,
                "started_at": started_at,
                "finished_at": _stamp(),
                "ok": bool(result.get("ok")),
                "skipped": bool(result.get("skipped", False)),
                "request_count": len(result.get("requests", {}).get("processed", []))
                if isinstance(result.get("requests"), dict)
                else 0,
                "pull_request_count": len(result.get("pull_requests", {}).get("processed", []))
                if isinstance(result.get("pull_requests"), dict)
                else 0,
            }
        ),
    )
    if result.get("ok") is not True:
        raise WorkflowError("Coordinator polling pass failed")
    return result
