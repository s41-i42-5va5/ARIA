from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from aria.activity import activity_template, dump_activity
from aria.collaboration import build_control_contract, dump_control_contract
from aria.collaborative_activity_runtime import (
    authenticated_activity_status,
    publish_verified_github_activity,
    submit_authenticated_activity,
)
from aria.collaborative_backlog import (
    ProviderIdentity,
    apply_backlog_request,
    collaborative_backlog_template,
    dump_collaborative_backlog,
)
from aria.collaborative_documents import collaborative_access_template
from aria.collaborative_team import (
    collaborative_team_template,
    dump_collaborative_team,
    sync_collaborative_team,
)
from aria.errors import WorkflowError
from aria.github_integration import (
    GitHubBoundOpenPullRequest,
    GitHubBoundPullRequest,
)
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
)
from tests.test_collaborative_backlog import (
    ALL_PERMISSIONS, ARAM, YURA, _request, _triage_payload,
)
from tests.test_collaborative_team import TEAM


COORDINATOR = ProviderIdentity(
    "github-app", ProviderActor("9001", "aria-coordinator", "ARIA Coordinator")
)


class _Adapter:
    provider_id = "github"

    def __init__(self, *, user_id: str = "200", role: str = "contributor") -> None:
        self.actor = next(row.actor for row in TEAM if row.actor.user_id == user_id)
        self.role = role

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return ProviderInspection(
            provider="github",
            repository_id=repository_id,
            actor=self.actor,
            membership=ProviderMembership(True, (self.role,)),
            protection=ProviderBranchProtection(True, False, "aria-coordinator"),
        )


class _CoordinatorAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__(user_id="100", role="admin")

    def list_collaborators(self, *, repository_id: str):
        return TEAM


class CollaborativeActivityRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prepare_patcher = mock.patch(
            "aria.collaborative_activity_runtime.prepare_control_mutation"
        )
        self.prepare_patcher.start()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.docs = root / "control"
        self.docs.mkdir()
        self.runtime = root / "runtime" / "projects" / "demo"
        self.runtime.mkdir(parents=True)
        contract = build_control_contract(
            project_id="demo", provider="github", repository_id="123456789"
        )
        (self.docs / "CONTROL.yaml").write_text(
            dump_control_contract(contract), encoding="utf-8"
        )
        (self.docs / "ACCESS.yaml").write_text(
            yaml.safe_dump(
                collaborative_access_template(contract), sort_keys=False
            ),
            encoding="utf-8",
        )
        team = sync_collaborative_team(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            ),
            TEAM,
            sync_id="team-sync-runtime-0001",
            coordinator=COORDINATOR,
            expected_revision=0,
            checked_at="2026-08-26T10:00:00Z",
        )["team"]
        (self.docs / "ARIA_TEAM.yaml").write_text(
            dump_collaborative_team(team), encoding="utf-8"
        )
        added = apply_backlog_request(
            collaborative_backlog_template("demo"),
            _request(
                request_id="request-add-0001",
                action="add",
                payload={
                    "title": "Работа Юры",
                    "description": "Activity test",
                    "priority": "P1",
                    "source_id": None,
                    "dependencies": [],
                    "evidence_required": False,
                },
            ),
            authenticated_actor=ARAM,
            active_members=(ARAM, YURA),
            permissions=ALL_PERMISSIONS,
            coordinator=COORDINATOR,
            expected_revision=0,
            committed_at="2026-08-26T10:01:00Z",
        )["backlog"]
        assigned = apply_backlog_request(
            added,
            _request(
                request_id="request-triage-0001",
                action="triage",
                item_id="BLG-000001",
                payload=_triage_payload(),
                minute=2,
            ),
            authenticated_actor=ARAM,
            active_members=(ARAM, YURA),
            permissions=ALL_PERMISSIONS,
            coordinator=COORDINATOR,
            expected_revision=1,
            committed_at="2026-08-26T10:02:02Z",
        )["backlog"]
        claimed = apply_backlog_request(
            assigned,
            _request(
                request_id="request-claim-0001",
                action="claim",
                item_id="BLG-000001",
                payload={"branch": "work/yura"},
                minute=3,
            ),
            authenticated_actor=YURA,
            active_members=(ARAM, YURA),
            permissions=ALL_PERMISSIONS,
            coordinator=COORDINATOR,
            expected_revision=2,
            committed_at="2026-08-26T10:03:02Z",
        )["backlog"]
        (self.docs / "BACKLOG.yaml").write_text(
            dump_collaborative_backlog(claimed), encoding="utf-8"
        )
        (self.docs / "ACTIVITY.yaml").write_text(
            dump_activity(activity_template("demo")), encoding="utf-8"
        )
        self.project = SimpleNamespace(
            project_id="demo", docs_root=self.docs, runtime_root=self.runtime
        )

    def tearDown(self) -> None:
        self.prepare_patcher.stop()
        self.temporary.cleanup()

    @mock.patch(
        "aria.collaborative_activity_runtime.synchronize_control_worktree",
        return_value={"control_commit": "c" * 40, "recovered": False},
    )
    def test_assignee_publishes_activity_and_retry_is_idempotent(self, remote) -> None:
        arguments = {
            "adapter": _Adapter(),
            "control_writer": object(),
            "expected_revision": 0,
            "task_id": "BLG-000001",
            "stage": "implementation",
            "branch": "work/yura",
            "note": "Пишу код",
            "observed_at": "2026-08-26T11:00:00Z",
            "received_at": "2026-08-26T11:00:01Z",
        }
        first = submit_authenticated_activity(self.project, **arguments)
        self.assertTrue(first["applied"])
        self.assertRegex(first["event_id"], r"^activity-r1-[0-9a-f]{16}$")
        arguments["observed_at"] = "2026-08-26T11:01:00Z"
        arguments["received_at"] = "2026-08-26T11:01:01Z"
        duplicate = submit_authenticated_activity(self.project, **arguments)
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_request")
        self.assertEqual(duplicate["event_id"], first["event_id"])
        self.assertEqual(remote.call_count, 2)
        mine = authenticated_activity_status(self.project, adapter=_Adapter())
        self.assertEqual(mine["active_work"][0]["stage"], "implementation")
        self.assertEqual(mine["actor"]["user_id"], "200")

    def test_non_assignee_and_viewer_are_rejected(self) -> None:
        common = {
            "control_writer": object(),
            "expected_revision": 0,
            "task_id": "BLG-000001",
            "stage": "analysis",
            "branch": "dev/actor",
            "observed_at": "2026-08-26T11:00:00Z",
            "received_at": "2026-08-26T11:00:01Z",
        }
        with self.assertRaisesRegex(WorkflowError, "only the backlog assignee"):
            submit_authenticated_activity(
                self.project,
                adapter=_Adapter(user_id="100", role="admin"),
                **common,
            )
        with self.assertRaisesRegex(WorkflowError, "activity write is not permitted"):
            submit_authenticated_activity(
                self.project,
                adapter=_Adapter(user_id="200", role="viewer"),
                **common,
            )

    @mock.patch(
        "aria.collaborative_activity_runtime.synchronize_control_worktree",
        return_value={"control_commit": "c" * 40, "recovered": False},
    )
    def test_verified_github_pr_publishes_review_then_ci_stage(self, remote) -> None:
        opened = GitHubBoundOpenPullRequest(
            17,
            "BLG-000001",
            "2026-08-26T11:00:00Z",
            TEAM[1].actor,
            "work/yura",
        )
        review = publish_verified_github_activity(
            self.project,
            coordinator_adapter=_CoordinatorAdapter(),
            candidate=opened,
            stage="in_review",
            expected_revision=0,
            control_writer=object(),
            received_at="2026-08-26T11:00:01Z",
        )
        self.assertTrue(review["applied"])
        merged = GitHubBoundPullRequest(
            17,
            "BLG-000001",
            "2026-08-26T12:00:00Z",
            TEAM[1].actor,
            "work/yura",
        )
        waiting = publish_verified_github_activity(
            self.project,
            coordinator_adapter=_CoordinatorAdapter(),
            candidate=merged,
            stage="waiting_for_ci",
            expected_revision=1,
            control_writer=object(),
            received_at="2026-08-26T12:00:01Z",
        )
        self.assertTrue(waiting["applied"])
        snapshot = yaml.safe_load(
            (self.docs / "ACTIVITY.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["active_work"][0]["stage"], "waiting_for_ci")
        self.assertEqual(snapshot["active_work"][0]["pr_number"], 17)
        self.assertEqual(remote.call_count, 2)

    @mock.patch("aria.collaborative_activity_runtime.synchronize_control_worktree")
    def test_github_pr_author_must_be_backlog_assignee(self, remote) -> None:
        candidate = GitHubBoundOpenPullRequest(
            18,
            "BLG-000001",
            "2026-08-26T11:00:00Z",
            TEAM[2].actor,
            "work/anton",
        )
        with self.assertRaisesRegex(WorkflowError, "not the backlog assignee"):
            publish_verified_github_activity(
                self.project,
                coordinator_adapter=_CoordinatorAdapter(),
                candidate=candidate,
                stage="in_review",
                expected_revision=0,
                control_writer=object(),
            )
        remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
