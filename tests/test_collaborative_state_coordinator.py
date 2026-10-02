from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aria.collaboration import build_control_contract
from aria.collaborative_backlog import ProviderIdentity
from aria.collaborative_documents import build_initial_collaborative_documents
from aria.collaborative_state import load_collaborative_state, validate_collaborative_history
from aria.collaborative_state_coordinator import submit_state_acceptance
from aria.github_integration import GitHubIntegrationAcceptance, RequiredGitHubCheck
from aria.io import atomic_write_bytes as real_atomic_write_bytes
from aria.provider import ProviderActor


class CollaborativeStateCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.control = root / "control"
        self.runtime = root / "runtime"
        self.control.mkdir()
        self.contract = build_control_contract(
            project_id="demo", provider="github", repository_id="123456789"
        )
        documents = build_initial_collaborative_documents(
            self.contract, display_name="Demo"
        ).documents
        for name, content in documents.items():
            (self.control / name).write_text(content, encoding="utf-8")
        actor = ProviderActor("200", "yura", "Yura")
        self.item = {
            "id": "BLG-000001",
            "title": "Accepted capability",
            "status": "done",
            "assignee": ProviderIdentity("github", actor).as_mapping(),
            "evidence_refs": sorted(["github-pr:17", f"git-commit:{'a' * 40}"]),
        }
        self.acceptance = GitHubIntegrationAcceptance(
            repository_id="123456789",
            pull_request_number=17,
            integration_branch="dev",
            merge_commit="a" * 40,
            merged_at="2026-08-26T12:00:00Z",
            pull_request_author=actor,
            required_checks=(RequiredGitHubCheck("ARIA integration", 9001),),
            backlog_item_id="BLG-000001",
            source_branch="work/yura",
            changed_paths=("src/export/writer.py",),
        )
        self.coordinator = ProviderIdentity(
            "github-app", ProviderActor("9001", "aria-coordinator", "ARIA Coordinator")
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def submit(self):
        return submit_state_acceptance(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
            backlog_item=self.item,
            backlog_revision=2,
            acceptance=self.acceptance,
            coordinator=self.coordinator,
            event_id="state-pr-17-aaaaaaaa",
            expected_revision=0,
        )

    def test_state_and_history_commit_together(self) -> None:
        result = self.submit()
        self.assertTrue(result["applied"])
        state = load_collaborative_state(self.control / "STATE.yaml", self.contract)
        history = (self.control / "HISTORY.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(validate_collaborative_history(history, state)), 1)
        self.assertFalse(
            (self.runtime / "collaborative-state" / "demo" / "transaction.json").exists()
        )

    def test_crash_between_state_and_history_recovers_idempotently(self) -> None:
        failed = False

        def fail_before_history(path: Path, content: bytes) -> None:
            nonlocal failed
            if path == self.control / "HISTORY.jsonl" and not failed:
                failed = True
                raise OSError("simulated interruption")
            real_atomic_write_bytes(path, content)

        with mock.patch(
            "aria.collaborative_state_coordinator.atomic_write_bytes",
            side_effect=fail_before_history,
        ):
            with self.assertRaisesRegex(OSError, "simulated interruption"):
                self.submit()
        recovered = self.submit()
        self.assertFalse(recovered["applied"])
        self.assertTrue(recovered["recovered"])
        state = load_collaborative_state(self.control / "STATE.yaml", self.contract)
        history = (self.control / "HISTORY.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(validate_collaborative_history(history, state)), 1)


if __name__ == "__main__":
    unittest.main()
