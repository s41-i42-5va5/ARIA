from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aria.access import bootstrap_access
from aria.backlog import (
    add_backlog_item,
    claim_backlog_item,
    load_backlog,
    reconcile_accepted_decisions,
)
from aria.errors import WorkflowError
from aria.governance import (
    decision_reconciliation_plan,
    governance_diagnostics,
    governance_preflight,
)
from aria.identity import enroll_identity
from aria.project import load_project
from aria.project_init import initialize_project
from aria.simple_run import start_project_run


class GovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.framework = Path(__file__).resolve().parents[1]
        self.code = root / "code"
        self.docs = root / "docs"
        self.runtime = root / "runtime"
        self.code.mkdir()
        (self.code / "docs" / "decisions").mkdir(parents=True)
        (self.code / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.code / "docs" / "decisions" / "DECISIONS.md").write_text(
            "# Decisions\n\n"
            "| ID | Status | Decision |\n"
            "|---|---|---|\n"
            "| DEC-003 | ACCEPTED | Use the approved provider |\n"
            "| DEC-004 | OPEN | Select access mode |\n",
            encoding="utf-8",
        )
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "ARIA Tests")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "fixture")
        initialize_project(
            "governed",
            code_root=self.code,
            docs_root=self.docs,
            runtime_root=self.runtime,
        )
        self.project = load_project(
            "governed",
            framework_root=self.framework,
            runtime_root=self.runtime,
        )
        request = root / "owner.json"
        enroll_identity(
            self.runtime,
            actor_id="local-owner",
            device_id="owner-pc",
            request_path=request,
        )
        bootstrap_access(
            self.project, actor_id="local-owner", device_id="owner-pc"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.code), *arguments],
            check=True,
            capture_output=True,
        )

    def _add_feature(self) -> str:
        added = add_backlog_item(
            self.project,
            title="Deliver governed change",
            item_type="feature",
            priority="high",
            target_versions=["1.5"],
            acceptance="The change is bound to a verified ARIA run",
            source_kind="canonical-backlog",
            source_ref="DEV-001",
            requirements=["DEV-001"],
            expected_revision=0,
            actor_id="local-owner",
            device_id="owner-pc",
        )
        return str(added["item"]["id"])

    def test_dirty_tree_without_active_run_requires_recovery(self) -> None:
        (self.code / "service.py").write_text("VALUE = 2\n", encoding="utf-8")

        diagnostics = governance_diagnostics(self.project)
        self.assertFalse(diagnostics["ok"])
        self.assertEqual(diagnostics["state"], "RECOVERY_REQUIRED")
        self.assertIn(
            "unmanaged_worktree_changes",
            {row["id"] for row in diagnostics["issues"]},
        )
        read = governance_preflight(self.project, operation="read")
        write = governance_preflight(self.project, operation="write")
        self.assertTrue(read["ok"])
        self.assertFalse(write["ok"])
        self.assertEqual(write["state"], "RECOVERY_REQUIRED")

    def test_build_requires_claimed_backlog_binding_and_preflight(self) -> None:
        item_id = self._add_feature()
        with self.assertRaisesRegex(WorkflowError, "TASK_REQUIRED"):
            start_project_run(
                self.project,
                task="Deliver governed change",
                intent="build",
                mode="standard",
                actor_id="local-owner",
                device_id="owner-pc",
            )
        claim_backlog_item(
            self.project,
            item_id=item_id,
            expected_revision=1,
            actor_id="local-owner",
            device_id="owner-pc",
            branch="main",
        )
        started = start_project_run(
            self.project,
            task="Deliver governed change",
            intent="build",
            mode="standard",
            backlog_item_id=item_id,
            actor_id="local-owner",
            device_id="owner-pc",
        )
        manifest = json.loads(
            Path(str(started["manifest_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["governance"]["backlog_item_id"], item_id)
        self.assertEqual(manifest["governance"]["backlog_revision"], 2)

        (self.code / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
        preflight = governance_preflight(
            self.project, operation="write", run_id=str(started["run_id"])
        )
        self.assertTrue(preflight["ok"], preflight)
        self.assertEqual(preflight["state"], "TASK_ACTIVE")

    def test_accepted_decision_reconciliation_is_explicit_and_signed(self) -> None:
        added = add_backlog_item(
            self.project,
            title="Select provider",
            item_type="clarification",
            priority="critical",
            target_versions=["M0"],
            acceptance="The owner accepts the provider and data boundary",
            source_kind="canonical-backlog",
            source_ref="DEC-003",
            requirements=["DEC-003"],
            expected_revision=0,
            actor_id="local-owner",
            device_id="owner-pc",
        )
        item_id = str(added["item"]["id"])
        plan = decision_reconciliation_plan(self.project)
        self.assertEqual([row["item_id"] for row in plan["actions"]], [item_id])
        with self.assertRaisesRegex(WorkflowError, "acceptance confirmation"):
            reconcile_accepted_decisions(
                self.project,
                item_ids=[item_id],
                expected_revision=1,
                actor_id="local-owner",
                device_id="owner-pc",
                version="1.5.3",
                branch="main",
            )

        reconciled = reconcile_accepted_decisions(
            self.project,
            item_ids=[item_id],
            expected_revision=1,
            confirm_acceptance=True,
            actor_id="local-owner",
            device_id="owner-pc",
            version="1.5.3",
            branch="main",
        )
        self.assertEqual(reconciled["reconciled"], [item_id])
        backlog = load_backlog(self.project)
        item = next(row for row in backlog["items"] if row["id"] == item_id)
        self.assertEqual(item["status"], "done")
        self.assertRegex(
            item["completion_evidence"][0], r"^decision:DEC-003:[0-9a-f]{64}$"
        )
        self.assertEqual(backlog["events"][-1]["type"], "accepted_decisions_reconciled")

    def test_reconciliation_rechecks_decision_evidence_before_write(self) -> None:
        added = add_backlog_item(
            self.project,
            title="Select provider",
            item_type="clarification",
            priority="critical",
            target_versions=["M0"],
            acceptance="The owner accepts the provider",
            source_kind="canonical-backlog",
            source_ref="DEC-003-drift",
            requirements=["DEC-003"],
            expected_revision=0,
            actor_id="local-owner",
            device_id="owner-pc",
        )
        item_id = str(added["item"]["id"])
        registry_path = self.code / "docs" / "decisions" / "DECISIONS.md"
        registry_path.write_text(
            registry_path.read_text(encoding="utf-8").replace(
                "DEC-003 | ACCEPTED", "DEC-003 | OPEN"
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(WorkflowError, "not supported"):
            reconcile_accepted_decisions(
                self.project,
                item_ids=[item_id],
                expected_revision=1,
                confirm_acceptance=True,
                actor_id="local-owner",
                device_id="owner-pc",
                version="1.5.3",
                branch="main",
            )
        backlog = load_backlog(self.project)
        self.assertEqual(backlog["revision"], 1)
        self.assertEqual(backlog["items"][0]["status"], "open")

    def test_competing_reconciliation_has_exactly_one_signed_winner(self) -> None:
        added = add_backlog_item(
            self.project,
            title="Select provider",
            item_type="clarification",
            priority="critical",
            target_versions=["M0"],
            acceptance="The owner accepts the provider",
            source_kind="canonical-backlog",
            source_ref="DEC-003-concurrent",
            requirements=["DEC-003"],
            expected_revision=0,
            actor_id="local-owner",
            device_id="owner-pc",
        )
        item_id = str(added["item"]["id"])

        def attempt() -> bool:
            try:
                result = reconcile_accepted_decisions(
                    self.project,
                    item_ids=[item_id],
                    expected_revision=1,
                    confirm_acceptance=True,
                    actor_id="local-owner",
                    device_id="owner-pc",
                    version="1.5.3",
                    branch="main",
                )
                return result["reconciled"] == [item_id]
            except WorkflowError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _index: attempt(), range(2)))
        self.assertEqual(outcomes.count(True), 1)
        backlog = load_backlog(self.project)
        self.assertEqual(backlog["revision"], 2)
        self.assertEqual(backlog["items"][0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
