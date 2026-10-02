from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aria.collaboration import build_control_contract, dump_control_contract
from aria.coordinator_scheduler import (
    ScheduledTaskInspection,
    WindowsTaskScheduler,
    _parse_schtasks_xml,
    _task_xml,
    coordinator_config_path,
    coordinator_schedule_status,
    install_coordinator_schedule,
    load_coordinator_configuration,
    remove_coordinator_schedule,
    run_configured_coordinator,
)
from aria.errors import AriaError, WorkflowError


class _Scheduler:
    def __init__(self, *, fail_installs: int = 0) -> None:
        self.task: ScheduledTaskInspection | None = None
        self.fail_installs = fail_installs
        self.triggered = False

    def install(self, *, task_name, command, arguments, interval_minutes):
        if self.fail_installs:
            self.fail_installs -= 1
            raise WorkflowError("simulated task failure")
        self.task = ScheduledTaskInspection(
            task_name,
            str(command),
            arguments,
            interval_minutes,
            "InteractiveToken",
            "IgnoreNew",
        )
        return self.task

    def inspect(self, *, task_name):
        return self.task if self.task is not None and self.task.task_name == task_name else None

    def remove(self, *, task_name):
        existed = self.inspect(task_name=task_name) is not None
        self.task = None
        return existed

    def run(self, *, task_name):
        if self.inspect(task_name=task_name) is None:
            raise WorkflowError("missing task")
        self.triggered = True


class CoordinatorSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.docs = self.root / "control"
        self.docs.mkdir()
        self.framework = self.root / "framework"
        self.framework.mkdir()
        self.executable = self.root / "aria.exe"
        self.executable.write_bytes(b"aria launcher")
        contract = build_control_contract(
            project_id="demo", provider="github", repository_id="123456789"
        )
        (self.docs / "CONTROL.yaml").write_text(
            dump_control_contract(contract), encoding="utf-8"
        )
        self.project = SimpleNamespace(
            project_id="demo",
            collaboration_mode="collaborative",
            docs_root=self.docs,
            runtime_root=self.runtime,
            registry_path=self.runtime / "projects.toml",
            framework_root=self.framework,
            code_root=self.root / "code",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install(self, scheduler: _Scheduler):
        return install_coordinator_schedule(
            self.project,
            client_id="Iv1.client123",
            coordinator_integration_id=9001,
            aria_executable=self.executable,
            interval_minutes=2,
            max_requests=7,
            max_pull_requests=9,
            scheduler=scheduler,
            now=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        )

    def test_install_status_and_remove_are_hash_bound_and_secret_free(self) -> None:
        scheduler = _Scheduler()
        installed = self._install(scheduler)
        path = coordinator_config_path(runtime_root=self.runtime, project_id="demo")
        configuration, content = load_coordinator_configuration(path)
        self.assertEqual(installed["config_sha256"], hashlib.sha256(content).hexdigest())
        self.assertNotIn("token", content.decode("utf-8").lower())
        self.assertNotIn("Iv1.client123", scheduler.task.arguments)
        self.assertNotIn("9001", scheduler.task.arguments)
        self.assertEqual(configuration["max_pull_requests"], 9)

        status = coordinator_schedule_status(
            project_id="demo", runtime_root=self.runtime, scheduler=scheduler
        )
        self.assertTrue(status["healthy"])
        removed = remove_coordinator_schedule(
            project_id="demo", runtime_root=self.runtime, scheduler=scheduler
        )
        self.assertTrue(removed["removed_task"])
        self.assertFalse(path.exists())
        self.assertTrue(self.runtime.exists())

    def test_failed_task_install_rolls_back_configuration(self) -> None:
        path = coordinator_config_path(runtime_root=self.runtime, project_id="demo")
        with self.assertRaisesRegex(WorkflowError, "simulated"):
            self._install(_Scheduler(fail_installs=1))
        self.assertFalse(path.exists())

    def test_failed_reconfiguration_restores_previous_config_and_task(self) -> None:
        scheduler = _Scheduler()
        self._install(scheduler)
        path = coordinator_config_path(runtime_root=self.runtime, project_id="demo")
        previous = path.read_bytes()
        previous_task = scheduler.task
        scheduler.fail_installs = 1
        with self.assertRaisesRegex(WorkflowError, "simulated"):
            self._install(scheduler)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(scheduler.task, previous_task)

    def test_task_xml_uses_interactive_least_privilege_and_ignore_new(self) -> None:
        payload = _task_xml(
            sid="S-1-5-21-1-2-3-1001",
            command=self.executable,
            arguments="coordinator run --config test",
            interval_minutes=3,
            start_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        )
        root = ET.fromstring(payload)
        self.assertEqual(root.attrib["version"], "1.3")
        self.assertEqual(root.findtext(".//{*}LogonType"), "InteractiveToken")
        self.assertEqual(root.findtext(".//{*}RunLevel"), "LeastPrivilege")
        self.assertEqual(root.findtext(".//{*}MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(root.findtext(".//{*}Interval"), "PT3M")
        redirected = payload.decode("utf-16").encode("mbcs")
        redirected_root = _parse_schtasks_xml(redirected)
        self.assertEqual(
            redirected_root.findtext(".//{*}MultipleInstancesPolicy"),
            "IgnoreNew",
        )

    def test_failed_exact_query_proves_absence_from_full_inventory(self) -> None:
        missing = SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
        inventory = SimpleNamespace(
            returncode=0,
            stdout=b'"\\Other-Task","N/A"\r\n',
            stderr=b"",
        )
        with mock.patch(
            "aria.coordinator_scheduler.subprocess.run",
            side_effect=[missing, inventory],
        ):
            result = WindowsTaskScheduler._run(
                ["/Query", "/TN", "ARIA-Codex-Coordinator-demo", "/XML"],
                missing_task_name="ARIA-Codex-Coordinator-demo",
            )
        self.assertIsNone(result)

    def test_failed_exact_query_does_not_hide_an_existing_task(self) -> None:
        missing = SimpleNamespace(returncode=1, stdout=b"", stderr=b"")
        inventory = SimpleNamespace(
            returncode=0,
            stdout=b'"\\ARIA-Codex-Coordinator-demo","N/A"\r\n',
            stderr=b"",
        )
        with mock.patch(
            "aria.coordinator_scheduler.subprocess.run",
            side_effect=[missing, inventory],
        ), self.assertRaisesRegex(WorkflowError, "operation failed"):
            WindowsTaskScheduler._run(
                ["/Query", "/TN", "ARIA-Codex-Coordinator-demo", "/XML"],
                missing_task_name="ARIA-Codex-Coordinator-demo",
            )

    def test_configured_run_builds_boundaries_and_writes_safe_receipt(self) -> None:
        scheduler = _Scheduler()
        installed = self._install(scheduler)
        path = coordinator_config_path(runtime_root=self.runtime, project_id="demo")
        contract = build_control_contract(
            project_id="demo", provider="github", repository_id="123456789"
        )
        with mock.patch(
            "aria.coordinator_scheduler.load_project", return_value=self.project
        ), mock.patch(
            "aria.coordinator_scheduler.load_control_contract", return_value=contract
        ), mock.patch(
            "aria.github_runtime.build_authenticated_github_adapter",
            return_value="adapter",
        ), mock.patch(
            "aria.github_runtime.build_github_app_request_queue", return_value="queue"
        ), mock.patch(
            "aria.github_runtime.build_github_control_writer", return_value="writer"
        ), mock.patch(
            "aria.github_runtime.build_github_app_integration_verifier",
            return_value="verifier",
        ), mock.patch(
            "aria.github_runtime.build_github_git_environment",
            return_value={"GIT_ASKPASS": "helper"},
        ), mock.patch(
            "aria.collaborative_worker.run_collaborative_coordinator_once",
            return_value={
                "ok": True,
                "requests": {"processed": [{"ok": True}]},
                "pull_requests": {"processed": [{"ok": True}]},
            },
        ) as run_once:
            result = run_configured_coordinator(
                config_path=path,
                expected_config_sha256=installed["config_sha256"],
            )
        self.assertTrue(result["ok"])
        self.assertEqual(run_once.call_args.kwargs["max_requests"], 7)
        receipt = json.loads(
            path.with_suffix(".last-run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["request_count"], 1)
        self.assertEqual(receipt["pull_request_count"], 1)
        self.assertNotIn("token", json.dumps(receipt).lower())

    def test_config_hash_tamper_fails_before_any_provider_call(self) -> None:
        scheduler = _Scheduler()
        installed = self._install(scheduler)
        path = coordinator_config_path(runtime_root=self.runtime, project_id="demo")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["max_requests"] = 8
        path.write_text(json.dumps(value), encoding="utf-8")
        with mock.patch(
            "aria.coordinator_scheduler.load_project"
        ) as load, self.assertRaisesRegex(WorkflowError, "hash mismatch"):
            run_configured_coordinator(
                config_path=path,
                expected_config_sha256=installed["config_sha256"],
            )
        load.assert_not_called()

    def test_overlapping_scheduled_run_is_a_safe_noop(self) -> None:
        scheduler = _Scheduler()
        installed = self._install(scheduler)
        path = coordinator_config_path(runtime_root=self.runtime, project_id="demo")
        with mock.patch(
            "aria.coordinator_scheduler.exclusive_lock",
            side_effect=AriaError("Timed out waiting for lock: worker"),
        ), mock.patch("aria.coordinator_scheduler.load_project") as load:
            result = run_configured_coordinator(
                config_path=path,
                expected_config_sha256=installed["config_sha256"],
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
