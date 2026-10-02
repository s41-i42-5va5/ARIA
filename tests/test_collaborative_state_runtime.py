from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aria.activity import load_activity
from aria.activity_coordinator import submit_activity_event
from aria.collaboration import build_control_contract
from aria.collaborative_backlog import ProviderIdentity, load_collaborative_backlog
from aria.collaborative_backlog_coordinator import submit_collaborative_backlog_request
from aria.collaborative_documents import build_initial_collaborative_documents
from aria.collaborative_state_runtime import sync_accepted_pull_request
from aria.collaborative_team import (
    collaborative_team_template,
    dump_collaborative_team,
    sync_collaborative_team,
)
from aria.errors import WorkflowError
from aria.github_integration import GitHubIntegrationAcceptance, RequiredGitHubCheck
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
)
from tests.test_collaborative_team import TEAM
from tests.test_collaborative_backlog import _triage_payload


COORDINATOR = ProviderIdentity(
    "github-app", ProviderActor("9001", "aria-coordinator", "ARIA Coordinator")
)


class _Adapter:
    provider_id = "github"

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return ProviderInspection(
            provider="github",
            repository_id=repository_id,
            actor=TEAM[0].actor,
            membership=ProviderMembership(True, ("admin",)),
            protection=ProviderBranchProtection(True, False, "aria-coordinator"),
        )

    def list_collaborators(self, *, repository_id: str):
        return TEAM


class _Verifier:
    def __init__(self, *, fail: bool = False, changed_paths=None) -> None:
        self.fail = fail
        self.changed_paths = changed_paths or (
            "src/export/writer.py",
            "tests/export/test_writer.py",
        )

    def verify(
        self,
        *,
        repository_id: str,
        integration_branch: str,
        pull_request_number: int,
        previous_accepted_head: str | None = None,
    ):
        if self.fail:
            raise WorkflowError("required integration check did not pass")
        return GitHubIntegrationAcceptance(
            repository_id=repository_id,
            pull_request_number=pull_request_number,
            integration_branch=integration_branch,
            merge_commit="a" * 40,
            merged_at="2026-08-26T12:00:00Z",
            pull_request_author=TEAM[1].actor,
            required_checks=(RequiredGitHubCheck("ARIA integration", 9001),),
            backlog_item_id="BLG-000001",
            source_branch="work/yura",
            changed_paths=tuple(self.changed_paths),
        )


class CollaborativeStateRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.docs = root / "control"
        self.docs.mkdir()
        self.runtime = root / "runtime" / "projects" / "demo"
        self.runtime.mkdir(parents=True)
        contract = build_control_contract(
            project_id="demo", provider="github", repository_id="123456789"
        )
        documents = build_initial_collaborative_documents(
            contract, display_name="Demo"
        ).documents
        for name, content in documents.items():
            (self.docs / name).write_text(content, encoding="utf-8")
        team = sync_collaborative_team(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            ),
            TEAM,
            sync_id="team-sync-state-runtime-0001",
            coordinator=COORDINATOR,
            expected_revision=0,
            checked_at="2026-08-26T10:00:00Z",
        )["team"]
        (self.docs / "ARIA_TEAM.yaml").write_text(
            dump_collaborative_team(team), encoding="utf-8"
        )
        self.project = SimpleNamespace(
            project_id="demo",
            docs_root=self.docs,
            code_root=root / "code",
            runtime_root=self.runtime,
            collaboration_mode="collaborative",
        )
        self.machine_runtime = root / "runtime"
        self._backlog_request(
            actor=ProviderIdentity("github", TEAM[0].actor),
            permissions=frozenset({"backlog.add"}),
            expected=0,
            request_id="backlog-add-state-0001",
            action="add",
            item_id=None,
            payload={
                "title": "Accepted capability",
                "description": "Merged through dev",
                "priority": "P0",
                "source_id": None,
                "dependencies": [],
                "evidence_required": True,
            },
        )
        self._backlog_request(
            actor=ProviderIdentity("github", TEAM[0].actor),
            permissions=frozenset({"backlog.triage"}),
            expected=1,
            request_id="backlog-triage-state-0001",
            action="triage",
            item_id="BLG-000001",
            payload=_triage_payload(),
        )
        self._backlog_request(
            actor=ProviderIdentity("github", TEAM[1].actor),
            permissions=frozenset({"backlog.claim"}),
            expected=2,
            request_id="backlog-claim-state-0001",
            action="claim",
            item_id="BLG-000001",
            payload={"branch": "work/yura"},
        )
        self._backlog_request(
            actor=ProviderIdentity("github", TEAM[1].actor),
            permissions=frozenset({"backlog.complete"}),
            expected=3,
            request_id="backlog-review-state-0001",
            action="review",
            item_id="BLG-000001",
            payload={
                "pull_request": 17,
                "head_commit": "a" * 40,
                "source_branch": "work/yura",
                "changed_paths": ["src/export/writer.py", "tests/export/test_writer.py"],
            },
            acceptance_verified=True,
        )
        self._publish_waiting_activity()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _backlog_request(
        self, *, actor, permissions, expected, request_id, action, item_id, payload,
        acceptance_verified=False,
    ):
        return submit_collaborative_backlog_request(
            control_root=self.docs,
            runtime_root=self.machine_runtime,
            project_id="demo",
            request_value={
                "schema_version": 1,
                "request_id": request_id,
                "correlation_id": f"correlation-{request_id}",
                "project_id": "demo",
                "action": action,
                "item_id": item_id,
                "payload": payload,
                "requested_at": "2026-08-26T11:00:00Z",
            },
            authenticated_actor=actor,
            active_members=tuple(ProviderIdentity("github", member.actor) for member in TEAM),
            permissions=permissions,
            coordinator=COORDINATOR,
            expected_revision=expected,
            committed_at="2026-08-26T11:00:01Z",
            acceptance_verified=acceptance_verified,
        )

    def _sync(self, verifier=None):
        return sync_accepted_pull_request(
            self.project,
            coordinator_adapter=_Adapter(),
            verifier=verifier or _Verifier(),
            control_writer=object(),
            coordinator_integration_id=9001,
            item_id="BLG-000001",
            pull_request_number=17,
            expected_backlog_revision=4,
            expected_state_revision=0,
            committed_at="2026-08-26T12:00:01Z",
        )

    def _publish_waiting_activity(self) -> None:
        submit_activity_event(
            control_root=self.docs,
            runtime_root=self.machine_runtime,
            project_id="demo",
            event_value={
                "schema_version": 1,
                "event_id": "github-pr-17-waiting-for-ci-BLG-000001",
                "project_id": "demo",
                "task_id": "BLG-000001",
                "actor": {
                    "provider": "github",
                    "user_id": TEAM[1].actor.user_id,
                    "username_snapshot": TEAM[1].actor.username_snapshot,
                },
                "source": "github",
                "stage": "waiting_for_ci",
                "branch": "work/yura",
                "pr_number": 17,
                "note": None,
                "observed_at": "2026-08-26T12:00:00Z",
            },
            request_id="github-pr-17-waiting-for-ci-BLG-000001",
            correlation_id="correlation-github-pr-17-waiting-for-ci-BLG-000001",
            expected_revision=0,
            authorized_source="github",
            authorized_provider="github",
            authorized_actor=TEAM[1].actor,
            received_at="2026-08-26T12:00:00Z",
        )

    @mock.patch(
        "aria.collaborative_state_runtime.synchronize_control_worktree",
        return_value={"control_commit": "c" * 40, "recovered": False},
    )
    def test_passed_merge_closes_backlog_and_publishes_state(self, remote) -> None:
        result = self._sync()
        self.assertTrue(result["backlog_applied"])
        self.assertTrue(result["state_applied"])
        self.assertEqual(result["backlog_revision"], 5)
        self.assertEqual(result["state_revision"], 1)
        backlog = load_collaborative_backlog(self.docs / "BACKLOG.yaml")
        self.assertEqual(backlog["items"][0]["status"], "done")
        self.assertIn("github-pr:17", backlog["items"][0]["evidence_refs"])
        remote.assert_called_once()

    @mock.patch(
        "aria.collaborative_state_runtime.synchronize_control_worktree",
        return_value={"control_commit": "c" * 40, "recovered": False},
    )
    def test_passed_merge_completes_matching_waiting_activity(self, remote) -> None:
        result = self._sync()
        self.assertTrue(result["activity_applied"])
        self.assertEqual(result["activity_revision"], 2)
        self.assertEqual(load_activity(self.docs / "ACTIVITY.yaml")["active_work"], [])
        remote.assert_called_once()

    @mock.patch("aria.collaborative_state_runtime.synchronize_control_worktree")
    def test_failed_checks_change_neither_backlog_nor_state(self, remote) -> None:
        backlog_before = (self.docs / "BACKLOG.yaml").read_bytes()
        state_before = (self.docs / "STATE.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "did not pass"):
            self._sync(_Verifier(fail=True))
        self.assertEqual((self.docs / "BACKLOG.yaml").read_bytes(), backlog_before)
        self.assertEqual((self.docs / "STATE.yaml").read_bytes(), state_before)
        remote.assert_not_called()

    @mock.patch("aria.collaborative_state_runtime.synchronize_control_worktree")
    def test_changed_path_outside_scope_changes_neither_backlog_nor_state(self, remote) -> None:
        backlog_before = (self.docs / "BACKLOG.yaml").read_bytes()
        state_before = (self.docs / "STATE.yaml").read_bytes()
        with self.assertRaisesRegex(WorkflowError, "outside task scope"):
            self._sync(_Verifier(changed_paths=("README.md",)))
        self.assertEqual((self.docs / "BACKLOG.yaml").read_bytes(), backlog_before)
        self.assertEqual((self.docs / "STATE.yaml").read_bytes(), state_before)
        remote.assert_not_called()

    def test_remote_failure_retries_without_duplicate_events(self) -> None:
        with mock.patch(
            "aria.collaborative_state_runtime.synchronize_control_worktree",
            side_effect=WorkflowError("remote unavailable"),
        ):
            with self.assertRaisesRegex(WorkflowError, "remote unavailable"):
                self._sync()
        with mock.patch(
            "aria.collaborative_state_runtime.synchronize_control_worktree",
            return_value={"control_commit": "c" * 40, "recovered": True},
        ):
            result = self._sync()
        self.assertFalse(result["backlog_applied"])
        self.assertFalse(result["state_applied"])
        backlog = load_collaborative_backlog(self.docs / "BACKLOG.yaml")
        self.assertEqual(len(backlog["events"]), 5)

    def test_crash_after_state_resumes_activity_before_remote_publication(self) -> None:
        with mock.patch(
            "aria.collaborative_state_runtime.submit_activity_event",
            side_effect=OSError("simulated crash before activity closure"),
        ), mock.patch(
            "aria.collaborative_state_runtime.synchronize_control_worktree"
        ) as remote:
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self._sync()
            remote.assert_not_called()
        with mock.patch(
            "aria.collaborative_state_runtime.synchronize_control_worktree",
            return_value={"control_commit": "c" * 40, "recovered": True},
        ):
            result = self._sync()
        self.assertTrue(result["activity_applied"])
        self.assertEqual(load_activity(self.docs / "ACTIVITY.yaml")["active_work"], [])
        self.assertFalse((self.runtime / "state-closure" / "pending.json").exists())

    def test_crash_after_activity_commit_promotes_marker_and_recovers(self) -> None:
        from aria import collaborative_state_runtime as runtime_module

        original_write = runtime_module.atomic_write_bytes

        def crash_before_documents_complete(path, content):
            try:
                payload = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            if payload.get("phase") == "documents_complete":
                raise OSError("simulated crash after activity commit")
            return original_write(path, content)

        with mock.patch.object(
            runtime_module, "atomic_write_bytes", side_effect=crash_before_documents_complete
        ), mock.patch(
            "aria.collaborative_state_runtime.synchronize_control_worktree"
        ) as remote:
            with self.assertRaisesRegex(OSError, "after activity commit"):
                self._sync()
            remote.assert_not_called()
        with mock.patch(
            "aria.collaborative_state_runtime.synchronize_control_worktree",
            return_value={"control_commit": "c" * 40, "recovered": True},
        ):
            result = self._sync()
        self.assertEqual(result["activity_reason"], "already_accepted")
        self.assertFalse((self.runtime / "state-closure" / "pending.json").exists())


if __name__ == "__main__":
    unittest.main()
