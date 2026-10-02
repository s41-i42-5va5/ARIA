from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

import yaml

from aria.cli import build_parser
from aria.errors import ConfigurationError, WorkflowError
from aria.execution import validate_execution_bundle, verify_project_run
from aria.integrity import engine_state
from aria.lifecycle import (
    amend_feature_contract,
    begin_implementation,
    converge_feature,
    export_spec_kit,
    import_spec_kit,
    lifecycle_status,
    start_feature,
    submit_lifecycle_phase,
)
from aria.migration_1_4 import _team_template, _trust_template
from aria.project import (
    _framework_root,
    canonical_sha,
    git_snapshot,
    history_events,
    load_project,
    run_project_doctor,
    safe_relative_path,
    verify_history,
)
from aria.project_activation import activate_project, run_project_canary
from aria.registry import read_registry, register_project
from aria.simple_run import (
    approve_project_spec,
    build_review_scope,
    close_project_run,
    decide_run_route,
    lock_project_feature_contract,
    project_status,
    role_target_identity,
    start_project_run,
    verify_completed_project_run,
)


class ProjectRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.framework = Path(__file__).resolve().parents[1]
        self.docs = base / "project-docs"
        self.code = base / "product-code"
        self.runtime = base / "runtime"
        self._verification_evidence: dict[str, list[dict[str, object]]] = {}
        self.docs.mkdir()
        self.code.mkdir()
        (self.docs / "specs" / "active").mkdir(parents=True)
        (self.docs / "adr").mkdir()
        (self.docs / "knowledge").mkdir()
        (self.code / "backend").mkdir()
        (self.code / "frontend").mkdir()
        (self.docs / "PROJECT.yaml").write_text(
            """schema_version: 1
project_id: demo
display_name: Demo
documents:
  state: STATE.yaml
  stack: STACK.md
  history: HISTORY.jsonl
  specs: specs
  adr: adr
  knowledge: knowledge
  team: ARIA_TEAM.yaml
  trust: TRUST.yaml
context:
  default_budget_bytes: 131072
  state_budget_bytes: 32768
  stack_manifests:
    - backend/pyproject.toml
""",
            encoding="utf-8",
        )
        (self.docs / "ARIA_TEAM.yaml").write_bytes(_team_template())
        (self.docs / "TRUST.yaml").write_bytes(_trust_template())
        (self.docs / "STATE.yaml").write_text(
            """schema_version: 1
project_id: demo
frontier:
  task_id: demo_task
  intent: null
  mode: null
  stage: null
  next_action: null
  spec: specs/active/demo_task.md
blockers: []
last_completed: null
history_checkpoint:
  sequence: 1
""",
            encoding="utf-8",
        )
        (self.docs / "STACK.md").write_text(
            "# Working stack\n\n- Python and pytest.\n",
            encoding="utf-8",
        )
        (self.docs / "specs" / "active" / "demo_task.md").write_text(
            "# Demo spec\n\n- Acceptance: behavior works.\n", encoding="utf-8"
        )
        (self.code / "backend" / "service.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        (self.code / "frontend" / "view.ts").write_text(
            "export const value = 1;\n", encoding="utf-8"
        )
        (self.code / "backend" / "pyproject.toml").write_text(
            "[project]\nname='demo'\n", encoding="utf-8"
        )
        event = {
            "schema_version": 1,
            "sequence": 1,
            "timestamp": "2026-07-17T00:00:00Z",
            "type": "project_created",
            "project_id": "demo",
            "task_id": None,
            "git_head": None,
            "previous_event_sha256": None,
            "refs": [],
            "result": {"summary": "fixture"},
        }
        event["event_sha256"] = canonical_sha(event)
        (self.docs / "HISTORY.jsonl").write_text(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        state_text = (self.docs / "STATE.yaml").read_text(encoding="utf-8")
        (self.docs / "STATE.yaml").write_text(
            state_text.replace(
                "history_checkpoint:\n  sequence: 1\n",
                "history_checkpoint:\n"
                "  sequence: 1\n"
                f"  event_sha256: {event['event_sha256']}\n",
            ),
            encoding="utf-8",
        )
        self._git("init", "-q")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "ARIA Tests")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "fixture")
        git = git_snapshot(self.code)
        (self.docs / "SYSTEM_MAP.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "project_id": "demo",
                    "generated_from": {
                        "git_head": git["head"],
                        "working_tree_sha256": canonical_sha(git["changes"]["paths"]),
                    },
                    "dimensions": {
                        "layers": ["application"],
                        "domains": ["demo"],
                        "runtime_surfaces": ["python"],
                        "cross_cutting": ["correctness"],
                    },
                    "components": [
                        {
                            "id": "backend",
                            "name": "Backend",
                            "paths": ["backend/**"],
                            "layer": "application",
                            "domain": "demo",
                            "responsibilities": ["demo behavior"],
                            "depends_on": [],
                            "risks": ["incorrect result"],
                            "test_seams": ["pytest"],
                        },
                        {
                            "id": "frontend",
                            "name": "Frontend",
                            "paths": ["frontend/**"],
                            "layer": "presentation",
                            "domain": "demo",
                            "responsibilities": ["demo UI"],
                            "depends_on": ["backend"],
                            "risks": ["contract drift"],
                            "test_seams": ["browser test"],
                        },
                    ],
                    "shared_primitives": [],
                    "critical_flows": [
                        {
                            "id": "demo-flow",
                            "steps": ["backend", "frontend"],
                            "failure_modes": ["backend failure"],
                            "assurance": ["linked E2E"],
                        }
                    ],
                    "unknowns": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.registry = self.runtime / "projects.toml"
        self.registry.parent.mkdir(parents=True)
        self.registry.write_text(
            "schema_version = 1\n\n"
            "[projects.demo]\n"
            f'docs_root = "{self.docs.as_posix()}"\n'
            f'code_root = "{self.code.as_posix()}"\n'
            'mode = "shadow"\n',
            encoding="utf-8",
        )
        self.project = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.code), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def _active(self, project=None):
        return replace(
            project or self.project,
            mode="active",
            activation_engine_sha256=str(engine_state(self.framework)["sha256"]),
        )

    def _configure_execution(self, commands: list[dict[str, object]]) -> None:
        project_path = self.docs / "PROJECT.yaml"
        project_doc = yaml.safe_load(project_path.read_text(encoding="utf-8"))
        project_doc["documents"]["verification"] = "VERIFY.yaml"
        project_path.write_text(
            yaml.safe_dump(project_doc, sort_keys=False), encoding="utf-8"
        )
        (self.docs / "VERIFY.yaml").write_text(
            yaml.safe_dump(
                {"schema_version": 1, "commands": commands}, sort_keys=False
            ),
            encoding="utf-8",
        )
        self.project = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )

    def _python_execution_command(
        self, source: str, *, command_id: str = "focused-tests"
    ) -> dict[str, object]:
        return {
            "id": command_id,
            "adapter": "python",
            "argv": ["python", "-c", source],
            "cwd": ".",
            "classes": ["focused"],
            "timeout_seconds": 30,
        }

    def test_verify_creates_receipts_and_resumes_same_git_state(self) -> None:
        self._configure_execution(
            [self._python_execution_command("print('FOCUSED verified result')")]
        )
        started = start_project_run(
            self.project,
            task="Execute trusted focused verification",
            intent="build",
            mode="quick",
        )

        first = verify_project_run(self.project, run_id=str(started["run_id"]))
        second = verify_project_run(self.project, run_id=str(started["run_id"]))

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        execution_id = first["executions"][0]["execution_id"]
        self.assertEqual(second["reused_execution_ids"], [execution_id])
        manifest = json.loads(Path(started["manifest_path"]).read_text(encoding="utf-8"))
        receipts = validate_execution_bundle(
            self.project,
            Path(started["manifest_path"]).parent,
            manifest,
        )
        self.assertIn(execution_id, receipts)
        self.assertEqual(receipts[execution_id]["git"]["head"], git_snapshot(self.code)["head"])

    def test_verify_failure_is_fail_closed_and_not_resumed(self) -> None:
        self._configure_execution(
            [self._python_execution_command("import sys; print('failed'); sys.exit(3)")]
        )
        started = start_project_run(
            self.project,
            task="Reject a failed verification command",
            intent="build",
            mode="quick",
        )

        first = verify_project_run(self.project, run_id=str(started["run_id"]))
        second = verify_project_run(self.project, run_id=str(started["run_id"]))

        self.assertFalse(first["ok"])
        self.assertEqual(first["failed_command_ids"], ["focused-tests"])
        self.assertEqual(second["reused_execution_ids"], [])
        self.assertNotEqual(
            first["executions"][0]["execution_id"],
            second["executions"][0]["execution_id"],
        )

    def test_verify_redacts_secret_values_and_detects_output_tamper(self) -> None:
        self._configure_execution(
            [
                self._python_execution_command(
                    "import os; print(os.environ.get('ARIA_TEST_SECRET', 'SECRET_NOT_INHERITED'))"
                )
            ]
        )
        started = start_project_run(
            self.project,
            task="Redact secrets in trusted evidence",
            intent="build",
            mode="quick",
        )
        with patch.dict(os.environ, {"ARIA_TEST_SECRET": "super-secret-value"}):
            verified = verify_project_run(self.project, run_id=str(started["run_id"]))
        run_root = Path(started["manifest_path"]).parent
        manifest = json.loads(Path(started["manifest_path"]).read_text(encoding="utf-8"))
        receipts = validate_execution_bundle(self.project, run_root, manifest)
        execution_id = str(verified["executions"][0]["execution_id"])
        receipt = receipts[execution_id]
        output = run_root / str(receipt["output_path"])
        content = output.read_text(encoding="utf-8")
        self.assertNotIn("super-secret-value", content)
        self.assertIn("SECRET_NOT_INHERITED", content)

        output.write_text(content + "tampered", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "output SHA mismatch"):
            validate_execution_bundle(self.project, run_root, manifest)

    def test_verify_rejects_unsafe_adapter_executable_and_config_drift(self) -> None:
        self._configure_execution(
            [self._python_execution_command("print('stable')")]
        )
        started = start_project_run(
            self.project,
            task="Freeze verification configuration",
            intent="build",
            mode="quick",
        )
        verify_doc = yaml.safe_load((self.docs / "VERIFY.yaml").read_text(encoding="utf-8"))
        verify_doc["commands"][0]["argv"] = ["python", "-c", "print('changed')"]
        (self.docs / "VERIFY.yaml").write_text(
            yaml.safe_dump(verify_doc, sort_keys=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(WorkflowError, "input document changed"):
            verify_project_run(self.project, run_id=str(started["run_id"]))

        self._configure_execution(
            [
                {
                    "id": "unsafe",
                    "adapter": "python",
                    "argv": ["cmd", "/c", "echo unsafe"],
                    "cwd": ".",
                    "classes": ["focused"],
                }
            ]
        )
        with self.assertRaisesRegex(WorkflowError, "verification_contract"):
            start_project_run(
                self.project,
                task="Reject unsafe executable",
                intent="build",
                mode="quick",
            )

    def test_new_execution_contract_requires_verify_before_closure(self) -> None:
        self._configure_execution(
            [self._python_execution_command("print('FOCUSED verified result')")]
        )
        started = start_project_run(
            self.project,
            task="Require trusted verification before closure",
            intent="build",
            mode="quick",
        )
        self._change_service()
        test_row = self._test_output(started)
        result_path = self.runtime / "missing-evidence-bundle.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claims closure without aria verify",
                    "changed_files": ["backend/service.py"],
                    "tests": [test_row],
                    "read_back": "Manual output was read",
                    "review": "Manual review claimed",
                    "closure": "Manual closure claimed",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "Evidence Bundle"):
            close_project_run(
                self.project,
                run_id=str(started["run_id"]),
                result_path=result_path,
            )

    def test_product_git_change_after_verify_invalidates_bundle(self) -> None:
        proof = "FOCUSED verified result"
        self._configure_execution(
            [self._python_execution_command(f"print({proof!r})")]
        )
        started = start_project_run(
            self.project,
            task="Bind verification to exact product state",
            intent="build",
            mode="quick",
        )
        self._change_service()
        verified = verify_project_run(self.project, run_id=str(started["run_id"]))
        run_root = Path(str(started["manifest_path"])).parent
        manifest = json.loads(Path(str(started["manifest_path"])).read_text(encoding="utf-8"))
        receipts = validate_execution_bundle(self.project, run_root, manifest)
        execution_id = str(verified["executions"][0]["execution_id"])
        receipt = receipts[execution_id]
        (self.code / "backend" / "service.py").write_text(
            (self.code / "backend" / "service.py").read_text(encoding="utf-8")
            + "# changed after verify\n",
            encoding="utf-8",
        )
        result_path = self.runtime / "stale-evidence-bundle.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claims closure from stale evidence",
                    "changed_files": ["backend/service.py"],
                    "tests": [
                        {
                            "execution_id": execution_id,
                            "classes": ["focused"],
                            "status": "passed",
                            "command": receipt["command"],
                            "exit_code": receipt["exit_code"],
                            "output_path": receipt["output_path"],
                            "output_sha256": receipt["output_sha256"],
                            "output_excerpt": proof,
                            "actual_result": "Observed the focused behavior before later drift",
                            "class_evidence": {
                                "focused": {"proof_excerpts": [proof]}
                            },
                        }
                    ],
                    "read_back": "Receipt was read",
                    "review": "Review used stale output",
                    "closure": "Stale closure claimed",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "Git state changed"):
            close_project_run(
                self.project,
                run_id=str(started["run_id"]),
                result_path=result_path,
            )

    def test_concurrent_verify_serializes_and_reuses_one_execution(self) -> None:
        self._configure_execution(
            [
                self._python_execution_command(
                    "import time; time.sleep(0.5); print('FOCUSED serialized result')"
                )
            ]
        )
        started = start_project_run(
            self.project,
            task="Serialize concurrent verification",
            intent="build",
            mode="quick",
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    verify_project_run,
                    self.project,
                    run_id=str(started["run_id"]),
                )
                for _ in range(2)
            ]
            results = [future.result(timeout=60) for future in futures]
        execution_ids = {
            str(result["executions"][0]["execution_id"]) for result in results
        }
        self.assertEqual(len(execution_ids), 1)
        self.assertEqual(sum(bool(result["reused_execution_ids"]) for result in results), 1)
        index = json.loads(
            (
                Path(str(started["manifest_path"])).parent
                / "execution"
                / "index.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(len(index["executions"]), 1)

    def test_verify_timeout_is_fail_closed(self) -> None:
        command = self._python_execution_command(
            "import time; print('starting'); time.sleep(3)"
        )
        command["timeout_seconds"] = 1
        self._configure_execution([command])
        started = start_project_run(
            self.project,
            task="Stop a timed out verification",
            intent="build",
            mode="quick",
        )

        verified = verify_project_run(self.project, run_id=str(started["run_id"]))

        self.assertFalse(verified["ok"])
        self.assertEqual(verified["failed_command_ids"], ["focused-tests"])
        run_root = Path(str(started["manifest_path"])).parent
        receipt = next((run_root / "execution" / "receipts").glob("*.json"))
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], "timed_out")

    def test_verify_timeout_terminates_child_process_tree(self) -> None:
        marker = self.code / "child-survived.txt"
        child = (
            "import pathlib,time; time.sleep(2); "
            f"pathlib.Path({str(marker)!r}).write_text('survived')"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "time.sleep(10)"
        )
        command = self._python_execution_command(parent)
        command["timeout_seconds"] = 1
        self._configure_execution([command])
        started = start_project_run(
            self.project,
            task="Terminate the complete timed-out process tree",
            intent="build",
            mode="quick",
        )

        result = verify_project_run(
            self.project, run_id=str(started["run_id"])
        )

        self.assertFalse(result["ok"])
        time.sleep(3)
        self.assertFalse(marker.exists())

    def test_verify_success_also_terminates_background_children(self) -> None:
        marker = self.code / "successful-child-survived.txt"
        child = (
            "import pathlib,time; time.sleep(2); "
            f"pathlib.Path({str(marker)!r}).write_text('survived')"
        )
        parent = (
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "print('parent passed')"
        )
        self._configure_execution([self._python_execution_command(parent)])
        started = start_project_run(
            self.project,
            task="Terminate background children after successful verification",
            intent="build",
            mode="quick",
        )

        result = verify_project_run(
            self.project, run_id=str(started["run_id"])
        )

        self.assertTrue(result["ok"])
        time.sleep(3)
        self.assertFalse(marker.exists())

    def test_canary_is_isolated_and_active_cutover_is_engine_bound(self) -> None:
        state_before = (self.docs / "STATE.yaml").read_bytes()
        history_before = (self.docs / "HISTORY.jsonl").read_bytes()
        canary = run_project_canary(self.project)
        self.assertTrue(canary["ok"])
        self.assertTrue(canary["source_unchanged"])
        self.assertEqual((self.docs / "STATE.yaml").read_bytes(), state_before)
        self.assertEqual((self.docs / "HISTORY.jsonl").read_bytes(), history_before)

        cutover = activate_project(self.project)
        self.assertEqual(cutover["mode"], "active")
        active = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )
        self.assertEqual(active.mode, "active")
        self.assertEqual(active.activation_engine_sha256, cutover["engine_sha256"])
        self.assertTrue(run_project_doctor(active)["ok"])
        self.assertEqual(verify_history(active)["events"], 2)
        stale = replace(active, activation_engine_sha256="0" * 64)
        stale_doctor = run_project_doctor(stale)
        self.assertFalse(stale_doctor["ok"])
        self.assertFalse(
            next(
                row
                for row in stale_doctor["checks"]
                if row["id"] == "active_engine_integrity"
            )["ok"]
        )

    def test_active_project_canary_rebinds_to_changed_engine(self) -> None:
        activate_project(self.project)
        second_docs = Path(self.temporary.name) / "second-docs"
        second_code = Path(self.temporary.name) / "second-code"
        second_docs.mkdir()
        second_code.mkdir()
        (second_docs / "PROJECT.yaml").write_text(
            "schema_version: 1\nproject_id: second\n", encoding="utf-8"
        )
        register_project(
            "second",
            docs_root=second_docs,
            code_root=second_code,
            runtime_root=self.runtime,
        )
        current_sha = str(engine_state(self.framework)["sha256"])
        stale_sha = "0" * 64
        registry_text = self.registry.read_text(encoding="utf-8")
        self.registry.write_text(
            registry_text.replace(current_sha, stale_sha), encoding="utf-8"
        )
        stale = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )
        self.assertFalse(run_project_doctor(stale)["ok"])

        rebound = activate_project(stale)

        self.assertEqual(rebound["action"], "engine-rebind")
        self.assertEqual(rebound["engine_sha256"], current_sha)
        active = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )
        self.assertTrue(run_project_doctor(active)["ok"])
        projects = read_registry(self.registry)["projects"]
        self.assertEqual(set(projects), {"demo", "second"})
        self.assertEqual(projects["second"]["mode"], "shadow")
        events = history_events(active)
        self.assertEqual(events[-1]["type"], "aria_engine_rebind")
        self.assertEqual(events[-1]["result"]["previous_engine_sha256"], stale_sha)

    def test_active_rebind_refuses_registry_identity_race(self) -> None:
        activate_project(self.project)
        current_sha = str(engine_state(self.framework)["sha256"])
        stale_sha = "0" * 64
        raced_sha = "1" * 64
        self.registry.write_text(
            self.registry.read_text(encoding="utf-8").replace(
                current_sha, stale_sha
            ),
            encoding="utf-8",
        )
        stale = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )
        state_before = (self.docs / "STATE.yaml").read_bytes()
        history_before = (self.docs / "HISTORY.jsonl").read_bytes()
        from aria import project_activation

        real_canary = project_activation.run_project_canary

        def race_registry(project: object) -> dict[str, object]:
            report = real_canary(project)
            self.registry.write_text(
                self.registry.read_text(encoding="utf-8").replace(
                    stale_sha, raced_sha
                ),
                encoding="utf-8",
            )
            return report

        with patch(
            "aria.project_activation.run_project_canary",
            side_effect=race_registry,
        ):
            with self.assertRaisesRegex(
                WorkflowError, "registry entry changed before cutover"
            ):
                activate_project(stale)

        self.assertEqual((self.docs / "STATE.yaml").read_bytes(), state_before)
        self.assertEqual((self.docs / "HISTORY.jsonl").read_bytes(), history_before)
        self.assertIn(raced_sha, self.registry.read_text(encoding="utf-8"))

    def test_engine_rebind_recovers_every_crash_window(self) -> None:
        activate_project(self.project)
        current_sha = str(engine_state(self.framework)["sha256"])
        stale_sha = "0" * 64
        active_state = (self.docs / "STATE.yaml").read_bytes()
        active_history = (self.docs / "HISTORY.jsonl").read_bytes()
        active_registry = self.registry.read_bytes()
        stale_registry = active_registry.replace(
            current_sha.encode("ascii"), stale_sha.encode("ascii")
        )
        targets = {
            self.docs / "HISTORY.jsonl",
            self.docs / "STATE.yaml",
            self.registry,
        }
        from aria import project_activation

        real_write = project_activation.atomic_write_bytes
        for crash_after in (1, 2, 3):
            with self.subTest(crash_after=crash_after):
                (self.docs / "STATE.yaml").write_bytes(active_state)
                (self.docs / "HISTORY.jsonl").write_bytes(active_history)
                self.registry.write_bytes(stale_registry)
                stale = load_project(
                    "demo",
                    framework_root=self.framework,
                    runtime_root=self.runtime,
                    registry_path=self.registry,
                )
                writes = 0

                def crash_after_write(path: Path, payload: bytes) -> None:
                    nonlocal writes
                    real_write(path, payload)
                    if Path(path) in targets:
                        writes += 1
                        if writes == crash_after:
                            raise SystemExit(
                                f"simulated rebind death after write {crash_after}"
                            )

                with patch(
                    "aria.project_activation.atomic_write_bytes",
                    side_effect=crash_after_write,
                ):
                    with self.assertRaisesRegex(SystemExit, "rebind death"):
                        activate_project(stale)

                crashed = load_project(
                    "demo",
                    framework_root=self.framework,
                    runtime_root=self.runtime,
                    registry_path=self.registry,
                )
                recovered = activate_project(crashed)
                self.assertEqual(recovered["action"], "engine-rebind")
                active = load_project(
                    "demo",
                    framework_root=self.framework,
                    runtime_root=self.runtime,
                    registry_path=self.registry,
                )
                self.assertTrue(run_project_doctor(active)["ok"])
                events = history_events(active)
                self.assertEqual(len(events), 3)
                self.assertEqual(events[-1]["type"], "aria_engine_rebind")
                self.assertEqual(
                    events[-1]["result"]["previous_engine_sha256"], stale_sha
                )
                state = yaml.safe_load(
                    (self.docs / "STATE.yaml").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    state["history_checkpoint"],
                    {
                        "sequence": events[-1]["sequence"],
                        "event_sha256": events[-1]["event_sha256"],
                    },
                )

    def test_activation_recovers_every_crash_window(self) -> None:
        initial_state = (self.docs / "STATE.yaml").read_bytes()
        initial_history = (self.docs / "HISTORY.jsonl").read_bytes()
        initial_registry = self.registry.read_bytes()
        targets = {
            self.docs / "HISTORY.jsonl",
            self.docs / "STATE.yaml",
            self.registry,
        }
        from aria import project_activation

        real_write = project_activation.atomic_write_bytes
        for crash_after in (1, 2, 3):
            with self.subTest(crash_after=crash_after):
                (self.docs / "STATE.yaml").write_bytes(initial_state)
                (self.docs / "HISTORY.jsonl").write_bytes(initial_history)
                self.registry.write_bytes(initial_registry)
                shadow = load_project(
                    "demo",
                    framework_root=self.framework,
                    runtime_root=self.runtime,
                    registry_path=self.registry,
                )
                writes = 0

                def crash_after_write(path: Path, payload: bytes) -> None:
                    nonlocal writes
                    real_write(path, payload)
                    if Path(path) in targets:
                        writes += 1
                        if writes == crash_after:
                            raise SystemExit(
                                f"simulated activation death after write {crash_after}"
                            )

                with patch(
                    "aria.project_activation.atomic_write_bytes",
                    side_effect=crash_after_write,
                ):
                    with self.assertRaisesRegex(SystemExit, "activation death"):
                        activate_project(shadow)

                crashed = load_project(
                    "demo",
                    framework_root=self.framework,
                    runtime_root=self.runtime,
                    registry_path=self.registry,
                )
                recovered = activate_project(crashed)
                self.assertEqual(recovered["mode"], "active")
                active = load_project(
                    "demo",
                    framework_root=self.framework,
                    runtime_root=self.runtime,
                    registry_path=self.registry,
                )
                self.assertTrue(run_project_doctor(active)["ok"])
                self.assertEqual(verify_history(active)["events"], 2)
                state = yaml.safe_load(
                    (self.docs / "STATE.yaml").read_text(encoding="utf-8")
                )
                history = verify_history(active)
                self.assertEqual(
                    state["history_checkpoint"],
                    {
                        "sequence": history["events"],
                        "event_sha256": history["head_sha256"],
                    },
                )

    def test_activation_recovery_is_project_scoped_and_blocks_root_switch(
        self,
    ) -> None:
        from aria import project_activation

        real_write = project_activation.atomic_write_bytes
        crashed = False

        def crash_after_history(path: Path, payload: bytes) -> None:
            nonlocal crashed
            real_write(path, payload)
            if Path(path) == self.docs / "HISTORY.jsonl" and not crashed:
                crashed = True
                raise SystemExit("simulated activation death before registry write")

        with patch(
            "aria.project_activation.atomic_write_bytes",
            side_effect=crash_after_history,
        ):
            with self.assertRaisesRegex(SystemExit, "activation death"):
                activate_project(self.project)

        second_docs = Path(self.temporary.name) / "second-docs"
        second_code = Path(self.temporary.name) / "second-code"
        second_docs.mkdir()
        second_code.mkdir()
        (second_docs / "PROJECT.yaml").write_text(
            "schema_version: 1\nproject_id: second\n", encoding="utf-8"
        )
        register_project(
            "second",
            docs_root=second_docs,
            code_root=second_code,
            runtime_root=self.runtime,
        )

        replacement_docs = Path(self.temporary.name) / "replacement-docs"
        replacement_code = Path(self.temporary.name) / "replacement-code"
        replacement_docs.mkdir()
        replacement_code.mkdir()
        (replacement_docs / "PROJECT.yaml").write_text(
            "schema_version: 1\nproject_id: demo\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ConfigurationError, "recovery is pending"):
            register_project(
                "demo",
                docs_root=replacement_docs,
                code_root=replacement_code,
                runtime_root=self.runtime,
            )
        self.assertFalse((replacement_docs / "STATE.yaml").exists())
        self.assertEqual(
            set(read_registry(self.registry)["projects"]), {"demo", "second"}
        )

        current = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )
        activate_project(current)
        projects = read_registry(self.registry)["projects"]
        self.assertEqual(set(projects), {"demo", "second"})
        self.assertEqual(projects["demo"]["mode"], "active")
        self.assertEqual(projects["second"]["mode"], "shadow")
        self.assertEqual(projects["demo"]["docs_root"], self.docs.as_posix())
        active = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )
        self.assertTrue(run_project_doctor(active)["ok"])
        self.assertEqual(verify_history(active)["events"], 2)

    def test_register_and_activate_share_one_linearizable_registry_lock(self) -> None:
        second_docs = Path(self.temporary.name) / "second-docs"
        second_code = Path(self.temporary.name) / "second-code"
        second_docs.mkdir()
        second_code.mkdir()
        (second_docs / "PROJECT.yaml").write_text(
            "schema_version: 1\nproject_id: second\n", encoding="utf-8"
        )
        canary_done = Event()
        continue_cutover = Event()
        from aria import project_activation

        real_canary = project_activation.run_project_canary

        def pause_after_canary(project: object) -> dict[str, object]:
            report = real_canary(project)
            canary_done.set()
            if not continue_cutover.wait(timeout=30):
                raise AssertionError("activation test barrier timed out")
            return report

        with patch(
            "aria.project_activation.run_project_canary",
            side_effect=pause_after_canary,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                activation = pool.submit(activate_project, self.project)
                self.assertTrue(canary_done.wait(timeout=30))
                registration = pool.submit(
                    register_project,
                    "second",
                    docs_root=second_docs,
                    code_root=second_code,
                    runtime_root=self.runtime,
                )
                registration.result(timeout=30)
                continue_cutover.set()
                activation.result(timeout=60)
        projects = read_registry(self.registry)["projects"]
        self.assertEqual(projects["demo"]["mode"], "active")
        self.assertEqual(projects["second"]["mode"], "shadow")
        self.assertEqual(set(projects), {"demo", "second"})

    def _commit_run(
        self, started: dict[str, object], *paths: str, subject: str = "ARIA task"
    ) -> None:
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        for path in paths:
            self._git("add", "--", path)
        trailers = [
            f"ARIA-Task: {manifest['task_id']}",
            f"ARIA-Run: {manifest['run_id']}",
        ]
        if manifest.get("relevant_spec"):
            trailers.append(f"ARIA-Spec: {manifest['relevant_spec']}")
            spec = yaml.safe_load(
                (self.docs / str(manifest["relevant_spec"]))
                .read_text(encoding="utf-8")
                .split("---", 2)[1]
                if (self.docs / str(manifest["relevant_spec"]))
                .read_text(encoding="utf-8")
                .startswith("---")
                else "{}"
            )
            if isinstance(spec, dict):
                for adr in spec.get("adrs", spec.get("adr", [])):
                    trailers.append(f"ARIA-ADR: {adr}")
        message = subject + "\n\n" + "\n".join(trailers)
        self._git("commit", "-q", "-m", message)

    def _docs_sha(self) -> str:
        rows = []
        for path in sorted(item for item in self.docs.rglob("*") if item.is_file()):
            rows.append(
                (
                    path.relative_to(self.docs).as_posix(),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        return canonical_sha(rows)

    def _test_output(
        self, started: dict[str, object], text: str = "1 passed\n"
    ) -> dict[str, object]:
        run_root = Path(str(started["manifest_path"])).parent
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        classes = manifest["assurance_plan"]["required_execution_classes"] or [
            "focused"
        ]
        complex_classes = {
            "e2e",
            "adversarial",
            "concurrency",
            "load",
            "stress",
            "soak",
            "recovery",
            "chaos",
        }
        lines = text.rstrip("\n").splitlines()
        class_evidence: dict[str, object] = {}
        for test_class in classes:
            proofs = [f"PROOF {test_class}: observable result"]
            if test_class in complex_classes:
                proofs.append(f"PROOF {test_class}: linked side effect")
            lines.extend(proofs)
            row: dict[str, object] = {"proof_excerpts": proofs}
            if test_class in {"concurrency", "load", "stress", "soak"}:
                metrics = json.dumps(
                    {
                        "class": test_class,
                        "concurrency": 4,
                        "operations": 100,
                        "duration_seconds": 1.25,
                        "error_rate": 0,
                    },
                    separators=(",", ":"),
                )
                lines.append(metrics)
                row["metrics_excerpt"] = metrics
            class_evidence[test_class] = row
        rendered = "\n".join(lines) + "\n"
        output = run_root / "outputs" / "pytest.log"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        excerpt = lines[0]
        return {
            "classes": classes,
            "command": "pytest -q",
            "exit_code": 0,
            "output_path": "outputs/pytest.log",
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "output_excerpt": excerpt,
            "actual_result": f"Observed raw output: {excerpt}",
            "class_evidence": class_evidence,
            "scenario": {
                "initial_state": "Isolated test fixture is ready",
                "actions": ["execute canonical fixture command", "read raw output"],
                "expected_result": "Requested behavior passes",
                "forbidden_result": "Failure, duplicate effect or hidden exception",
                "side_effects": "Only isolated fixture state may change",
                "correlation": str(started["run_id"]),
                "parallelism_or_load": "Fixture parameters defined by the test",
                "actual_result": f"Observed {excerpt}",
            },
        }

    def _test_outputs(self, started: dict[str, object]) -> list[dict[str, object]]:
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        rows = [self._test_output(started)]
        assessments = manifest["assurance_plan"]["required_assessment_classes"]
        if assessments:
            rows.append(
                {
                    "classes": assessments,
                    "status": "not_applicable",
                    "rationale": (
                        "The isolated fixture has no migration, rollback, security or "
                        "long-duration production surface; applicability was reviewed."
                    ),
                }
            )
        self._verification_evidence[str(started["run_id"])] = rows
        return rows

    def _feature_contract_evidence(
        self,
        started: dict[str, object],
        *,
        verification: list[dict[str, object]],
        changed_files: list[str],
        spec_candidate: Path | None = None,
        lock_only: bool = False,
        project: object | None = None,
    ) -> dict[str, object]:
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        policy = manifest.get("feature_contract", {})
        if policy.get("required") is not True:
            return {}
        run_root = Path(str(started["manifest_path"])).parent
        contract_relative = str(policy["artifact"])
        contract_path = run_root / contract_relative
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract = {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "task_id": manifest["task_id"],
            "status": "ready",
            "outcome": "The requested behavior is implemented and verified",
            "ambiguities_resolved": True,
            "requirements": [
                {"id": "R-001", "statement": str(manifest["task"])}
            ],
            "acceptance": [
                {
                    "id": "AC-001",
                    "requirement_ids": ["R-001"],
                    "oracle": "The requested behavior passes project verification",
                }
            ],
            "clarifications": [],
            "plan": {
                "summary": "Implement the smallest complete project change",
                "steps": [
                    {
                        "id": "P-001",
                        "title": "Implement and verify the requested behavior",
                        "requirement_ids": ["R-001"],
                    }
                ],
            },
            "tasks": [
                {
                    "id": "T-001",
                    "title": "Implement and verify the requested behavior",
                    "requirement_ids": ["R-001"],
                    "plan_step_ids": ["P-001"],
                    "depends_on": [],
                }
            ],
        }
        if not contract_path.is_file():
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
        contract_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        if manifest.get("contract_phase") != "feature_contract_locked":
            lock_project_feature_contract(
                project or self.project, run_id=str(started["run_id"])
            )
            manifest = json.loads(
                Path(str(started["manifest_path"])).read_text(encoding="utf-8")
            )
        if spec_candidate is not None:
            text = spec_candidate.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) != 3:
                raise AssertionError("Test spec candidate requires YAML frontmatter")
            frontmatter = yaml.safe_load(parts[1])
            frontmatter["feature_contract_sha256"] = contract_sha
            spec_candidate.write_text(
                "---\n"
                + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
                + "---"
                + parts[2],
                encoding="utf-8",
            )
        evidence: dict[str, object] = {
            "feature_contract": {
                "path": contract_relative,
                "sha256": contract_sha,
            }
        }
        if lock_only:
            return evidence
        convergence_policy = policy.get("convergence", {})
        if convergence_policy.get("required") is True:
            convergence_relative = str(convergence_policy["artifact"])
            convergence_path = run_root / convergence_relative
            executed = [
                str(item)
                for item in verification[0].get("classes", [])
                if isinstance(item, str)
            ]
            proof = verification[0]["class_evidence"][executed[0]][
                "proof_excerpts"
            ][0]
            convergence = {
                "schema_version": 1,
                "run_id": manifest["run_id"],
                "feature_contract_sha256": contract_sha,
                "verdict": "converged",
                "tasks": [{"id": "T-001", "status": "completed"}],
                "requirements": [
                    {
                        "id": "R-001",
                        "status": "proven",
                        "task_ids": ["T-001"],
                        "implementation_paths": changed_files,
                        "acceptance_results": [
                            {
                                "id": "AC-001",
                                "status": "proven",
                                "oracle": "The requested behavior passes project verification",
                                "evidence_refs": [
                                    {
                                        "verification_index": 0,
                                        "classes": [executed[0]],
                                        "proof_excerpts": [proof],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            convergence_path.write_text(json.dumps(convergence), encoding="utf-8")
            evidence["convergence"] = {
                "path": convergence_relative,
                "sha256": hashlib.sha256(convergence_path.read_bytes()).hexdigest(),
            }
        return evidence

    def _lock_feature_contract(
        self, started: dict[str, object], *, project: object | None = None
    ) -> None:
        self._feature_contract_evidence(
            started,
            verification=[],
            changed_files=[],
            lock_only=True,
            project=project,
        )

    def _role_evidence(self, started: dict[str, object]) -> list[dict[str, object]]:
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        run_root = Path(str(started["manifest_path"])).parent
        target = role_target_identity(self.project, run_root, manifest)
        rows: list[dict[str, object]] = []
        for index, role in enumerate(manifest["role_contract"]["required_roles"]):
            agent_id = f"agent-{index + 1}-{role}"
            relative = f"outputs/roles/{role}.json"
            artifact = run_root / relative
            artifact.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "run_id": manifest["run_id"],
                "role": role,
                "agent_id": agent_id,
                "context_sha256": manifest["context"]["context_sha256"],
                "target_kind": target["kind"],
                "target_sha256": target["sha256"],
                "independent": True,
                "changed_code": False,
                "changed_documents": False,
                "git_operations": False,
                "verdict": "pass",
                "summary": f"Independent {role} completed",
                "findings": [],
            }
            if role == "functional_coverage_reviewer":
                functional = run_root / str(
                    manifest["functional_coverage_contract"]["artifact"]
                )
                payload.update(
                    {
                        "functional_coverage_sha256": (
                            hashlib.sha256(functional.read_bytes()).hexdigest()
                            if functional.is_file()
                            else None
                        ),
                        "functional_coverage_scope_sha256": manifest["context"][
                            "scope_sha256"
                        ],
                        "test_obligations_reviewed": True,
                        "verification_sha256": canonical_sha(
                            self._verification_evidence.get(
                                str(started["run_id"]), []
                            )
                        ),
                    }
                )
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            rows.append(
                {
                    "role": role,
                    "agent_id": agent_id,
                    "artifact_path": relative,
                    "artifact_sha256": hashlib.sha256(
                        artifact.read_bytes()
                    ).hexdigest(),
                }
            )
        return rows

    def _review_coverage(self, started: dict[str, object]) -> dict[str, str]:
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        run_root = Path(str(started["manifest_path"])).parent
        scope = json.loads(Path(str(started["scope_path"])).read_text(encoding="utf-8"))
        relative = "outputs/review-coverage.json"
        artifact = run_root / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": manifest["run_id"],
                    "scope_sha256": scope["sha256"],
                    "files": [
                        {"path": row["path"], "status": "reviewed"}
                        for row in scope["files"]
                    ],
                    "excluded": [
                        {
                            "path": row["path"],
                            "reason": row["reason"],
                            "disposition": "boundary-accepted",
                        }
                        for row in scope.get("excluded", [])
                    ],
                    "dimensions": [
                        {
                            "dimension": dimension,
                            "status": "reviewed",
                            "evidence": f"Reviewed {dimension} against the captured scope",
                        }
                        for dimension in manifest["assurance_plan"]["review_dimensions"]
                    ],
                }
            ),
            encoding="utf-8",
        )
        evidence = {
            "coverage_path": relative,
            "coverage_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
        functional_contract = manifest.get("functional_coverage_contract", {})
        if functional_contract.get("required") is True:
            functional_relative = str(functional_contract["artifact"])
            functional_artifact = run_root / functional_relative
            functional_artifact.write_text(
                "\n\n".join(
                    f"{heading}\nReviewed behavior and evidence for {heading[3:].lower()}."
                    for heading in functional_contract["headings"]
                )
                + "\n",
                encoding="utf-8",
            )
            evidence.update(
                {
                    "functional_coverage_path": functional_relative,
                    "functional_coverage_sha256": hashlib.sha256(
                        functional_artifact.read_bytes()
                    ).hexdigest(),
                    "functional_coverage_scope_sha256": scope["sha256"],
                }
            )
        return evidence

    def _approve_proposal(
        self,
        project: object,
        started: dict[str, object],
        proposal: dict[str, object],
    ) -> dict[str, object]:
        approval = self.runtime / f"approval-{started['run_id']}.json"
        approval.write_text(
            json.dumps(
                {
                    "decision": "approved",
                    "actor": "user",
                    "statement": "I reviewed and approve this exact proposal",
                    "proposal_sha256": proposal["proposal_sha256"],
                    "spec_sha256": proposal["spec_sha256"],
                }
            ),
            encoding="utf-8",
        )
        return approve_project_spec(
            project, run_id=str(started["run_id"]), approval_path=approval
        )

    def _change_service(self) -> None:
        path = self.code / "backend" / "service.py"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n# changed by test\n",
            encoding="utf-8",
        )

    def test_project_doctor_and_history_read_actual_roots(self) -> None:
        doctor = run_project_doctor(self.project)
        self.assertTrue(doctor["ok"], doctor)
        git_check = next(
            check for check in doctor["checks"] if check["id"] == "git_root_identity"
        )
        self.assertIn(str(self.code.resolve()), git_check["detail"])
        history = verify_history(self.project)
        self.assertTrue(history["ok"], history)
        self.assertEqual(history["events"], 1)

    def test_git_command_budget_is_configurable_and_bounded(self) -> None:
        from aria import project as project_module

        completed = subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=b"abc123\n", stderr=b""
        )
        with patch.dict(
            os.environ, {"ARIA_GIT_TIMEOUT_SECONDS": "123"}, clear=False
        ), patch("aria.project.subprocess.run", return_value=completed) as run:
            self.assertEqual(
                project_module._git_text(self.code, "rev-parse", "HEAD"), "abc123"
            )
            self.assertEqual(run.call_args.kwargs["timeout"], 123.0)
        with patch.dict(
            os.environ, {"ARIA_GIT_TIMEOUT_SECONDS": "0"}, clear=False
        ):
            with self.assertRaisesRegex(ConfigurationError, "between 1 and 1800"):
                project_module._git_text(self.code, "rev-parse", "HEAD")

    def test_five_routes_cover_simple_medium_and_deep_work(self) -> None:
        simple_design = decide_run_route(
            task="Спроектируй подпись кнопки без документа",
            intent="auto",
            mode="auto",
            changed_paths=[],
            risk_flags=[],
            spec_exists=False,
        )
        simple_build = decide_run_route(
            task="Исправь опечатку",
            intent="build",
            mode="auto",
            changed_paths=["frontend/view.ts"],
            risk_flags=[],
            spec_exists=False,
        )
        medium = decide_run_route(
            task="Измени backend и frontend согласованно",
            intent="build",
            mode="auto",
            changed_paths=["backend/service.py", "frontend/view.ts"],
            risk_flags=[],
            spec_exists=False,
        )
        deep_design = decide_run_route(
            task="Спроектируй миграцию схемы",
            intent="design",
            mode="auto",
            changed_paths=[],
            risk_flags=["migration"],
            spec_exists=False,
        )
        deep_build = decide_run_route(
            task="Реализуй миграцию схемы",
            intent="build",
            mode="auto",
            changed_paths=["backend/service.py"],
            risk_flags=["schema"],
            spec_exists=True,
        )
        self.assertEqual(
            [
                simple_design["intent"],
                simple_design["mode"],
                simple_build["mode"],
                medium["mode"],
                deep_design["mechanism"],
                deep_build["mechanism"],
            ],
            ["design", "quick", "quick", "standard", "spec", "next-task-new"],
        )
        text_only_medium = decide_run_route(
            task="Согласованно обнови API и UI",
            intent="auto",
            mode="auto",
            changed_paths=[],
            risk_flags=[],
            spec_exists=False,
        )
        self.assertEqual(text_only_medium["mode"], "standard")

    def test_quick_omits_spec_standard_uses_stack_spec_and_git(self) -> None:
        quick = start_project_run(
            self.project,
            task="Исправь подпись",
            intent="build",
            mode="quick",
            changed_paths=["frontend/view.ts"],
        )
        quick_manifest = json.loads(
            Path(quick["manifest_path"]).read_text(encoding="utf-8")
        )
        quick_context = Path(quick["context_path"]).read_text(encoding="utf-8")
        self.assertIsNone(quick_manifest["relevant_spec"])
        self.assertIn("# Working stack", quick_context)
        self.assertNotIn("# Demo spec", quick_context)

        standard = start_project_run(
            self.project,
            task="Реализуй demo spec",
            intent="build",
            mode="standard",
            changed_paths=["backend/service.py"],
            spec="specs/active/demo_task.md",
        )
        standard_manifest = json.loads(
            Path(standard["manifest_path"]).read_text(encoding="utf-8")
        )
        standard_context = Path(standard["context_path"]).read_text(encoding="utf-8")
        self.assertEqual(
            standard_manifest["relevant_spec"], "specs/active/demo_task.md"
        )
        self.assertIsNotNone(standard_manifest["context"]["git"])
        self.assertIn("# Demo spec", standard_context)
        self.assertIn(
            "backend/pyproject.toml",
            {row["path"] for row in standard_manifest["context"]["stack_sources"]},
        )

    def test_deep_design_and_build_keep_named_mechanisms(self) -> None:
        design = start_project_run(
            self.project,
            task="Спроектируй новую архитектуру",
            intent="design",
            mode="deep",
        )
        build = start_project_run(
            self.project,
            task="Реализуй утверждённую архитектуру",
            intent="build",
            mode="deep",
            spec="specs/active/demo_task.md",
        )
        self.assertEqual(design["route"]["mechanism"], "spec")
        self.assertEqual(build["route"]["mechanism"], "next-task-new")

    def test_shadow_close_reads_result_back_without_project_writes(self) -> None:
        before = self._docs_sha()
        started = start_project_run(
            self.project,
            task="Исправь service",
            intent="build",
            mode="quick",
            changed_paths=["backend/service.py"],
        )
        result_path = self.runtime / "result.json"
        self._change_service()
        self._commit_run(started, "backend/service.py")
        test_output = self._test_output(started)
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Service fixed",
                    "changed_files": ["backend/service.py"],
                    "tests": [test_output],
                    "read_back": "Function returns the expected value",
                    "review": "Final diff contains only the intended line",
                    "closure": "Task requirements met",
                }
            ),
            encoding="utf-8",
        )
        closed = close_project_run(
            self.project, run_id=started["run_id"], result_path=result_path
        )
        self.assertEqual(closed["status"], "completed")
        self.assertEqual(closed["project_writes"], [])
        self.assertEqual(before, self._docs_sha())
        stored = json.loads(Path(closed["result_path"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["tests"][0]["exit_code"], 0)
        raw_output = (
            Path(started["manifest_path"]).parent / "outputs" / "pytest.log"
        ).read_text(encoding="utf-8")
        self.assertTrue(raw_output.startswith("1 passed\n"))
        self.assertIn("PROOF focused: observable result", raw_output)
        verified = verify_completed_project_run(self.project, str(started["run_id"]))
        self.assertEqual(verified["result_sha256"], closed["result_sha256"])

    def test_standard_build_requires_feature_contract_and_convergence(self) -> None:
        started = start_project_run(
            self.project,
            task="Update backend and integration workflow",
            intent="build",
            mode="standard",
        )
        self._lock_feature_contract(started)
        self._change_service()
        tests = self._test_outputs(started)
        feature = self._feature_contract_evidence(
            started,
            verification=tests,
            changed_files=["backend/service.py"],
        )
        result_path = self.runtime / "standard-feature-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Standard feature completed",
                    "changed_files": ["backend/service.py"],
                    "tests": tests,
                    "read_back": "Updated behavior and raw output were read back",
                    "review": "Independent review passed",
                    "closure": "Requirement R-001 converged",
                    **feature,
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )
        closed = close_project_run(
            self.project, run_id=str(started["run_id"]), result_path=result_path
        )
        stored = json.loads(Path(closed["result_path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            stored["feature_contract_evidence"]["requirement_ids"], ["R-001"]
        )
        self.assertEqual(stored["convergence_evidence"]["verdict"], "converged")

    def test_standard_build_converges_only_with_linked_execution_receipt(self) -> None:
        classes = ["focused", "integration", "e2e", "adversarial"]
        proof_lines = [
            "FOCUSED result is observable",
            "INTEGRATION neighbor read-back is observable",
            "E2E linked workflow result is observable",
            "E2E linked side effect is observable",
            "ADVERSARIAL forbidden result is absent",
            "ADVERSARIAL failure boundary is observable",
        ]
        self._configure_execution(
            [
                {
                    **self._python_execution_command(
                        ";".join(f"print({line!r})" for line in proof_lines),
                        command_id="feature-verification",
                    ),
                    "classes": classes,
                }
            ]
        )
        started = start_project_run(
            self.project,
            task="Update backend and integration workflow",
            intent="build",
            mode="standard",
        )
        self._lock_feature_contract(started)
        self._change_service()
        links = self.runtime / "verify-links.json"
        links.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "commands": {
                        "feature-verification": {
                            "requirement_ids": ["R-001"],
                            "acceptance_ids": ["AC-001"],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        verified = verify_project_run(
            self.project,
            run_id=str(started["run_id"]),
            links_path=links,
        )
        self.assertTrue(verified["ok"])
        run_root = Path(str(started["manifest_path"])).parent
        manifest = json.loads(Path(str(started["manifest_path"])).read_text(encoding="utf-8"))
        receipts = validate_execution_bundle(self.project, run_root, manifest)
        execution_id = str(verified["executions"][0]["execution_id"])
        receipt = receipts[execution_id]
        class_evidence = {
            "focused": {"proof_excerpts": [proof_lines[0]]},
            "integration": {"proof_excerpts": [proof_lines[1]]},
            "e2e": {"proof_excerpts": proof_lines[2:4]},
            "adversarial": {"proof_excerpts": proof_lines[4:6]},
        }
        test_row = {
            "execution_id": execution_id,
            "classes": classes,
            "status": "passed",
            "command": receipt["command"],
            "exit_code": receipt["exit_code"],
            "output_path": receipt["output_path"],
            "output_sha256": receipt["output_sha256"],
            "output_excerpt": proof_lines[0],
            "actual_result": "Verified the linked feature workflow and negative boundary",
            "class_evidence": class_evidence,
            "scenario": {
                "initial_state": "Feature fixture is ready",
                "actions": ["execute feature verification", "read linked output"],
                "expected_result": "Feature and neighbor pass",
                "forbidden_result": "Failure boundary is crossed",
                "side_effects": "Only the fixture state changes",
                "correlation": str(started["run_id"]),
                "parallelism_or_load": "Single deterministic feature scenario",
                "actual_result": "All linked and adversarial proofs were observed",
            },
        }
        feature = self._feature_contract_evidence(
            started,
            verification=[test_row],
            changed_files=["backend/service.py"],
        )
        result_path = self.runtime / "trusted-execution-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Trusted execution feature completed",
                    "changed_files": ["backend/service.py"],
                    "tests": [test_row],
                    "read_back": "Receipt, raw output and product behavior were read back",
                    "review": "Independent review passed",
                    "closure": "Requirement R-001 converged from trusted evidence",
                    **feature,
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )

        closed = close_project_run(
            self.project, run_id=str(started["run_id"]), result_path=result_path
        )

        stored = json.loads(Path(closed["result_path"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["tests"][0]["execution_id"], execution_id)
        self.assertEqual(stored["tests"][0]["requirement_ids"], ["R-001"])
        self.assertEqual(stored["tests"][0]["acceptance_ids"], ["AC-001"])

    def test_standard_build_rejects_missing_feature_contract(self) -> None:
        started = start_project_run(
            self.project,
            task="Update backend and integration workflow",
            intent="build",
            mode="standard",
        )
        self._change_service()
        tests = self._test_outputs(started)
        result_path = self.runtime / "missing-feature-contract.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claims completion without a feature contract",
                    "changed_files": ["backend/service.py"],
                    "tests": tests,
                    "read_back": "Output read",
                    "review": "Review claimed",
                    "closure": "Closure claimed",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "Feature Contract declaration"):
            close_project_run(
                self.project,
                run_id=str(started["run_id"]),
                result_path=result_path,
            )

    def test_feature_contract_lock_rejects_post_implementation_contract(self) -> None:
        started = start_project_run(
            self.project,
            task="Change backend with a frozen contract",
            intent="build",
            mode="standard",
        )
        self._change_service()
        with self.assertRaisesRegex(WorkflowError, "Git baseline already changed"):
            self._feature_contract_evidence(
                started,
                verification=[],
                changed_files=[],
                lock_only=True,
            )

    def test_feature_contract_lock_rejects_precontract_runtime_outputs(self) -> None:
        started = start_project_run(
            self.project,
            task="Design a frozen contract before the proposal",
            intent="design",
            mode="deep",
        )
        run_root = Path(str(started["manifest_path"])).parent
        candidate = run_root / "spec.md"
        candidate.write_text("proposal created too early", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "unexpected=.*spec.md"):
            self._feature_contract_evidence(
                started,
                verification=[],
                changed_files=[],
                lock_only=True,
            )

    def test_feature_contract_manifest_rehash_cannot_forge_lock_receipt(self) -> None:
        from aria import simple_run

        started = start_project_run(
            self.project,
            task="Build from a separately anchored contract",
            intent="build",
            mode="standard",
        )
        self._lock_feature_contract(started)
        manifest_path = Path(str(started["manifest_path"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["feature_contract_lock"]["locked_at"] = "forged"
        manifest["contract_sha256"] = canonical_sha(
            simple_run._run_contract_payload(manifest)
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(WorkflowError, "receipt does not match"):
            simple_run._validate_run_identity(self.project, manifest)

    def test_run_contract_payload_preserves_unfinished_v1_shape(self) -> None:
        from aria import simple_run

        started = start_project_run(
            self.project,
            task="Inspect a legacy unfinished run",
            intent="review",
            mode="quick",
            target_type="component",
            target="backend",
        )
        manifest_path = Path(str(started["manifest_path"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in (
            "feature_contract",
            "contract_phase",
            "feature_contract_lock",
            "amendment_history",
            "managed_lifecycle",
        ):
            manifest.pop(key, None)
        legacy_keys = (
            "schema_version",
            "run_id",
            "created_at",
            "project",
            "project_mode",
            "task",
            "task_id",
            "task_selection",
            "state_profile",
            "route",
            "roots",
            "engine",
            "context",
            "context_path",
            "scope_path",
            "review_request",
            "relevant_spec",
            "design_assessment",
            "role_contract",
            "functional_coverage_contract",
            "assurance_plan",
            "system_map",
            "closure_contract",
        )
        manifest["contract_sha256"] = canonical_sha(
            {key: manifest.get(key) for key in legacy_keys}
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (manifest_path.parent / "contract-anchor.json").unlink()

        self.assertIsNone(simple_run._validate_run_identity(self.project, manifest))

    def test_start_anchor_rejects_optional_policy_downgrade(self) -> None:
        from aria import simple_run

        self._configure_execution(
            [self._python_execution_command("print('FOCUSED anchored result')")]
        )
        started = start_project_run(
            self.project,
            task="Reject removal of trusted policy keys",
            intent="build",
            mode="standard",
        )
        manifest_path = Path(str(started["manifest_path"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in (
            "feature_contract",
            "contract_phase",
            "feature_contract_lock",
            "managed_lifecycle",
            "execution_contract",
        ):
            manifest.pop(key, None)
        manifest["contract_sha256"] = canonical_sha(
            simple_run._run_contract_payload(manifest)
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            WorkflowError, "Feature Contract policy was removed"
        ):
            simple_run._validate_run_identity(self.project, manifest)

    def test_legacy_start_anchor_rejects_feature_policy_downgrade(self) -> None:
        from aria import simple_run

        started = start_project_run(
            self.project,
            task="Reject downgrade against an ARIA 1.2-style anchor",
            intent="build",
            mode="standard",
        )
        manifest_path = Path(str(started["manifest_path"]))
        anchor_path = manifest_path.parent / "contract-anchor.json"
        anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        anchor.pop("feature_contract_present", None)
        anchor.pop("execution_contract_present", None)
        anchor.pop("execution_contract_sha256", None)
        anchor_path.write_text(json.dumps(anchor), encoding="utf-8")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in (
            "feature_contract",
            "contract_phase",
            "feature_contract_lock",
            "managed_lifecycle",
        ):
            manifest.pop(key, None)
        manifest["contract_sha256"] = canonical_sha(
            simple_run._run_contract_payload(manifest)
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(
            WorkflowError, "Feature Contract policy was removed"
        ):
            simple_run._validate_run_identity(self.project, manifest)

    def test_active_close_updates_compact_state_and_hash_chained_history(self) -> None:
        active = self._active()
        started = start_project_run(
            active,
            task="Исправь service",
            intent="build",
            mode="quick",
            changed_paths=["backend/service.py"],
        )
        result_path = self.runtime / "active-result.json"
        self._change_service()
        self._commit_run(started, "backend/service.py")
        test_output = self._test_output(started)
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Service fixed",
                    "changed_files": ["backend/service.py"],
                    "tests": [test_output],
                    "read_back": "Function returns the expected value",
                    "review": "Diff reviewed",
                    "closure": "Requirements met",
                }
            ),
            encoding="utf-8",
        )
        closed = close_project_run(
            active, run_id=started["run_id"], result_path=result_path
        )
        self.assertEqual(len(closed["project_writes"]), 2)
        self.assertEqual(
            {row["path"] for row in closed["project_writes"]},
            {"STATE.yaml", "HISTORY.jsonl"},
        )
        state = (self.docs / "STATE.yaml").read_text(encoding="utf-8")
        self.assertIn("stage: completed", state)
        self.assertLess(len(state.encode("utf-8")), 32768)
        history = verify_history(active)
        self.assertTrue(history["ok"], history)
        self.assertEqual(history["events"], 2)

    def test_parallel_run_start_load_has_unique_readable_contracts(self) -> None:
        def start(index: int) -> dict[str, object]:
            return start_project_run(
                self.project,
                task=f"Read-only load probe {index}",
                intent="design",
                mode="quick",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            started = list(executor.map(start, range(24)))
        self.assertEqual(len({str(row["run_id"]) for row in started}), 24)
        for row in started:
            manifest_path = Path(str(row["manifest_path"]))
            context_path = Path(str(row["context_path"]))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_id"], row["run_id"])
            self.assertEqual(
                hashlib.sha256(context_path.read_bytes()).hexdigest(),
                manifest["context"]["context_sha256"],
            )

    def test_concurrent_active_close_appends_exactly_one_history_event(self) -> None:
        active = self._active()
        started = start_project_run(
            active,
            task="Исправь service",
            intent="build",
            mode="quick",
            changed_paths=["backend/service.py"],
        )
        result_path = self.runtime / "concurrent-result.json"
        self._change_service()
        self._commit_run(started, "backend/service.py")
        test_output = self._test_output(started)
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Service fixed",
                    "changed_files": ["backend/service.py"],
                    "tests": [test_output],
                    "read_back": "Expected value observed",
                    "review": "Diff reviewed",
                    "closure": "Requirements met",
                }
            ),
            encoding="utf-8",
        )

        def close() -> str:
            try:
                close_project_run(
                    active, run_id=started["run_id"], result_path=result_path
                )
                return "closed"
            except WorkflowError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: close(), range(2)))
        self.assertEqual(outcomes.count("closed"), 1, outcomes)
        self.assertEqual(
            sum("already closed" in outcome for outcome in outcomes), 1, outcomes
        )
        history = verify_history(active)
        self.assertTrue(history["ok"], history)
        self.assertEqual(history["events"], 2)

    def test_deep_build_rejects_non_spec_project_document(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "under specs"):
            start_project_run(
                self.project,
                task="Реализуй сложное изменение",
                intent="build",
                mode="deep",
                spec="STACK.md",
            )

    def test_unrelated_task_does_not_inherit_frontier_spec(self) -> None:
        started = start_project_run(
            self.project,
            task="Исправь несвязанную опечатку",
            intent="build",
            mode="auto",
            changed_paths=["frontend/view.ts"],
        )
        self.assertEqual(started["route"]["mode"], "quick")
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertIsNone(manifest["relevant_spec"])

    def test_shadow_run_cannot_be_closed_after_mode_flip(self) -> None:
        started = start_project_run(
            self.project,
            task="Спроектируй простое решение",
            intent="design",
            mode="quick",
        )
        result_path = self.runtime / "mode-flip.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Design ready",
                    "deliverable": "Use the existing component",
                    "read_back": "Design reread",
                    "review": "Design reviewed",
                    "closure": "Requirements met",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "mode changed"):
            close_project_run(
                self._active(),
                run_id=started["run_id"],
                result_path=result_path,
            )
        self.assertEqual(verify_history(self.project)["events"], 1)

    def test_review_close_rejects_scope_drift(self) -> None:
        started = start_project_run(
            self.project,
            task="Проверь backend",
            intent="review",
            mode="standard",
            target_type="component",
            target="backend",
        )
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        (self.code / "backend" / "service.py").write_text(
            "def value():\n    return 2\n", encoding="utf-8"
        )
        result_path = self.runtime / "stale-review.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Review completed",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "role_evidence": self._role_evidence(started),
                    "read_back": "Scope reread",
                    "review": "Findings reviewed",
                    "closure": "No findings",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "scope drifted"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_completed_build_rejects_unproven_change_and_fake_output(self) -> None:
        started = start_project_run(
            self.project,
            task="Исправь service",
            intent="build",
            mode="quick",
            changed_paths=["backend/service.py"],
        )
        result_path = self.runtime / "false-build.json"
        self._change_service()
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claimed fix",
                    "changed_files": ["backend/missing.py"],
                    "tests": [
                        {
                            "command": "never-ran",
                            "exit_code": 0,
                            "output_path": "outputs/missing.log",
                            "output_sha256": "0" * 64,
                        }
                    ],
                    "read_back": "Claimed read-back",
                    "review": "Claimed review",
                    "closure": "Claimed closure",
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "exactly match Git read-back"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_completed_build_rejects_missing_test_output(self) -> None:
        started = start_project_run(
            self.project,
            task="Change service",
            intent="build",
            mode="quick",
            changed_paths=["backend/service.py"],
        )
        self._change_service()
        result_path = self.runtime / "missing-output.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claimed fix",
                    "changed_files": ["backend/service.py"],
                    "tests": [
                        {
                            "command": "never-ran",
                            "exit_code": 0,
                            "output_path": "outputs/missing.log",
                            "output_sha256": "0" * 64,
                        }
                    ],
                    "read_back": "Claimed read-back",
                    "review": "Claimed review",
                    "closure": "Claimed closure",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "does not exist"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_completed_build_rejects_no_change_reason_when_git_changed(self) -> None:
        started = start_project_run(
            self.project,
            task="Change service",
            intent="build",
            mode="quick",
            changed_paths=["backend/service.py"],
        )
        self._change_service()
        result_path = self.runtime / "false-no-change.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claimed no-op",
                    "changed_files": [],
                    "no_change_reason": "Already correct",
                    "tests": [self._test_output(started)],
                    "read_back": "Claimed read-back",
                    "review": "Claimed review",
                    "closure": "Claimed closure",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "cannot use no_change_reason"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_completed_build_requires_complete_git_delta(self) -> None:
        started = start_project_run(
            self.project,
            task="Change backend and frontend",
            intent="build",
            mode="standard",
            changed_paths=["backend/service.py", "frontend/view.ts"],
            spec="specs/active/demo_task.md",
        )
        self._lock_feature_contract(started)
        self._change_service()
        view = self.code / "frontend" / "view.ts"
        view.write_text(
            view.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8"
        )
        result_path = self.runtime / "partial-diff.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claimed partial result",
                    "changed_files": ["backend/service.py"],
                    "tests": [self._test_output(started)],
                    "read_back": "Both layers read",
                    "review": "Claimed review",
                    "closure": "Claimed closure",
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "exactly match Git read-back"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_completed_deep_build_requires_traced_commit(self) -> None:
        started = start_project_run(
            self.project,
            task="Implement the approved architecture",
            intent="build",
            mode="deep",
            changed_paths=["backend/service.py"],
            spec="specs/active/demo_task.md",
        )
        self._lock_feature_contract(started)
        self._change_service()
        result_path = self.runtime / "uncommitted-deep-build.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Implementation changed but was not committed",
                    "changed_files": ["backend/service.py"],
                    "tests": [self._test_output(started)],
                    "read_back": "Changed behavior reread",
                    "review": "Final diff reviewed",
                    "closure": "Requirements appear complete",
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            WorkflowError, "deep build requires all task changes"
        ):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_completed_deep_build_records_commit_range_and_trailers(self) -> None:
        started = start_project_run(
            self.project,
            task="Implement the approved architecture",
            intent="build",
            mode="deep",
            changed_paths=["backend/service.py"],
            spec="specs/active/demo_task.md",
        )
        self._lock_feature_contract(started)
        self._change_service()
        self._commit_run(
            started, "backend/service.py", subject="Implement architecture"
        )
        tests = [self._test_output(started)]
        feature = self._feature_contract_evidence(
            started,
            verification=tests,
            changed_files=["backend/service.py"],
        )
        result_path = self.runtime / "committed-deep-build.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Approved architecture implemented",
                    "changed_files": ["backend/service.py"],
                    "tests": tests,
                    "read_back": "Changed behavior reread",
                    "review": "Final commit and wiring reviewed",
                    "closure": "Requirements are complete",
                    **feature,
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )

        closed = close_project_run(
            self.project, run_id=started["run_id"], result_path=result_path
        )

        stored = json.loads(Path(closed["result_path"]).read_text(encoding="utf-8"))
        git_trace = stored["trace_evidence"]["git"]
        self.assertTrue(git_trace["commit_required"])
        self.assertNotEqual(git_trace["base_commit"], git_trace["head_commit"])
        self.assertEqual(len(git_trace["commits"]), 1)
        self.assertEqual(
            git_trace["commits"][0]["trailers"]["task"],
            [stored["trace_evidence"]["task_id"]],
        )

    def test_independent_role_is_bound_to_final_build_subject(self) -> None:
        started = start_project_run(
            self.project,
            task="Change backend service",
            intent="build",
            mode="standard",
            changed_paths=["backend/service.py"],
        )
        self._lock_feature_contract(started)
        self._change_service()
        tests = self._test_outputs(started)
        roles = self._role_evidence(started)
        service = self.code / "backend" / "service.py"
        service.write_text(
            service.read_text(encoding="utf-8") + "# changed after review\n",
            encoding="utf-8",
        )
        feature = self._feature_contract_evidence(
            started,
            verification=tests,
            changed_files=["backend/service.py"],
        )
        result_path = self.runtime / "stale-role-target.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Service changed",
                    "changed_files": ["backend/service.py"],
                    "tests": tests,
                    "read_back": "Final file reread",
                    "review": "Earlier role artifact supplied",
                    "closure": "Claimed complete",
                    **feature,
                    "role_evidence": roles,
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "target_sha256 mismatch"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_close_rejects_mutation_after_final_role_validation(self) -> None:
        active = self._active()
        started = start_project_run(
            active,
            task="Change backend service",
            intent="build",
            mode="standard",
            changed_paths=["backend/service.py"],
        )
        self._change_service()
        tests = self._test_outputs(started)
        roles = self._role_evidence(started)
        result_path = self.runtime / "post-validation-tamper.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Service changed",
                    "changed_files": ["backend/service.py"],
                    "tests": tests,
                    "read_back": "Final file and output reread",
                    "review": "Independent role supplied",
                    "closure": "Claimed complete",
                    "role_evidence": roles,
                }
            ),
            encoding="utf-8",
        )
        state_before = (self.docs / "STATE.yaml").read_bytes()
        history_before = (self.docs / "HISTORY.jsonl").read_bytes()
        from aria import simple_run

        real_validate = simple_run._validate_role_evidence
        mutated = False

        def mutate_after_validation(*args: object, **kwargs: object) -> object:
            nonlocal mutated
            rows = real_validate(*args, **kwargs)
            if not mutated:
                mutated = True
                service = self.code / "backend" / "service.py"
                service.write_text(
                    service.read_text(encoding="utf-8") + "# TOCTOU mutation\n",
                    encoding="utf-8",
                )
                output = Path(started["manifest_path"]).parent / "outputs" / "pytest.log"
                output.write_text(
                    output.read_text(encoding="utf-8") + "tampered output\n",
                    encoding="utf-8",
                )
                role_path = Path(started["manifest_path"]).parent / str(
                    roles[0]["artifact_path"]
                )
                role_path.write_text(
                    role_path.read_text(encoding="utf-8") + " ", encoding="utf-8"
                )
            return rows

        with patch(
            "aria.simple_run._validate_role_evidence",
            side_effect=mutate_after_validation,
        ):
            with self.assertRaises(WorkflowError):
                close_project_run(
                    active, run_id=started["run_id"], result_path=result_path
                )
        self.assertEqual((self.docs / "STATE.yaml").read_bytes(), state_before)
        self.assertEqual((self.docs / "HISTORY.jsonl").read_bytes(), history_before)

    def test_actual_shared_primitive_delta_expands_closure_assurance(self) -> None:
        system_map = yaml.safe_load(
            (self.docs / "SYSTEM_MAP.yaml").read_text(encoding="utf-8")
        )
        system_map["shared_primitives"] = [
            {
                "id": "service-contract",
                "paths": ["backend/service.py"],
                "consumers": ["backend", "frontend"],
                "invalidation": "Retest backend and frontend contracts",
            }
        ]
        (self.docs / "SYSTEM_MAP.yaml").write_text(
            yaml.safe_dump(system_map, sort_keys=False), encoding="utf-8"
        )
        started = start_project_run(
            self.project,
            task="Small local correction",
            intent="build",
            mode="quick",
            changed_paths=[],
        )
        self._change_service()
        result_path = self.runtime / "under-tested-shared-delta.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Shared contract changed",
                    "changed_files": ["backend/service.py"],
                    "tests": [self._test_output(started)],
                    "read_back": "Changed file reread",
                    "review": "Local diff reviewed",
                    "closure": "Claimed complete",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "Missing required executed"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_close_rejects_context_artifact_tampering(self) -> None:
        started = start_project_run(
            self.project,
            task="Design a small change",
            intent="design",
            mode="quick",
        )
        context_path = Path(str(started["context_path"]))
        context_path.write_text("tampered\n", encoding="utf-8")
        result_path = self.runtime / "tampered-context.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Cannot continue",
                    "read_back": "Context changed",
                    "review": "Run inspected",
                    "closure": "Start a new run",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "context artifact changed"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_blocked_close_records_relevant_spec_drift_without_project_write(
        self,
    ) -> None:
        started = start_project_run(
            self.project,
            task="Continue demo spec",
            intent="build",
            mode="standard",
            spec="specs/active/demo_task.md",
        )
        spec_path = self.docs / "specs" / "active" / "demo_task.md"
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8") + "\n- changed\n", encoding="utf-8"
        )
        result_path = self.runtime / "stale-spec.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Spec drifted",
                    "read_back": "Drift observed",
                    "review": "Run inspected",
                    "closure": "Start a new run",
                }
            ),
            encoding="utf-8",
        )
        closed = close_project_run(
            self.project, run_id=started["run_id"], result_path=result_path
        )
        self.assertEqual(closed["status"], "blocked")
        self.assertEqual(closed["project_writes"], [])
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertIn("input document changed", manifest["context_stale_reason"])

    def test_blocked_close_can_terminally_record_engine_drift(self) -> None:
        started = start_project_run(
            self.project,
            task="Design a small change",
            intent="design",
            mode="quick",
        )
        result_path = self.runtime / "engine-drift.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "Engine changed",
                    "read_back": "The run context is stale",
                    "review": "No product result claimed",
                    "closure": "Start a new run",
                }
            ),
            encoding="utf-8",
        )
        drifted_engine = dict(engine_state(self.framework))
        drifted_engine["sha256"] = "f" * 64
        with patch("aria.simple_run.engine_state", return_value=drifted_engine):
            closed = close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )
        self.assertEqual(closed["status"], "blocked")
        self.assertIn("engine changed", closed["note"].lower())

    def test_completed_review_rejects_malformed_finding(self) -> None:
        started = start_project_run(
            self.project,
            task="Review backend",
            intent="review",
            mode="standard",
            target_type="component",
            target="backend",
        )
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        result_path = self.runtime / "malformed-finding.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Review completed",
                    "findings": [42],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "role_evidence": self._role_evidence(started),
                    "read_back": "Scope reread",
                    "review": "Findings reviewed",
                    "closure": "Review complete",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "must be an object"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_completed_review_requires_full_coverage_and_runtime_evidence(self) -> None:
        started = start_project_run(
            self.project,
            task="Review backend integration",
            intent="review",
            mode="standard",
            target_type="component",
            target="backend",
        )
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        coverage = self._review_coverage(started)
        verification = self._test_outputs(started)
        roles = self._role_evidence(started)
        result_path = self.runtime / "complete-review.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Backend review completed",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "role_evidence": roles,
                    "verification": verification,
                    **coverage,
                    "read_back": "Every captured file and raw test output was reread",
                    "review": "Independent review completed without findings",
                    "closure": "Scope, dimensions and runtime evidence are complete",
                }
            ),
            encoding="utf-8",
        )
        closed = close_project_run(
            self.project, run_id=started["run_id"], result_path=result_path
        )
        self.assertEqual(closed["status"], "completed")
        stored = json.loads(Path(closed["result_path"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["coverage"]["scope_files"], 2)
        self.assertEqual(
            manifest["role_contract"]["required_roles"],
            ["functional_coverage_reviewer", "independent_reviewer"],
        )
        self.assertEqual(
            stored["functional_coverage"]["scope_sha256"],
            manifest["context"]["scope_sha256"],
        )
        self.assertIsNone(stored["system_map_evidence"])
        self.assertEqual(
            set(stored["verification"][0]["classes"]),
            set(manifest["assurance_plan"]["required_execution_classes"]),
        )

    def test_component_review_rejects_incomplete_functional_coverage(self) -> None:
        started = start_project_run(
            self.project,
            task="Review backend functional behavior",
            intent="review",
            mode="standard",
            target_type="component",
            target="backend",
        )
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        coverage = self._review_coverage(started)
        run_root = Path(str(started["manifest_path"])).parent
        functional = run_root / str(coverage["functional_coverage_path"])
        text = functional.read_text(encoding="utf-8")
        missing = manifest["functional_coverage_contract"]["headings"][2]
        functional.write_text(text.replace(missing, "## Missing binding section"), encoding="utf-8")
        coverage["functional_coverage_sha256"] = hashlib.sha256(
            functional.read_bytes()
        ).hexdigest()
        result_path = self.runtime / "incomplete-functional-review.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claimed complete component review",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "role_evidence": self._role_evidence(started),
                    "verification": self._test_outputs(started),
                    **coverage,
                    "read_back": "Captured files and outputs were reread",
                    "review": "Functional coverage was independently challenged",
                    "closure": "Claimed complete",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            WorkflowError, "requires exactly one heading"
        ):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_functional_reviewer_is_bound_to_map_and_test_obligations(self) -> None:
        started = start_project_run(
            self.project,
            task="Review backend functional behavior",
            intent="review",
            mode="standard",
            target_type="component",
            target="backend",
        )
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        coverage = self._review_coverage(started)
        verification = self._test_outputs(started)
        roles = self._role_evidence(started)
        functional_role = next(
            row for row in roles if row["role"] == "functional_coverage_reviewer"
        )
        run_root = Path(str(started["manifest_path"])).parent
        artifact = run_root / str(functional_role["artifact_path"])
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        payload["test_obligations_reviewed"] = False
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        functional_role["artifact_sha256"] = hashlib.sha256(
            artifact.read_bytes()
        ).hexdigest()
        result_path = self.runtime / "unbound-functional-reviewer.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claimed complete component review",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "role_evidence": roles,
                    "verification": verification,
                    **coverage,
                    "read_back": "Captured files and outputs were reread",
                    "review": "Functional coverage was independently challenged",
                    "closure": "Claimed complete",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            WorkflowError, "test_obligations_reviewed mismatch"
        ):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_functional_reviewer_rejects_verification_metadata_swap(self) -> None:
        started = start_project_run(
            self.project,
            task="Review backend functional behavior",
            intent="review",
            mode="standard",
            target_type="component",
            target="backend",
        )
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        coverage = self._review_coverage(started)
        verification = self._test_outputs(started)
        roles = self._role_evidence(started)
        swapped = json.loads(json.dumps(verification))
        swapped[0]["command"] = "pytest -q --metadata-swapped-after-review"
        result_path = self.runtime / "swapped-verification-review.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Claimed complete component review",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "role_evidence": roles,
                    "verification": swapped,
                    **coverage,
                    "read_back": "Captured files and outputs were reread",
                    "review": "Functional coverage was independently challenged",
                    "closure": "Claimed complete",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "verification_sha256 mismatch"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_active_map_refresh_is_recoverable_and_verified(self) -> None:
        active = self._active()
        started = start_project_run(
            active,
            task="Review backend integration",
            intent="review",
            mode="deep",
            target_type="repository",
        )
        manifest = json.loads(Path(started["manifest_path"]).read_text(encoding="utf-8"))
        candidate_payload = yaml.safe_load(
            (self.docs / "SYSTEM_MAP.yaml").read_text(encoding="utf-8")
        )
        candidate_payload["unknowns"] = ["Map refreshed by verified active review"]
        candidate = Path(started["manifest_path"]).parent / "outputs" / "SYSTEM_MAP.yaml"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(
            yaml.safe_dump(candidate_payload, sort_keys=False), encoding="utf-8"
        )
        coverage = self._review_coverage(started)
        verification = self._test_outputs(started)
        roles = self._role_evidence(started)
        result_path = self.runtime / "active-map-review.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Review and map refresh completed",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "role_evidence": roles,
                    "verification": verification,
                    "system_map_candidate_path": "outputs/SYSTEM_MAP.yaml",
                    "system_map_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    **coverage,
                    "read_back": "Coverage, raw output and published map were reread",
                    "review": "Independent review found no unresolved issue",
                    "closure": "Map and project trace are recoverably committed",
                }
            ),
            encoding="utf-8",
        )
        before_events = verify_history(self.project)["events"]
        closed = close_project_run(
            active, run_id=started["run_id"], result_path=result_path
        )
        self.assertEqual(closed["status"], "completed")
        self.assertEqual(verify_history(self.project)["events"], before_events + 1)
        published = yaml.safe_load(
            (self.docs / "SYSTEM_MAP.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            published["unknowns"], ["Map refreshed by verified active review"]
        )
        preimage = (
            Path(started["manifest_path"]).parent
            / "closure-preimage"
            / "SYSTEM_MAP.preimage.yaml"
        )
        self.assertTrue(preimage.is_file())
        self.assertTrue(run_project_doctor(active)["ok"])

    def test_active_publication_transaction_recovers_every_crash_window(
        self,
    ) -> None:
        active = self._active()
        from aria import simple_run

        real_write_bytes = simple_run.atomic_write_bytes
        real_write_json = simple_run.atomic_write_json
        stages = ("publication", "history", "state", "manifest")
        for stage in stages:
            with self.subTest(stage=stage):
                started = start_project_run(
                    active,
                    task=f"Repository recovery review {stage}",
                    intent="review",
                    mode="deep",
                    target_type="repository",
                )
                run_root = Path(str(started["manifest_path"])).parent
                manifest = json.loads(
                    Path(str(started["manifest_path"])).read_text(encoding="utf-8")
                )
                candidate_payload = yaml.safe_load(
                    (self.docs / "SYSTEM_MAP.yaml").read_text(encoding="utf-8")
                )
                candidate_payload["unknowns"] = [
                    f"Recovered publication crash window: {stage}"
                ]
                candidate = run_root / "outputs" / "SYSTEM_MAP.yaml"
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text(
                    yaml.safe_dump(candidate_payload, sort_keys=False),
                    encoding="utf-8",
                )
                coverage = self._review_coverage(started)
                verification = self._test_outputs(started)
                roles = self._role_evidence(started)
                result_path = self.runtime / f"crash-publication-{stage}.json"
                result_path.write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "summary": f"Crash window {stage} reviewed",
                            "findings": [],
                            "scope_sha256": manifest["context"]["scope_sha256"],
                            "role_evidence": roles,
                            "verification": verification,
                            "system_map_candidate_path": "outputs/SYSTEM_MAP.yaml",
                            "system_map_sha256": hashlib.sha256(
                                candidate.read_bytes()
                            ).hexdigest(),
                            **coverage,
                            "read_back": "Every recovery artifact was reread",
                            "review": "Crash recovery was independently reviewed",
                            "closure": "One durable event and map publication are required",
                        }
                    ),
                    encoding="utf-8",
                )
                history_before = int(verify_history(active)["events"])
                crashed = False

                def crash_bytes(path: Path, payload: bytes) -> None:
                    nonlocal crashed
                    real_write_bytes(path, payload)
                    targets = {
                        "publication": self.docs / "SYSTEM_MAP.yaml",
                        "history": self.docs / "HISTORY.jsonl",
                        "state": self.docs / "STATE.yaml",
                    }
                    if stage in targets and Path(path) == targets[stage] and not crashed:
                        crashed = True
                        raise SystemExit(f"simulated closure death after {stage}")

                def crash_manifest(path: Path, payload: object) -> None:
                    nonlocal crashed
                    real_write_json(path, payload)
                    if (
                        stage == "manifest"
                        and Path(path).name == "manifest.json"
                        and isinstance(payload, dict)
                        and payload.get("status") == "completed"
                        and not crashed
                    ):
                        crashed = True
                        raise SystemExit("simulated closure death after manifest")

                with patch(
                    "aria.simple_run.atomic_write_bytes", side_effect=crash_bytes
                ), patch(
                    "aria.simple_run.atomic_write_json", side_effect=crash_manifest
                ):
                    with self.assertRaisesRegex(SystemExit, "closure death"):
                        close_project_run(
                            active,
                            run_id=str(started["run_id"]),
                            result_path=result_path,
                        )

                if stage == "manifest":
                    with self.assertRaisesRegex(WorkflowError, "already closed"):
                        close_project_run(
                            active,
                            run_id=str(started["run_id"]),
                            result_path=result_path,
                        )
                else:
                    retried = close_project_run(
                        active,
                        run_id=str(started["run_id"]),
                        result_path=result_path,
                    )
                    self.assertEqual(retried["status"], "completed")
                self.assertEqual(
                    int(verify_history(active)["events"]), history_before + 1
                )
                published = yaml.safe_load(
                    (self.docs / "SYSTEM_MAP.yaml").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    published["unknowns"],
                    [f"Recovered publication crash window: {stage}"],
                )
                journal = json.loads(
                    (run_root / "closure-journal.json").read_text(encoding="utf-8")
                )
                self.assertEqual(journal["phase"], "run-committed")
                self.assertEqual(journal["schema_version"], 2)
                self.assertTrue(run_project_doctor(active)["ok"])

    def test_component_review_cannot_replace_system_map(self) -> None:
        started = start_project_run(
            self.project,
            task="Review backend",
            intent="review",
            mode="standard",
            target_type="component",
            target="backend",
        )
        manifest = json.loads(Path(started["manifest_path"]).read_text(encoding="utf-8"))
        candidate = Path(started["manifest_path"]).parent / "outputs" / "SYSTEM_MAP.yaml"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes((self.docs / "SYSTEM_MAP.yaml").read_bytes())
        coverage = self._review_coverage(started)
        result_path = self.runtime / "component-map-replacement.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Component reviewed",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "system_map_candidate_path": "outputs/SYSTEM_MAP.yaml",
                    "system_map_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    **coverage,
                    "read_back": "Scope reread",
                    "review": "Component reviewed",
                    "closure": "Claimed complete",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "deep repository review"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_repository_map_refresh_cannot_drop_existing_components(self) -> None:
        started = start_project_run(
            self.project,
            task="Full repository review",
            intent="review",
            mode="deep",
            target_type="repository",
        )
        manifest = json.loads(Path(started["manifest_path"]).read_text(encoding="utf-8"))
        candidate_payload = yaml.safe_load(
            (self.docs / "SYSTEM_MAP.yaml").read_text(encoding="utf-8")
        )
        candidate_payload["components"] = candidate_payload["components"][:1]
        candidate = Path(started["manifest_path"]).parent / "outputs" / "SYSTEM_MAP.yaml"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(yaml.safe_dump(candidate_payload, sort_keys=False), encoding="utf-8")
        coverage = self._review_coverage(started)
        result_path = self.runtime / "downgraded-map.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Repository reviewed",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "system_map_candidate_path": "outputs/SYSTEM_MAP.yaml",
                    "system_map_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    **coverage,
                    "read_back": "Scope reread",
                    "review": "Repository reviewed",
                    "closure": "Claimed complete",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "cannot drop existing components"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_repository_map_rejects_semantic_downgrade_with_preserved_ids(self) -> None:
        started = start_project_run(
            self.project,
            task="Full repository map audit",
            intent="review",
            mode="deep",
            target_type="repository",
        )
        manifest = json.loads(Path(started["manifest_path"]).read_text(encoding="utf-8"))
        candidate_payload = yaml.safe_load(
            (self.docs / "SYSTEM_MAP.yaml").read_text(encoding="utf-8")
        )
        for component in candidate_payload["components"]:
            component["paths"] = [f"unrelated/{component['id']}/**"]
        candidate = Path(started["manifest_path"]).parent / "outputs" / "SYSTEM_MAP.yaml"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(
            yaml.safe_dump(candidate_payload, sort_keys=False), encoding="utf-8"
        )
        coverage = self._review_coverage(started)
        result_path = self.runtime / "semantic-map-downgrade.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Repository map reviewed",
                    "findings": [],
                    "scope_sha256": manifest["context"]["scope_sha256"],
                    "system_map_candidate_path": "outputs/SYSTEM_MAP.yaml",
                    "system_map_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    **coverage,
                    "read_back": "Map candidate reread",
                    "review": "Semantic map claimed complete",
                    "closure": "Claimed complete",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "loses the semantic anchor"):
            close_project_run(
                self.project, run_id=started["run_id"], result_path=result_path
            )

    def test_review_rejects_empty_ignored_component_scope(self) -> None:
        ignored = self.code / ".venv" / "only.py"
        ignored.parent.mkdir()
        ignored.write_text("generated = True\n", encoding="utf-8")
        self._git("add", "-f", ".venv/only.py")
        self._git("commit", "-q", "-m", "ignored fixture")
        with self.assertRaisesRegex(WorkflowError, "no reviewable files"):
            build_review_scope(
                self.project,
                target_type="component",
                target=".venv",
                spec=None,
            )

    def test_crash_retry_does_not_duplicate_active_history_event(self) -> None:
        active = self._active()
        started = start_project_run(
            active,
            task="Исправь service",
            intent="build",
            mode="quick",
            changed_paths=["backend/service.py"],
        )
        self._change_service()
        self._commit_run(started, "backend/service.py")
        test_output = self._test_output(started)
        result_path = self.runtime / "crash-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Service fixed",
                    "changed_files": ["backend/service.py"],
                    "tests": [test_output],
                    "read_back": "Expected value observed",
                    "review": "Diff reviewed",
                    "closure": "Requirements met",
                }
            ),
            encoding="utf-8",
        )
        from aria import simple_run

        real_write = simple_run.atomic_write_json
        failed = False

        def fail_final_manifest(path: Path, payload: object) -> None:
            nonlocal failed
            if (
                Path(path).name == "manifest.json"
                and isinstance(payload, dict)
                and payload.get("status") == "completed"
                and not failed
            ):
                failed = True
                raise OSError("simulated crash window")
            real_write(path, payload)

        with patch(
            "aria.simple_run.atomic_write_json", side_effect=fail_final_manifest
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                close_project_run(
                    active, run_id=started["run_id"], result_path=result_path
                )
        self.assertEqual(verify_history(active)["events"], 2)
        self.assertEqual(
            json.loads(Path(started["manifest_path"]).read_text(encoding="utf-8"))[
                "status"
            ],
            "started",
        )
        retried = close_project_run(
            active, run_id=started["run_id"], result_path=result_path
        )
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(verify_history(active)["events"], 2)

    def test_interleaved_close_waits_for_state_history_recovery(self) -> None:
        active = self._active()
        first = start_project_run(
            active,
            task="First blocked design",
            intent="design",
            mode="quick",
        )
        second = start_project_run(
            active,
            task="Second blocked design",
            intent="design",
            mode="quick",
        )

        def blocked_result(path: Path, summary: str) -> None:
            path.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "summary": summary,
                        "read_back": "Blocking state reread",
                        "review": "No completed result claimed",
                        "closure": "Resolve the external blocker",
                    }
                ),
                encoding="utf-8",
            )

        first_result = self.runtime / "first-interleaved.json"
        second_result = self.runtime / "second-interleaved.json"
        blocked_result(first_result, "First run blocked")
        blocked_result(second_result, "Second run blocked")

        from aria import simple_run

        real_write = simple_run.atomic_write_bytes

        def crash_before_state(path: Path, payload: bytes) -> None:
            if Path(path) == self.docs / "STATE.yaml":
                raise SystemExit("simulated process death after HISTORY write")
            real_write(path, payload)

        with patch("aria.simple_run.atomic_write_bytes", side_effect=crash_before_state):
            with self.assertRaisesRegex(SystemExit, "process death"):
                close_project_run(
                    active, run_id=first["run_id"], result_path=first_result
                )
        self.assertEqual(verify_history(active)["events"], 2)
        stale_state = yaml.safe_load(
            (self.docs / "STATE.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(stale_state["history_checkpoint"]["sequence"], 1)

        with self.assertRaisesRegex(WorkflowError, "recover the last incomplete run"):
            close_project_run(
                active, run_id=second["run_id"], result_path=second_result
            )
        recovered = close_project_run(
            active, run_id=first["run_id"], result_path=first_result
        )
        self.assertEqual(recovered["status"], "blocked")
        recovered_state = yaml.safe_load(
            (self.docs / "STATE.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(recovered_state["history_checkpoint"]["sequence"], 2)
        close_project_run(
            active, run_id=second["run_id"], result_path=second_result
        )
        self.assertEqual(verify_history(active)["events"], 3)

    def test_active_deep_design_promotes_spec_and_links_state(self) -> None:
        active = self._active()
        started = start_project_run(
            active,
            task="Спроектируй новый сложный контракт",
            intent="design",
            mode="deep",
        )
        self._lock_feature_contract(started, project=active)
        run_root = Path(started["manifest_path"]).parent
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        candidate = run_root / "outputs" / "spec.md"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(
            "---\n"
            "schema_version: 1\n"
            "id: new_contract\n"
            f"task_id: {manifest['task_id']}\n"
            "revision: 1\n"
            "status: approved\n"
            "adrs: []\n"
            "research: []\n"
            "references: []\n"
            "---\n\n"
            "# New contract\n\n- AC1\n",
            encoding="utf-8",
        )
        feature = self._feature_contract_evidence(
            started,
            verification=[],
            changed_files=[],
            spec_candidate=candidate,
            project=active,
        )
        result_path = self.runtime / "design-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "proposed",
                    "summary": "Contract designed",
                    "deliverable": "Compact implementation contract",
                    "spec_candidate_path": "outputs/spec.md",
                    "spec_target": "specs/active/new_contract.md",
                    "spec_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    "research_assessment": {
                        "required": False,
                        "reason": "No external facts are needed for this local contract",
                        "references": [],
                    },
                    "adr_assessment": {
                        "required": False,
                        "reason": "No durable architecture choice is introduced",
                        "candidates": [],
                    },
                    "read_back": "Spec reread",
                    "review": "Architecture reviewed",
                    "closure": "Ready for deep build",
                    **feature,
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )
        direct_result = self.runtime / "design-direct-completed.json"
        direct_payload = json.loads(result_path.read_text(encoding="utf-8"))
        direct_payload["status"] = "completed"
        direct_result.write_text(json.dumps(direct_payload), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "exact user approval"):
            close_project_run(
                active, run_id=started["run_id"], result_path=direct_result
            )
        proposal = close_project_run(
            active, run_id=started["run_id"], result_path=result_path
        )
        self.assertEqual(proposal["status"], "awaiting_user_approval")
        self.assertFalse((self.docs / "specs" / "active" / "new_contract.md").exists())
        closed = self._approve_proposal(active, started, proposal)
        self.assertEqual(len(closed["project_writes"]), 3)
        self.assertEqual(
            (self.docs / "specs" / "active" / "new_contract.md").read_bytes(),
            candidate.read_bytes(),
        )
        state = (self.docs / "STATE.yaml").read_text(encoding="utf-8")
        self.assertIn("spec: specs/active/new_contract.md", state)
        event = json.loads(
            (self.docs / "HISTORY.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(
            event["result"]["trace"]["feature_contract"]["requirements"][0]["id"],
            "R-001",
        )

    def test_new_spec_publication_is_removed_and_retried_after_process_death(
        self,
    ) -> None:
        active = self._active()
        started = start_project_run(
            active,
            task="Design a crash-recoverable publication contract",
            intent="design",
            mode="deep",
        )
        self._lock_feature_contract(started, project=active)
        run_root = Path(str(started["manifest_path"])).parent
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        candidate = run_root / "outputs" / "recoverable-spec.md"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(
            "---\n"
            "schema_version: 1\n"
            "id: recoverable_contract\n"
            f"task_id: {manifest['task_id']}\n"
            "revision: 1\n"
            "status: approved\n"
            "adrs: []\n"
            "research: []\n"
            "references: []\n"
            "---\n\n"
            "# Recoverable contract\n\n- AC1\n",
            encoding="utf-8",
        )
        feature = self._feature_contract_evidence(
            started,
            verification=[],
            changed_files=[],
            spec_candidate=candidate,
            project=active,
        )
        result_path = self.runtime / "recoverable-design.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "proposed",
                    "summary": "Recoverable publication designed",
                    "deliverable": "A crash-safe implementation contract",
                    "spec_candidate_path": "outputs/recoverable-spec.md",
                    "spec_target": "specs/active/recoverable_contract.md",
                    "spec_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
                    "research_assessment": {
                        "required": False,
                        "reason": "No external facts are required",
                        "references": [],
                    },
                    "adr_assessment": {
                        "required": False,
                        "reason": "No separate durable architecture choice",
                        "candidates": [],
                    },
                    "read_back": "Candidate reread",
                    "review": "Architecture reviewed",
                    "closure": "Ready for exact approval",
                    **feature,
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )
        proposal = close_project_run(
            active, run_id=str(started["run_id"]), result_path=result_path
        )
        target = self.docs / "specs" / "active" / "recoverable_contract.md"
        history_before = int(verify_history(active)["events"])
        from aria import simple_run

        real_write = simple_run.atomic_write_bytes
        crashed = False

        def crash_after_spec(path: Path, payload: bytes) -> None:
            nonlocal crashed
            real_write(path, payload)
            if Path(path) == target and not crashed:
                crashed = True
                raise SystemExit("simulated death after new spec publication")

        with patch(
            "aria.simple_run.atomic_write_bytes", side_effect=crash_after_spec
        ):
            with self.assertRaisesRegex(SystemExit, "new spec publication"):
                self._approve_proposal(active, started, proposal)
        self.assertTrue(target.is_file())

        closed = close_project_run(
            active,
            run_id=str(started["run_id"]),
            result_path=run_root / "approved-result-input.json",
        )
        self.assertEqual(closed["status"], "completed")
        self.assertEqual(target.read_bytes(), candidate.read_bytes())
        self.assertEqual(int(verify_history(active)["events"]), history_before + 1)
        journal = json.loads(
            (run_root / "closure-journal.json").read_text(encoding="utf-8")
        )
        self.assertEqual(journal["phase"], "run-committed")

    def test_deep_design_publishes_material_research_adr_and_portable_trace(
        self,
    ) -> None:
        active = self._active()
        started = start_project_run(
            active,
            task="Design a durable API retry contract using external references",
            intent="design",
            mode="deep",
        )
        self._lock_feature_contract(started, project=active)
        run_root = Path(str(started["manifest_path"])).parent
        outputs = run_root / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        task_id = str(manifest["task_id"])
        reference = {
            "id": "REF-001",
            "url": "https://www.rfc-editor.org/rfc/rfc9110",
            "title": "HTTP Semantics",
            "accessed_at": "2026-07-17T00:00:00Z",
            "claims": ["Idempotent methods can be retried after communication failure"],
        }
        research = outputs / "research.md"
        research.write_text(
            "---\n"
            "schema_version: 1\n"
            "id: research-retry-contract\n"
            f"task_id: {task_id}\n"
            "references: [REF-001]\n"
            "---\n\n"
            "# Retry research\n\nThe cited standard constrains retry safety.\n",
            encoding="utf-8",
        )
        adr = outputs / "adr.md"
        adr.write_text(
            "---\n"
            "schema_version: 1\n"
            "id: ADR-001\n"
            "status: accepted\n"
            f"task_ids: [{task_id}]\n"
            "specs: [retry_contract]\n"
            "references: [REF-001]\n"
            "---\n\n"
            "# Retry only idempotent operations\n",
            encoding="utf-8",
        )
        spec = outputs / "spec.md"
        spec.write_text(
            "---\n"
            "schema_version: 1\n"
            "id: retry_contract\n"
            f"task_id: {task_id}\n"
            "revision: 1\n"
            "status: approved\n"
            "adrs: [ADR-001]\n"
            "research: [research-retry-contract]\n"
            "references: [REF-001]\n"
            "---\n\n"
            "# Retry contract\n\n- Retry only idempotent operations.\n",
            encoding="utf-8",
        )
        feature = self._feature_contract_evidence(
            started,
            verification=[],
            changed_files=[],
            spec_candidate=spec,
            project=active,
        )
        result_path = self.runtime / "traced-design-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "proposed",
                    "summary": "Retry contract designed from a material standard",
                    "deliverable": "Approved retry contract",
                    "spec_candidate_path": "outputs/spec.md",
                    "spec_target": "specs/active/retry_contract.md",
                    "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
                    "research_assessment": {
                        "required": True,
                        "reason": "Retry semantics depend on an external HTTP standard",
                        "references": [reference],
                        "candidate_path": "outputs/research.md",
                        "target": "knowledge/research/retry_contract.md",
                        "sha256": hashlib.sha256(research.read_bytes()).hexdigest(),
                    },
                    "adr_assessment": {
                        "required": True,
                        "reason": "Retry policy is a durable cross-component choice",
                        "candidates": [
                            {
                                "id": "ADR-001",
                                "candidate_path": "outputs/adr.md",
                                "target": "adr/active/ADR-001-retry-contract.md",
                                "sha256": hashlib.sha256(adr.read_bytes()).hexdigest(),
                            }
                        ],
                    },
                    "read_back": "All published candidates were reread",
                    "review": "Spec, research, and ADR links were reviewed",
                    "closure": "The design is ready for a traced implementation",
                    **feature,
                    "role_evidence": self._role_evidence(started),
                }
            ),
            encoding="utf-8",
        )

        proposal = close_project_run(
            active, run_id=str(started["run_id"]), result_path=result_path
        )
        closed = self._approve_proposal(active, started, proposal)

        self.assertEqual(len(closed["project_writes"]), 5)
        for relative in (
            "specs/active/retry_contract.md",
            "knowledge/research/retry_contract.md",
            "adr/active/ADR-001-retry-contract.md",
        ):
            self.assertTrue((self.docs / relative).is_file(), relative)
        state = yaml.safe_load((self.docs / "STATE.yaml").read_text(encoding="utf-8"))
        task = state["frontier"]
        self.assertEqual(task["trace"]["spec"]["id"], "retry_contract")
        self.assertEqual(task["trace"]["adrs"][0]["id"], "ADR-001")
        self.assertEqual(task["trace"]["references"][0]["id"], "REF-001")
        event = json.loads(
            (self.docs / "HISTORY.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(event["result"]["trace"]["spec"]["id"], "retry_contract")
        self.assertEqual(event["result"]["trace"]["references"][0]["id"], "REF-001")
        status = project_status(active, task_id=task_id)
        self.assertEqual(
            status["trace"]["state"]["trace"]["spec"]["id"], "retry_contract"
        )
        self.assertEqual(len(status["trace"]["history_events"]), 1)

    def test_doctor_rejects_foreign_state_identity(self) -> None:
        state_path = self.docs / "STATE.yaml"
        state_path.write_text(
            state_path.read_text(encoding="utf-8").replace(
                "project_id: demo", "project_id: other"
            ),
            encoding="utf-8",
        )
        doctor = run_project_doctor(self.project)
        self.assertFalse(doctor["ok"])
        state_check = next(
            check for check in doctor["checks"] if check["id"] == "state_identity"
        )
        self.assertFalse(state_check["ok"])

    def test_doctor_accepts_previous_1_5_patch_project_metadata(self) -> None:
        compatible = replace(self.project, framework_version="1.5.2")

        doctor = run_project_doctor(compatible)

        version_check = next(
            check for check in doctor["checks"] if check["id"] == "framework_version"
        )
        self.assertTrue(version_check["ok"])

    def test_repository_scope_excludes_generated_and_legacy_directories(self) -> None:
        for relative in (
            ".venv/lib/site.py",
            "project-docs/legacy.md",
            "_db_backups/db.sql",
        ):
            path = self.code / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("generated\n", encoding="utf-8")
        for relative in (
            "backend/credentials.py",
            "deploy/secrets.yaml",
            "tests/test_credentials.py",
        ):
            path = self.code / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("reviewable = True\n", encoding="utf-8")
        for relative in (
            ".env",
            "private.pem",
            "runtime.log",
            "snapshot.sqlite",
            "screen.png",
            "bundle.tar",
        ):
            path = self.code / relative
            path.write_bytes(b"runtime-or-sensitive")
        scope = build_review_scope(
            self.project, target_type="repository", target=None, spec=None
        )
        included = {row["path"] for row in scope["files"]}
        excluded = {row["path"] for row in scope["excluded"]}
        self.assertFalse(
            included
            & {
                ".venv/lib/site.py",
                "project-docs/legacy.md",
                "_db_backups/db.sql",
            }
        )
        self.assertTrue(
            {
                ".venv/lib/site.py",
                "project-docs/legacy.md",
                "_db_backups/db.sql",
            }.issubset(excluded)
        )
        self.assertTrue(
            {
                "backend/credentials.py",
                "deploy/secrets.yaml",
                "tests/test_credentials.py",
            }.issubset(included)
        )
        self.assertTrue(
            {
                ".env",
                "private.pem",
                "runtime.log",
                "snapshot.sqlite",
                "screen.png",
                "bundle.tar",
            }.issubset(excluded)
        )

    def test_repository_scope_includes_product_packages_named_coverage(self) -> None:
        product_sources = {
            "app/src/main/java/ru/solar/autopilot/coverage/GapTracker.java",
            "app/src/test/java/ru/solar/autopilot/coverage/GapTrackerTest.java",
            "services/coverage/policy.py",
        }
        for relative in product_sources:
            path = self.code / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("product_source = True\n", encoding="utf-8")

        scope = build_review_scope(
            self.project, target_type="repository", target=None, spec=None
        )

        included = {row["path"] for row in scope["files"]}
        excluded = {row["path"] for row in scope["excluded"]}
        self.assertTrue(product_sources.issubset(included))
        self.assertFalse(product_sources & excluded)

    def test_repository_scope_leaves_git_ignored_coverage_output_outside_inventory(
        self,
    ) -> None:
        (self.code / ".gitignore").write_text("/coverage/\n", encoding="utf-8")
        report = self.code / "coverage" / "index.html"
        report.parent.mkdir()
        report.write_text("generated report\n", encoding="utf-8")

        scope = build_review_scope(
            self.project, target_type="repository", target=None, spec=None
        )

        captured = {
            row["path"] for key in ("files", "excluded") for row in scope[key]
        }
        self.assertNotIn("coverage/index.html", captured)

    def test_project_config_cannot_hide_source_with_git_ignore_prefix(self) -> None:
        payload = yaml.safe_load(
            (self.docs / "PROJECT.yaml").read_text(encoding="utf-8")
        )
        payload.setdefault("context", {})["git_ignore_prefixes"] = ["backend"]
        (self.docs / "PROJECT.yaml").write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(ConfigurationError, "only the ARIA docs boundary"):
            load_project(
                "demo",
                framework_root=self.framework,
                runtime_root=self.runtime,
                registry_path=self.registry,
            )

    def test_windows_ads_and_alternate_framework_root_are_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            safe_relative_path("dir/file.txt:hidden")
        with self.assertRaisesRegex(ConfigurationError, "running ARIA framework"):
            load_project(
                "demo",
                framework_root=self.runtime,
                runtime_root=self.runtime,
                registry_path=self.registry,
            )

    def test_wheel_framework_root_accepts_only_line_ending_equivalent_sources(
        self,
    ) -> None:
        installed_root = Path(self.temporary.name) / "installed-site-packages"
        installed_package = installed_root / "aria"
        requested_root = Path(self.temporary.name) / "clean-archive"
        requested_package = requested_root / "aria"
        installed_package.mkdir(parents=True)
        requested_package.mkdir(parents=True)
        (requested_root / ".aria-root").write_text("aria-codex\n", encoding="utf-8")
        for source in (self.framework / "aria").rglob("*.py"):
            relative = source.relative_to(self.framework / "aria")
            content = source.read_bytes().replace(b"\r\n", b"\n")
            installed = installed_package / relative
            requested = requested_package / relative
            installed.parent.mkdir(parents=True, exist_ok=True)
            requested.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(content)
            requested.write_bytes(content.replace(b"\n", b"\r\n"))

        with patch("aria.project.__file__", str(installed_package / "project.py")):
            self.assertEqual(_framework_root(requested_root), requested_root.resolve())
            (requested_package / "project.py").write_bytes(
                (requested_package / "project.py").read_bytes()
                + b"# semantic change\r\n"
            )
            with self.assertRaisesRegex(
                ConfigurationError, "must use the running ARIA framework"
            ):
                _framework_root(requested_root)

    def test_review_scope_is_bound_to_registered_code_root(self) -> None:
        scope = build_review_scope(
            self.project,
            target_type="component",
            target="backend",
            spec=None,
        )
        self.assertEqual(Path(scope["root"]), self.code.resolve())
        self.assertEqual(
            {row["path"] for row in scope["files"]},
            {"backend/pyproject.toml", "backend/service.py"},
        )
        self.assertEqual(scope["file_count"], 2)

    def test_public_help_hides_internal_commands(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("run", help_text)
        self.assertIn("spec", help_text)
        self.assertNotIn("ack-context", help_text)
        self.assertNotIn("score-add", help_text)
        self.assertNotIn("migration-gate", help_text)

    def test_engine_hash_ignores_generated_caches(self) -> None:
        root = Path(self.temporary.name) / "engine"
        root.mkdir()
        (root / "source.py").write_text("value = 1\n", encoding="utf-8")
        baseline = engine_state(root)
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "source.pyc").write_bytes(b"generated")
        (root / ".pytest_cache").mkdir()
        (root / ".pytest_cache" / "README.md").write_text("generated", encoding="utf-8")
        (root / ".coverage").write_bytes(b"generated")
        self.assertEqual(engine_state(root)["sha256"], baseline["sha256"])

    def test_engine_hash_includes_operational_package_named_coverage(self) -> None:
        root = Path(self.temporary.name) / "engine-with-coverage-package"
        package = root / "aria" / "coverage"
        package.mkdir(parents=True)
        (root / "aria" / "__init__.py").write_text("", encoding="utf-8")
        baseline = engine_state(root)

        policy = package / "policy.py"
        policy.write_text("enabled = True\n", encoding="utf-8")
        updated = engine_state(root)

        self.assertNotEqual(updated["sha256"], baseline["sha256"])
        self.assertIn("aria/coverage/policy.py", {row["path"] for row in updated["files"]})

    def test_roadmap_run_without_task_selects_focus_and_skips_deferred(self) -> None:
        project_path = self.docs / "PROJECT.yaml"
        project_path.write_text(
            project_path.read_text(encoding="utf-8").replace(
                "display_name: Demo\n",
                "display_name: Demo\nstate_profile: roadmap\n",
            ),
            encoding="utf-8",
        )
        history = verify_history(self.project)
        (self.docs / "STATE.yaml").write_text(
            f"""schema_version: 2
project_id: demo
profile: roadmap
focus:
  id: release
  goal: Finish release
  stage_id: stage-1
  ordered_tasks: [done-task, deferred-task, next-task]
current:
  task_id: null
  status: null
stages:
  - id: stage-1
    title: Stage 1
    status: in_progress
    exit_criteria: []
    tasks:
      - id: done-task
        title: Done task
        status: done
        priority: 1
        depends_on: []
        spec: null
      - id: deferred-task
        title: Deferred task
        status: deferred
        priority: 2
        depends_on: [done-task]
        spec: null
      - id: next-task
        title: Next roadmap task
        status: not_started
        priority: 99
        depends_on: [done-task]
        spec: specs/active/demo_task.md
        safety_impact: high
issues:
  blockers: []
  bugs: []
  questions: []
  inbox: []
last_verified: null
last_completed: null
history_checkpoint:
  sequence: {history["events"]}
  event_sha256: {history["head_sha256"]}
""",
            encoding="utf-8",
        )
        roadmap_project = load_project(
            "demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
            registry_path=self.registry,
        )
        started = start_project_run(roadmap_project, task=None)
        self.assertEqual(started["task_selection"]["task_id"], "next-task")
        self.assertEqual(started["task_selection"]["source"], "focus")
        self.assertEqual(started["route"]["mode"], "deep")
        self.assertIn("safety", started["route"]["risk_signals"])
        self.assertEqual(
            started["task_selection"]["skipped"],
            [
                {"task_id": "done-task", "reason": "done"},
                {"task_id": "deferred-task", "reason": "deferred"},
            ],
        )
        context = Path(started["context_path"]).read_text(encoding="utf-8")
        self.assertIn("selected_task:", context)
        self.assertNotIn("title: Deferred task", context)

        active = self._active(roadmap_project)
        active_started = start_project_run(
            active,
            task="next-task",
            intent="design",
            mode="quick",
        )
        result_path = self.runtime / "roadmap-design-result.json"
        result_path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Roadmap design completed",
                    "deliverable": "Use the existing section contract",
                    "read_back": "Design reread against the selected roadmap task",
                    "review": "Design scope reviewed",
                    "closure": "Roadmap task design requirements are met",
                }
            ),
            encoding="utf-8",
        )
        close_project_run(
            active,
            run_id=active_started["run_id"],
            result_path=result_path,
        )
        closed_state = yaml.safe_load(
            (self.docs / "STATE.yaml").read_text(encoding="utf-8")
        )
        self.assertIsNone(closed_state["current"]["task_id"])
        self.assertEqual(closed_state["stages"][0]["tasks"][2]["status"], "ready")

        build_started = start_project_run(
            active,
            task="next-task",
            intent="build",
            mode="quick",
            changed_paths=["backend/service.py"],
        )
        (self.code / "backend" / "service.py").write_text(
            "def value():\n    return 2\n", encoding="utf-8"
        )
        self._commit_run(build_started, "backend/service.py")
        test_output = self._test_output(
            build_started, "roadmap linked scenario passed\n"
        )
        build_result = self.runtime / "roadmap-build-result.json"
        build_result.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "Roadmap implementation completed",
                    "changed_files": ["backend/service.py"],
                    "tests": [test_output],
                    "read_back": "Changed behavior and test output reread",
                    "review": "Final implementation delta reviewed",
                    "closure": "Roadmap implementation requirements are met",
                }
            ),
            encoding="utf-8",
        )
        close_project_run(
            active,
            run_id=build_started["run_id"],
            result_path=build_result,
        )
        built_state = yaml.safe_load(
            (self.docs / "STATE.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(built_state["stages"][0]["tasks"][2]["status"], "done")
        self.assertTrue(verify_history(active)["ok"])

    def test_spec_read_docs_and_read_code_are_fingerprinted(self) -> None:
        spec_path = self.docs / "specs" / "active" / "demo_task.md"
        spec_path.write_text(
            """---
read_docs:
  - STACK.md
read_code:
  - backend/service.py
---
# Demo spec
""",
            encoding="utf-8",
        )
        started = start_project_run(
            self.project,
            task="Implement the demo contract across the relevant component",
            intent="build",
            mode="standard",
            spec="specs/active/demo_task.md",
        )
        manifest = json.loads(
            Path(started["manifest_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [row["path"] for row in manifest["context"]["relevant_code"]],
            ["backend/service.py"],
        )
        self.assertEqual(manifest["context"]["unresolved_spec_hints"], [])
        context = Path(started["context_path"]).read_text(encoding="utf-8")
        self.assertIn("Read document `STACK.md`", context)
        self.assertIn("Read code `backend/service.py`", context)

    def test_public_feature_lifecycle_locks_amends_and_exports_contract(self) -> None:
        started = start_feature(
            self.project,
            task="Add a verified lifecycle capability",
        )
        manifest = json.loads(Path(started["manifest_path"]).read_text(encoding="utf-8"))
        phase_files = {}
        for phase in ("specify", "clarify", "plan"):
            path = self.runtime / f"{phase}.md"
            path.write_text(f"# {phase.title()}\n\nVerified {phase} content.\n", encoding="utf-8")
            phase_files[phase] = path
            submitted = submit_lifecycle_phase(
                self.project,
                run_id=str(started["run_id"]),
                phase=phase,
                input_path=path,
            )
            self.assertTrue(submitted["ok"])
        tasks_path = self.runtime / "tasks.md"
        tasks_path.write_text("# Tasks\n\n- [ ] Implement and verify lifecycle.\n", encoding="utf-8")
        contract = {
            "schema_version": 1,
            "run_id": manifest["run_id"],
            "task_id": manifest["task_id"],
            "status": "ready",
            "outcome": "Lifecycle capability is observable and verified",
            "ambiguities_resolved": True,
            "requirements": [{"id": "R-001", "statement": "Provide the lifecycle capability"}],
            "acceptance": [
                {
                    "id": "AC-001",
                    "requirement_ids": ["R-001"],
                    "oracle": "The lifecycle capability passes its observable verification",
                }
            ],
            "clarifications": [],
            "plan": {
                "summary": "Implement and verify the lifecycle",
                "steps": [
                    {
                        "id": "P-001",
                        "title": "Implement lifecycle",
                        "requirement_ids": ["R-001"],
                    }
                ],
            },
            "tasks": [
                {
                    "id": "T-001",
                    "title": "Implement lifecycle",
                    "requirement_ids": ["R-001"],
                    "plan_step_ids": ["P-001"],
                    "depends_on": [],
                }
            ],
        }
        contract_path = self.runtime / "feature-contract-input.json"
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        submitted = submit_lifecycle_phase(
            self.project,
            run_id=str(started["run_id"]),
            phase="tasks",
            input_path=tasks_path,
            contract_path=contract_path,
        )
        self.assertEqual(submitted["phase"], "implement")
        implementing = begin_implementation(
            self.project, run_id=str(started["run_id"])
        )
        self.assertEqual(implementing["phase"], "converge")

        amended = json.loads(json.dumps(contract))
        amended["requirements"].append(
            {"id": "R-002", "statement": "Preserve a revision receipt"}
        )
        amended["acceptance"].append(
            {
                "id": "AC-002",
                "requirement_ids": ["R-002"],
                "oracle": "A chained revision receipt is readable",
            }
        )
        amended["plan"]["steps"][0]["requirement_ids"].append("R-002")
        amended["tasks"][0]["requirement_ids"].append("R-002")
        amended_path = self.runtime / "amended-contract.json"
        amended_path.write_text(json.dumps(amended), encoding="utf-8")
        revision = amend_feature_contract(
            self.project,
            run_id=str(started["run_id"]),
            contract_path=amended_path,
            reason="Discovered a material audit requirement",
        )
        self.assertEqual(revision["revision"], 1)
        self.assertNotEqual(revision["before_sha256"], revision["after_sha256"])
        self.assertEqual(
            lifecycle_status(self.project, run_id=str(started["run_id"]))["phase"],
            "converge",
        )
        exported = export_spec_kit(
            self.project,
            run_id=str(started["run_id"]),
            output_dir=self.runtime / "spec-kit-export",
        )
        self.assertEqual({row["path"] for row in exported["files"]}, {"spec.md", "plan.md", "tasks.md"})

    def test_spec_kit_import_creates_valid_contract_without_manual_sha(self) -> None:
        started = start_feature(self.project, task="Import a Spec Kit feature")
        source = self.runtime / "spec-kit-source"
        source.mkdir()
        (source / "spec.md").write_text(
            "# Feature\n\n## Functional Requirements\n\n"
            "- **FR-001**: Return an observable result\n\n"
            "## Acceptance Scenarios\n\n"
            "1. [FR-001] **Given** a valid request, **When** it is processed, **Then** an observable result is returned.\n",
            encoding="utf-8",
        )
        (source / "plan.md").write_text(
            "# Implementation Plan\n\nUse the existing service boundary.\n\nCoverage: [FR-001]\n",
            encoding="utf-8",
        )
        (source / "tasks.md").write_text(
            "# Tasks\n\n- [ ] T001 [FR-001] Implement the observable result\n",
            encoding="utf-8",
        )
        imported = import_spec_kit(
            self.project,
            run_id=str(started["run_id"]),
            spec_dir=source,
        )
        self.assertEqual(imported["phase"], "implement")
        self.assertEqual(imported["requirements"], 1)
        locked = begin_implementation(self.project, run_id=str(started["run_id"]))
        self.assertEqual(locked["phase"], "converge")

    def _imported_managed_feature(self) -> tuple[dict[str, object], Path]:
        started = start_feature(self.project, task="Exercise managed lifecycle gates")
        source = self.runtime / f"spec-kit-{started['run_id']}"
        source.mkdir()
        (source / "spec.md").write_text(
            "# Feature\n\n## Functional Requirements\n\n"
            "- **FR-001**: Return an observable result\n\n"
            "## Acceptance Scenarios\n\n"
            "1. [FR-001] **Given** a request, **When** processed, **Then** a result is returned.\n",
            encoding="utf-8",
        )
        (source / "plan.md").write_text(
            "# Plan\n\nImplement through the service.\n\nCoverage: [FR-001]\n",
            encoding="utf-8",
        )
        (source / "tasks.md").write_text(
            "# Tasks\n\n- [ ] T001 [FR-001] Implement the result\n",
            encoding="utf-8",
        )
        import_spec_kit(
            self.project, run_id=str(started["run_id"]), spec_dir=source
        )
        return started, source

    def test_spec_kit_import_rejects_missing_mapping_and_locked_reimport(self) -> None:
        started = start_feature(self.project, task="Reject fabricated import coverage")
        source = self.runtime / "unmapped-spec-kit"
        source.mkdir()
        (source / "spec.md").write_text(
            "# Feature\n\n- **FR-001**: Return a result\n\n"
            "1. **Given** a request, **When** processed, **Then** return a result.\n",
            encoding="utf-8",
        )
        (source / "plan.md").write_text("# Plan\n\nImplement it.\n", encoding="utf-8")
        (source / "tasks.md").write_text(
            "# Tasks\n\n- [ ] T001 Implement it\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(WorkflowError, r"explicit \[FR-001\]"):
            import_spec_kit(
                self.project, run_id=str(started["run_id"]), spec_dir=source
            )

        locked_started, valid_source = self._imported_managed_feature()
        begin_implementation(self.project, run_id=str(locked_started["run_id"]))
        contract_path = (
            Path(str(locked_started["manifest_path"])).parent
            / "outputs"
            / "feature-contract.json"
        )
        before = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(WorkflowError, "forbidden after"):
            import_spec_kit(
                self.project,
                run_id=str(locked_started["run_id"]),
                spec_dir=valid_source,
            )
        self.assertEqual(before, hashlib.sha256(contract_path.read_bytes()).hexdigest())

    def test_managed_core_gates_and_blocked_terminal_are_fail_closed(self) -> None:
        started = start_feature(self.project, task="Enforce lifecycle core gates")
        with self.assertRaisesRegex(WorkflowError, "must be in 'implement'"):
            lock_project_feature_contract(
                self.project, run_id=str(started["run_id"])
            )
        lifecycle_path = Path(str(started["lifecycle_path"]))
        lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        lifecycle["phase"] = "completed"
        lifecycle_path.write_text(json.dumps(lifecycle), encoding="utf-8")
        phase_input = self.runtime / "terminal-spec.md"
        phase_input.write_text("# Cannot reopen\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "terminal"):
            submit_lifecycle_phase(
                self.project,
                run_id=str(started["run_id"]),
                phase="specify",
                input_path=phase_input,
            )

        blocked_started, _ = self._imported_managed_feature()
        begin_implementation(self.project, run_id=str(blocked_started["run_id"]))
        result = self.runtime / "managed-blocked.json"
        result.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "summary": "External dependency unavailable",
                    "read_back": "Blocking state reread",
                    "review": "No completed result claimed",
                    "closure": "Resolve the external blocker",
                }
            ),
            encoding="utf-8",
        )
        closed = converge_feature(
            self.project,
            run_id=str(blocked_started["run_id"]),
            result_path=result,
        )
        self.assertEqual(closed["lifecycle_phase"], "blocked")
        retried = converge_feature(
            self.project,
            run_id=str(blocked_started["run_id"]),
            result_path=result,
        )
        self.assertTrue(retried["idempotent"])

    def test_amendment_recovers_crash_and_verifies_receipt(self) -> None:
        from aria import lifecycle as lifecycle_module

        started, _ = self._imported_managed_feature()
        begin_implementation(self.project, run_id=str(started["run_id"]))
        run_root = Path(str(started["manifest_path"])).parent
        current = json.loads(
            (run_root / "outputs" / "feature-contract.json").read_text(encoding="utf-8")
        )
        current["outcome"] = "Observable result plus a crash-safe revision"
        amended = self.runtime / "crash-safe-amendment.json"
        amended.write_text(json.dumps(current), encoding="utf-8")
        real_write = lifecycle_module.atomic_write_json
        failed = False

        def fail_manifest_once(path: Path, payload: object) -> None:
            nonlocal failed
            if (
                not failed
                and Path(path).name == "manifest.json"
                and isinstance(payload, dict)
                and payload.get("amendment_history")
            ):
                failed = True
                raise OSError("simulated amendment crash")
            real_write(path, payload)

        with patch("aria.lifecycle.atomic_write_json", side_effect=fail_manifest_once):
            with self.assertRaisesRegex(OSError, "simulated amendment crash"):
                amend_feature_contract(
                    self.project,
                    run_id=str(started["run_id"]),
                    contract_path=amended,
                    reason="Crash recovery test",
                )
        recovered = amend_feature_contract(
            self.project,
            run_id=str(started["run_id"]),
            contract_path=amended,
            reason="Crash recovery test",
        )
        self.assertEqual(recovered["revision"], 1)
        Path(str(recovered["receipt_path"])).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "receipt mismatch"):
            lifecycle_status(self.project, run_id=str(started["run_id"]))


if __name__ == "__main__":
    unittest.main()
