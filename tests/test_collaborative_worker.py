from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aria.collaborative_worker import run_collaborative_coordinator_once
from aria.errors import WorkflowError
from aria.github_integration import GitHubBoundOpenPullRequest, GitHubBoundPullRequest
from aria.provider import ProviderActor


class _Verifier:
    def list_bound_merged_pull_requests(
        self, *, integration_branch: str, maximum: int, repository_id: str
    ):
        return (
            GitHubBoundPullRequest(17, "BLG-000001", "2026-08-26T12:00:00Z"),
            GitHubBoundPullRequest(18, "BLG-000002", "2026-08-26T13:00:00Z"),
        )


class _ActivityVerifier:
    def list_bound_open_pull_requests(
        self, *, repository_id: str, integration_branch: str, maximum: int
    ):
        return (
            GitHubBoundOpenPullRequest(
                17,
                "BLG-000001",
                "2026-08-26T11:00:00Z",
                ProviderActor("200", "yura"),
                "work/yura",
            ),
        )

    def list_bound_merged_pull_requests(
        self, *, integration_branch: str, maximum: int, repository_id: str
    ):
        return (
            GitHubBoundPullRequest(
                17,
                "BLG-000001",
                "2026-08-26T12:00:00Z",
                ProviderActor("200", "yura"),
                "work/yura",
            ),
        )

    def verify_open(self, **kwargs):
        return GitHubBoundOpenPullRequest(
            17,
            "BLG-000001",
            "2026-08-26T11:00:00Z",
            ProviderActor("200", "yura"),
            "work/yura",
            "a" * 40,
            ("src/export/writer.py",),
        )


class CollaborativeWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = SimpleNamespace(project_id="demo", docs_root=Path("C:/control"))
        self.contract = SimpleNamespace(
            integration_branch="dev", repository_id="123456789"
        )
        self.queue_result = {"ok": True, "processed": []}
        self.state = {
            "revision": 0,
            "events": [],
        }
        self.backlog = {
            "revision": 2,
            "items": [
                {
                    "id": "BLG-000001",
                    "status": "in_progress",
                    "assignee": {"provider": "github", "user_id": "200"},
                    "lease": {
                        "branch": "work/yura",
                        "scope_paths": ["src/export"],
                    },
                }
            ],
        }

    def _run_worker(self):
        with mock.patch(
            "aria.collaborative_worker.recover_pending_state_closure",
            return_value=None,
        ), mock.patch(
            "aria.collaborative_worker.recover_pending_control_sync",
            return_value=None,
        ), mock.patch(
            "aria.collaborative_worker.refresh_control_worktree",
            return_value={"ok": True, "updated": False, "control_commit": "a" * 40},
        ), mock.patch(
            "aria.collaborative_worker.process_github_request_queue",
            return_value=self.queue_result,
        ), mock.patch(
            "aria.collaborative_worker.load_control_contract",
            return_value=self.contract,
        ), mock.patch(
            "aria.collaborative_worker.load_collaborative_state",
            return_value=self.state,
        ), mock.patch(
            "aria.collaborative_worker.load_collaborative_backlog",
            return_value=self.backlog,
        ), mock.patch(
            "aria.collaborative_worker.sync_accepted_pull_request",
            side_effect=[
                {"merge_commit": "a" * 40, "control_commit": "c" * 40},
                {"merge_commit": "b" * 40, "control_commit": "d" * 40},
            ],
        ) as sync:
            result = run_collaborative_coordinator_once(
                self.project,
                coordinator_adapter=object(),
                request_queue=object(),
                control_writer=object(),
                integration_verifier=_Verifier(),
                coordinator_integration_id=9001,
            )
        return result, sync

    def test_run_once_refreshes_requests_and_processes_merged_prs_in_order(self) -> None:
        result, sync = self._run_worker()
        self.assertTrue(result["ok"])
        self.assertEqual(
            [row["pull_request_number"] for row in result["pull_requests"]["processed"]],
            [17, 18],
        )
        self.assertEqual(sync.call_args_list[0].kwargs["item_id"], "BLG-000001")
        self.assertEqual(sync.call_args_list[1].kwargs["item_id"], "BLG-000002")

    def test_already_accepted_pr_is_filtered_before_limit(self) -> None:
        self.state = {
            "revision": 1,
            "events": [{"pull_request_number": 17}],
        }
        result, sync = self._run_worker()
        self.assertEqual(result["pull_requests"]["pending"], 1)
        sync.assert_called_once()
        self.assertEqual(sync.call_args.kwargs["pull_request_number"], 18)

    def test_first_failed_pr_stops_later_state_publication(self) -> None:
        with mock.patch(
            "aria.collaborative_worker.recover_pending_state_closure",
            return_value=None,
        ), mock.patch(
            "aria.collaborative_worker.recover_pending_control_sync",
            return_value=None,
        ), mock.patch(
            "aria.collaborative_worker.refresh_control_worktree",
            return_value={"ok": True, "updated": False, "control_commit": "a" * 40},
        ), mock.patch(
            "aria.collaborative_worker.process_github_request_queue",
            return_value=self.queue_result,
        ), mock.patch(
            "aria.collaborative_worker.load_control_contract", return_value=self.contract
        ), mock.patch(
            "aria.collaborative_worker.load_collaborative_state", return_value=self.state
        ), mock.patch(
            "aria.collaborative_worker.load_collaborative_backlog", return_value=self.backlog
        ), mock.patch(
            "aria.collaborative_worker.sync_accepted_pull_request",
            side_effect=WorkflowError("required check failed"),
        ) as sync:
            result = run_collaborative_coordinator_once(
                self.project,
                coordinator_adapter=object(),
                request_queue=object(),
                control_writer=object(),
                integration_verifier=_Verifier(),
                coordinator_integration_id=9001,
            )
        self.assertFalse(result["ok"])
        sync.assert_called_once()
        self.assertEqual(result["pull_requests"]["processed"][0]["error"], "WorkflowError")

    def test_open_and_merged_pull_requests_publish_verified_activity_stages(self) -> None:
        with mock.patch(
            "aria.collaborative_worker.recover_pending_state_closure",
            return_value=None,
        ), mock.patch(
            "aria.collaborative_worker.recover_pending_control_sync",
            return_value=None,
        ), mock.patch(
            "aria.collaborative_worker.refresh_control_worktree",
            return_value={"ok": True, "updated": False, "control_commit": "a" * 40},
        ), mock.patch(
            "aria.collaborative_worker.process_github_request_queue",
            return_value=self.queue_result,
        ), mock.patch(
            "aria.collaborative_worker.load_control_contract",
            return_value=self.contract,
        ), mock.patch(
            "aria.collaborative_worker.load_collaborative_state",
            return_value=self.state,
        ), mock.patch(
            "aria.collaborative_worker.load_collaborative_backlog",
            return_value=self.backlog,
        ), mock.patch(
            "aria.collaborative_worker.load_activity",
            return_value={"revision": 0, "active_work": []},
        ), mock.patch(
            "aria.collaborative_worker.sync_open_pull_request_review",
            return_value={"ok": True, "applied": True},
        ), mock.patch(
            "aria.collaborative_worker.publish_verified_github_activity",
            side_effect=[
                {"ok": True, "stage": "in_review", "applied": True},
                {"ok": True, "stage": "waiting_for_ci", "applied": True},
            ],
        ) as publish, mock.patch(
            "aria.collaborative_worker.sync_accepted_pull_request",
            return_value={"merge_commit": "a" * 40, "control_commit": "c" * 40},
        ):
            result = run_collaborative_coordinator_once(
                self.project,
                coordinator_adapter=object(),
                request_queue=object(),
                control_writer=object(),
                integration_verifier=_ActivityVerifier(),
                coordinator_integration_id=9001,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(
            [call.kwargs["stage"] for call in publish.call_args_list],
            ["in_review", "waiting_for_ci"],
        )
        self.assertEqual(result["activity"]["open_discovered"], 1)


if __name__ == "__main__":
    unittest.main()
