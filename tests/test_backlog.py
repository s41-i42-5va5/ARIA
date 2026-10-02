from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from aria.access import access_template, bootstrap_access, grant_access
from aria.backlog import (
    add_backlog_item,
    backlog_audit,
    backlog_template,
    block_backlog_item,
    claim_backlog_item,
    complete_backlog_item,
    list_backlog_items,
    load_backlog,
    show_backlog_item,
    sync_backlog,
)
from aria.errors import WorkflowError
from aria.identity import enroll_identity


class BacklogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.docs = root / "docs"
        self.code = root / "code"
        self.runtime = root / "runtime"
        self.docs.mkdir()
        self.code.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "feature/backlog"],
            cwd=self.code,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "tests@example.invalid"],
            cwd=self.code,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "ARIA Tests"],
            cwd=self.code,
            check=True,
        )
        (self.code / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.code, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"], cwd=self.code, check=True
        )
        self.registry = self.runtime / "projects.toml"
        self.registry.parent.mkdir()
        (self.docs / "ARIA_TEAM.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "actors": [
                        {
                            "id": "owner",
                            "display_name": "Owner",
                            "type": "human",
                            "roles": ["maintainer", "release-manager"],
                        },
                        {
                            "id": "bob",
                            "display_name": "Bob",
                            "type": "human",
                            "roles": ["contributor"],
                        },
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (self.docs / "TRUST.yaml").write_text(
            yaml.safe_dump(
                {"schema_version": 1, "keys": [], "policies": {}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (self.docs / "ACCESS.yaml").write_bytes(access_template("demo"))
        (self.docs / "BACKLOG.yaml").write_bytes(backlog_template("demo"))
        self.project = SimpleNamespace(
            project_id="demo",
            docs_root=self.docs,
            code_root=self.code,
            git_ignore_prefixes=(),
            runtime_root=self.runtime / "projects" / "demo",
            registry_path=self.registry,
            files=SimpleNamespace(
                team="ARIA_TEAM.yaml",
                trust="TRUST.yaml",
                access="ACCESS.yaml",
                backlog="BACKLOG.yaml",
            ),
        )
        owner_request = root / "owner.json"
        bob_request = root / "bob.json"
        enroll_identity(
            self.runtime,
            actor_id="owner",
            device_id="owner-pc",
            request_path=owner_request,
        )
        enroll_identity(
            self.runtime,
            actor_id="bob",
            device_id="bob-pc",
            request_path=bob_request,
        )
        bootstrap_access(self.project, actor_id="owner", device_id="owner-pc")
        grant_access(
            self.project,
            request_path=bob_request,
            permissions=[
                "project.read",
                "backlog.read",
                "backlog.write",
                "backlog.claim",
                "backlog.close",
            ],
            versions=["1.*"],
            branches=["feature/*"],
            expected_revision=1,
            admin_actor_id="owner",
            admin_device_id="owner-pc",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _add(self, *, source_ref: str = "user:1"):
        return add_backlog_item(
            self.project,
            title="Deliver signed backlog",
            item_type="feature",
            priority="high",
            target_versions=["1.5"],
            acceptance="One actor owns the item and completion has evidence",
            source_kind="user-request",
            source_ref=source_ref,
            expected_revision=0,
            actor_id="owner",
            device_id="owner-pc",
        )

    def test_signed_lifecycle_deduplication_and_evidence_completion(self) -> None:
        added = self._add()
        item_id = added["item"]["id"]
        duplicate = self._add()
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(duplicate["revision"], 1)

        claimed = claim_backlog_item(
            self.project,
            item_id=item_id,
            expected_revision=1,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        self.assertEqual(claimed["item"]["assignee"], "bob")
        with self.assertRaisesRegex(WorkflowError, "Stale backlog revision"):
            claim_backlog_item(
                self.project,
                item_id=item_id,
                expected_revision=1,
                actor_id="owner",
                device_id="owner-pc",
            )
        blocked = block_backlog_item(
            self.project,
            item_id=item_id,
            reason="Waiting for integration evidence",
            expected_revision=2,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        self.assertEqual(blocked["item"]["status"], "blocked")
        with self.assertRaisesRegex(WorkflowError, "must be in_progress"):
            complete_backlog_item(
                self.project,
                item_id=item_id,
                evidence=["run:demo"],
                expected_revision=3,
                actor_id="bob",
                device_id="bob-pc",
                branch="feature/backlog",
            )

        # An administrator may assign again, but the assignee must claim before closure.
        from aria.backlog import assign_backlog_item

        assign_backlog_item(
            self.project,
            item_id=item_id,
            assignee="bob",
            expected_revision=3,
            actor_id="owner",
            device_id="owner-pc",
        )
        claim_backlog_item(
            self.project,
            item_id=item_id,
            expected_revision=4,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.code,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        completed = complete_backlog_item(
            self.project,
            item_id=item_id,
            evidence=[f"git:{head}"],
            expected_revision=5,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        self.assertEqual(completed["item"]["status"], "done")
        audit = backlog_audit(
            self.project, actor_id="owner", device_id="owner-pc"
        )
        self.assertEqual(audit["revision"], 6)
        self.assertEqual(audit["events"], 6)

    def test_claim_rejects_incomplete_dependencies(self) -> None:
        dependency_id = self._add(source_ref="user:dependency")["item"]["id"]
        dependent = add_backlog_item(
            self.project,
            title="Dependent delivery",
            item_type="feature",
            priority="normal",
            target_versions=["1.5"],
            acceptance="The dependency is completed first",
            source_kind="user-request",
            source_ref="user:dependent",
            dependencies=[dependency_id],
            expected_revision=1,
            actor_id="owner",
            device_id="owner-pc",
        )
        with self.assertRaisesRegex(WorkflowError, "incomplete dependencies"):
            claim_backlog_item(
                self.project,
                item_id=dependent["item"]["id"],
                expected_revision=2,
                actor_id="bob",
                device_id="bob-pc",
                branch="feature/backlog",
            )
        self.assertEqual(load_backlog(self.project)["revision"], 2)
        claim_backlog_item(
            self.project,
            item_id=dependency_id,
            expected_revision=2,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.code,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        complete_backlog_item(
            self.project,
            item_id=dependency_id,
            evidence=[f"git:{head}"],
            expected_revision=3,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        claimed = claim_backlog_item(
            self.project,
            item_id=dependent["item"]["id"],
            expected_revision=4,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        self.assertEqual(claimed["item"]["status"], "in_progress")

    def test_completion_requires_resolvable_run_or_git_evidence(self) -> None:
        item_id = self._add(source_ref="user:verified-evidence")["item"]["id"]
        claim_backlog_item(
            self.project,
            item_id=item_id,
            expected_revision=1,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        with self.assertRaisesRegex(WorkflowError, "Completion evidence must reference"):
            complete_backlog_item(
                self.project,
                item_id=item_id,
                evidence=["manual:trust-me"],
                expected_revision=2,
                actor_id="bob",
                device_id="bob-pc",
                branch="feature/backlog",
            )
        with self.assertRaisesRegex(WorkflowError, "full closure read-back"):
            complete_backlog_item(
                self.project,
                item_id=item_id,
                evidence=["run:missing"],
                expected_revision=2,
                actor_id="bob",
                device_id="bob-pc",
                branch="feature/backlog",
            )
        fabricated_id = "20260730T120000Z-deadbeef"
        fabricated = self.project.runtime_root / "runs" / fabricated_id
        fabricated.mkdir(parents=True)
        (fabricated / "result.json").write_text(
            json.dumps({"status": "completed"}), encoding="utf-8"
        )
        (fabricated / "manifest.json").write_text(
            json.dumps(
                {
                    "project": self.project.project_id,
                    "run_id": fabricated_id,
                    "status": "completed",
                    "result_sha256": "not-a-real-closure",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "contract anchor is missing"):
            complete_backlog_item(
                self.project,
                item_id=item_id,
                evidence=[f"run:{fabricated_id}"],
                expected_revision=2,
                actor_id="bob",
                device_id="bob-pc",
                branch="feature/backlog",
            )
        old_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.code,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (self.code / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "service.py"], cwd=self.code, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "current evidence fixture"],
            cwd=self.code,
            check=True,
        )
        with self.assertRaisesRegex(WorkflowError, "current reachable HEAD"):
            complete_backlog_item(
                self.project,
                item_id=item_id,
                evidence=[f"git:{old_head}"],
                expected_revision=2,
                actor_id="bob",
                device_id="bob-pc",
                branch="feature/backlog",
            )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.code,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (self.code / "service.py").write_text("VALUE = 3\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "clean worktree"):
            complete_backlog_item(
                self.project,
                item_id=item_id,
                evidence=[f"git:{head}"],
                expected_revision=2,
                actor_id="bob",
                device_id="bob-pc",
                branch="feature/backlog",
            )
        (self.code / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
        completed = complete_backlog_item(
            self.project,
            item_id=item_id,
            evidence=[f"git:{head}"],
            expected_revision=2,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        self.assertEqual(completed["item"]["completion_evidence"], [f"git:{head}"])

    def test_state_tamper_fails_signed_head(self) -> None:
        self._add(source_ref="user:tamper")
        path = self.docs / "BACKLOG.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["items"][0]["title"] = "Tampered"
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "signed audit head"):
            load_backlog(self.project)

    def test_competing_claims_produce_exactly_one_owner(self) -> None:
        item_id = self._add(source_ref="user:concurrent")["item"]["id"]

        def claim(actor_id: str, device_id: str) -> str:
            try:
                result = claim_backlog_item(
                    self.project,
                    item_id=item_id,
                    expected_revision=1,
                    actor_id=actor_id,
                    device_id=device_id,
                    branch="feature/backlog",
                )
                return str(result["item"]["assignee"])
            except WorkflowError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda pair: claim(*pair),
                    [("owner", "owner-pc"), ("bob", "bob-pc")],
                )
            )
        winners = [value for value in outcomes if value != "rejected"]
        self.assertEqual(len(winners), 1)
        stored = load_backlog(self.project)["items"][0]
        self.assertEqual(stored["status"], "in_progress")
        self.assertEqual(stored["assignee"], winners[0])

    def test_branch_scoped_user_can_read_audit_and_sync(self) -> None:
        item_id = self._add(source_ref="user:branch-scope")["item"]["id"]
        shown = show_backlog_item(
            self.project,
            item_id=item_id,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        self.assertEqual(shown["item"]["id"], item_id)
        audit = backlog_audit(
            self.project,
            actor_id="bob",
            device_id="bob-pc",
            version="1.5",
            branch="feature/backlog",
        )
        self.assertEqual(audit["events"], 1)
        synced = sync_backlog(
            self.project,
            expected_revision=1,
            actor_id="bob",
            device_id="bob-pc",
            version="1.5",
            branch="feature/backlog",
        )
        self.assertTrue(synced["idempotent"])

    def test_direct_api_rejects_spoofed_branch_context(self) -> None:
        item_id = self._add(source_ref="user:spoofed-branch")["item"]["id"]
        with self.assertRaisesRegex(WorkflowError, "does not match"):
            show_backlog_item(
                self.project,
                item_id=item_id,
                actor_id="bob",
                device_id="bob-pc",
                branch="main",
            )

    def test_transition_restores_preimage_when_readback_fails(self) -> None:
        before = (self.docs / "BACKLOG.yaml").read_bytes()
        from aria.backlog import load_backlog as real_load

        calls = 0

        def fail_second_read(project):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise WorkflowError("simulated read-back failure")
            return real_load(project)

        with patch("aria.backlog.load_backlog", side_effect=fail_second_read):
            with self.assertRaisesRegex(WorkflowError, "simulated read-back failure"):
                self._add(source_ref="user:rollback")

        self.assertEqual((self.docs / "BACKLOG.yaml").read_bytes(), before)
        self.assertEqual(load_backlog(self.project)["revision"], 0)

    def test_transition_restores_preimage_on_semantic_readback_mismatch(self) -> None:
        before = (self.docs / "BACKLOG.yaml").read_bytes()
        from aria.backlog import load_backlog as real_load

        calls = 0

        def mismatch_second_read(project):
            nonlocal calls
            calls += 1
            loaded = real_load(project)
            if calls == 2:
                return {**loaded, "revision": loaded["revision"] + 1}
            return loaded

        with patch("aria.backlog.load_backlog", side_effect=mismatch_second_read):
            with self.assertRaisesRegex(
                WorkflowError, "atomic transition read-back failed"
            ):
                self._add(source_ref="user:semantic-rollback")

        self.assertEqual((self.docs / "BACKLOG.yaml").read_bytes(), before)
        self.assertEqual(load_backlog(self.project)["revision"], 0)

    def test_sync_discovers_run_and_review_finding_idempotently(self) -> None:
        run = self.project.runtime_root / "runs" / "run-1"
        roles = run / "outputs" / "roles"
        roles.mkdir(parents=True)
        receipts = run / "execution" / "receipts"
        receipts.mkdir(parents=True)
        (run / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "task": "Add a weather provider",
                    "status": "started",
                }
            ),
            encoding="utf-8",
        )
        (roles / "reviewer.json").write_text(
            json.dumps(
                {
                    "findings": [
                        {"severity": "high", "summary": "Retry loses correlation id"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        (receipts / "exec-1.json").write_text(
            json.dumps(
                {
                    "execution_id": "exec-1",
                    "command_id": "focused-tests",
                    "status": "failed",
                    "exit_code": 1,
                }
            ),
            encoding="utf-8",
        )
        synced = sync_backlog(
            self.project,
            expected_revision=0,
            actor_id="owner",
            device_id="owner-pc",
        )
        self.assertEqual(len(synced["added"]), 3)
        repeated = sync_backlog(
            self.project,
            expected_revision=1,
            actor_id="owner",
            device_id="owner-pc",
        )
        self.assertTrue(repeated["idempotent"])
        listed = list_backlog_items(
            self.project, actor_id="owner", device_id="owner-pc"
        )
        self.assertEqual(listed["count"], 3)
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        manifest["status"] = "completed"
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "full closure read-back"):
            sync_backlog(
                self.project,
                expected_revision=1,
                actor_id="owner",
                device_id="owner-pc",
            )
        self.assertEqual(load_backlog(self.project)["revision"], 1)

    def test_sync_cannot_complete_run_item_without_backlog_close(self) -> None:
        team_path = self.docs / "ARIA_TEAM.yaml"
        team = yaml.safe_load(team_path.read_text(encoding="utf-8"))
        team["actors"].append(
            {
                "id": "writer",
                "display_name": "Writer",
                "type": "human",
                "roles": ["contributor"],
            }
        )
        team_path.write_text(
            yaml.safe_dump(team, sort_keys=False), encoding="utf-8"
        )
        request = Path(self.temporary.name) / "writer.json"
        enroll_identity(
            self.runtime,
            actor_id="writer",
            device_id="writer-pc",
            request_path=request,
        )
        grant_access(
            self.project,
            request_path=request,
            permissions=["project.read", "backlog.read", "backlog.write"],
            versions=["1.*"],
            branches=["feature/*"],
            expected_revision=2,
            admin_actor_id="owner",
            admin_device_id="owner-pc",
        )
        run = self.project.runtime_root / "runs" / "permission-run"
        run.mkdir(parents=True)
        manifest_path = run / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "run_id": "permission-run",
                    "task": "Permission boundary",
                    "status": "started",
                }
            ),
            encoding="utf-8",
        )
        sync_backlog(
            self.project,
            expected_revision=0,
            actor_id="owner",
            device_id="owner-pc",
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "completed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "backlog.close"):
            sync_backlog(
                self.project,
                expected_revision=1,
                actor_id="writer",
                device_id="writer-pc",
                version="1.5",
                branch="feature/backlog",
            )
        self.assertEqual(load_backlog(self.project)["revision"], 1)

    def test_sync_reconciles_bound_run_without_creating_duplicate_item(self) -> None:
        item_id = self._add(source_ref="canonical:DEV-001")["item"]["id"]
        claim_backlog_item(
            self.project,
            item_id=item_id,
            expected_revision=1,
            actor_id="bob",
            device_id="bob-pc",
            branch="feature/backlog",
        )
        run_id = "20260824T120000Z-a1b2c3d4"
        run = self.project.runtime_root / "runs" / run_id
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "task": "Deliver governed change",
                    "status": "completed",
                    "governance": {
                        "enforced": True,
                        "backlog_item_id": item_id,
                        "actor_id": "bob",
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch(
            "aria.simple_run.verify_completed_project_run",
            return_value={"run_id": run_id},
        ):
            synced = sync_backlog(
                self.project,
                expected_revision=2,
                actor_id="bob",
                device_id="bob-pc",
                version="1.5",
                branch="feature/backlog",
            )
        self.assertEqual(synced["reconciled"], [item_id])
        self.assertEqual(synced["added"], [])
        backlog = load_backlog(self.project)
        self.assertEqual(len(backlog["items"]), 1)
        self.assertEqual(backlog["items"][0]["status"], "done")
        self.assertEqual(backlog["items"][0]["completion_evidence"], [f"run:{run_id}"])


if __name__ == "__main__":
    unittest.main()
