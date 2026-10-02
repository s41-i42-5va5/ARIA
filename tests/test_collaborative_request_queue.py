from __future__ import annotations

import tempfile
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from aria.activity_outbox import (
    activity_outbox_status,
    flush_activity_outbox,
    queue_request_matches_projection,
)
from aria.activity import activity_template, dump_activity, load_activity
from aria.activity_coordinator import activity_coordinator_paths, submit_activity_event
from aria.collaboration import build_control_contract, dump_control_contract
from aria.collaborative_backlog import (
    ProviderIdentity,
    apply_backlog_request,
    collaborative_backlog_template,
    dump_collaborative_backlog,
)
from aria.collaborative_documents import collaborative_access_template
from aria.collaborative_request_queue import (
    _control_commit_has_request,
    enqueue_activity,
    enqueue_backlog_action,
    process_github_request_queue,
)
from aria.collaborative_team import (
    collaborative_team_template,
    dump_collaborative_team,
    sync_collaborative_team,
)
from aria.errors import ConfigurationError, ProviderAdapterError, WorkflowError
from aria.github import GitHubRepository
from aria.github_request_queue import GitHubRequestQueue, load_queue_request
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
)
from tests.test_collaborative_team import TEAM
from tests.test_collaborative_backlog import (
    ALL_PERMISSIONS, ARAM, YURA, _request, _triage_payload,
)
from tests.test_github_request_queue import _Transport


COORDINATOR = ProviderIdentity(
    "github-app", ProviderActor("9001", "aria-coordinator", "ARIA Coordinator")
)


class _Adapter:
    provider_id = "github"

    def __init__(self, *, user_id: str, role: str) -> None:
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

    def list_collaborators(self, *, repository_id: str):
        return TEAM


class _OfflineAdapter(_Adapter):
    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        raise ProviderAdapterError("network unavailable")


class _OfflineQueue:
    def submit(self, request, *, expected_actor):
        raise ProviderAdapterError("network unavailable")


class CollaborativeRequestQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.docs = root / "control"
        self.docs.mkdir()
        runtime = root / "runtime" / "projects" / "demo"
        runtime.mkdir(parents=True)
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
                request_id="request-add-queue-0001",
                action="add",
                payload={
                    "title": "Работа Юры",
                    "description": "Activity queue test",
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
                request_id="request-triage-queue-0001",
                action="triage",
                item_id="BLG-000001",
                payload=_triage_payload(),
            ),
            authenticated_actor=ARAM,
            active_members=(ARAM, YURA),
            permissions=ALL_PERMISSIONS,
            coordinator=COORDINATOR,
            expected_revision=1,
            committed_at="2026-08-26T10:02:00Z",
        )["backlog"]
        claimed = apply_backlog_request(
            assigned,
            _request(
                request_id="request-claim-queue-0001",
                action="claim",
                item_id="BLG-000001",
                payload={"branch": "work/yura"},
            ),
            authenticated_actor=YURA,
            active_members=(ARAM, YURA),
            permissions=ALL_PERMISSIONS,
            coordinator=COORDINATOR,
            expected_revision=2,
            committed_at="2026-08-26T10:03:00Z",
        )["backlog"]
        (self.docs / "BACKLOG.yaml").write_text(
            dump_collaborative_backlog(claimed), encoding="utf-8"
        )
        (self.docs / "ACTIVITY.yaml").write_text(
            dump_activity(activity_template("demo")), encoding="utf-8"
        )
        (self.docs / "PROJECT.yaml").write_text(
            yaml.safe_dump(
                {
                    "repository": {
                        "required_checks": [
                            {"context": "ARIA integration", "app_id": 9001}
                        ]
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.project = SimpleNamespace(
            project_id="demo", docs_root=self.docs, runtime_root=runtime
        )
        self.transport = _Transport()
        self.queue = GitHubRequestQueue(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_developer_enqueue_and_coordinator_process_end_to_end(self) -> None:
        submitted = enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            action="add",
            item_id=None,
            payload={
                "title": "Идея Юры",
                "description": "Без App key на ПК Юры",
                "priority": "P1",
                "source_id": None,
                "dependencies": [],
                "evidence_required": False,
            },
            submitted_at="2026-08-26T12:00:00Z",
        )
        self.assertEqual(submitted["issue_number"], 1)
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action",
            return_value={
                "applied": True,
                "control_commit": "c" * 40,
            },
        ) as apply, mock.patch(
            "aria.collaborative_request_queue._control_commit_has_request",
            return_value=True,
        ):
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(self.queue.read(1).state, "closed")
        inspection = apply.call_args.kwargs["adapter"].inspect_collaboration(
            repository_id="123456789", control_branch="aria-control"
        )
        self.assertEqual(inspection.actor.user_id, "200")
        self.assertEqual(apply.call_args.kwargs["request_id"], submitted["request_id"])
        self.assertIn("body_sha256=", self.transport.comments[0][1])

    def test_receipt_commit_is_ancestor_with_request_proof_at_that_commit(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.docs)], check=True)
        subprocess.run(
            ["git", "-C", str(self.docs), "config", "user.name", "ARIA Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.docs), "config", "user.email", "aria@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.docs), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.docs), "commit", "-q", "-m", "control proof"],
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(self.docs), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        subprocess.run(
            [
                "git", "-C", str(self.docs), "update-ref",
                "refs/remotes/origin/aria-control", commit,
            ],
            check=True,
        )
        request = {
            "schema_version": 1,
            "request_id": "request-claim-queue-0001",
            "project_id": "demo",
            "kind": "backlog",
            "expected_revision": 2,
            "operation": {
                "action": "claim",
                "item_id": "BLG-000001",
                "payload": {"branch": "work/yura"},
            },
            "submitted_at": "2026-08-26T10:00:00Z",
        }
        self.assertTrue(
            _control_commit_has_request(
                self.project,
                control_commit=commit,
                request=request,
                expected_actor=YURA.actor,
            )
        )
        changed = {
            **request,
            "expected_revision": 999,
            "operation": {
                "action": "cancel",
                "item_id": "BLG-000001",
                "payload": {"reason": "different request content"},
            },
        }
        self.assertFalse(
            _control_commit_has_request(
                self.project,
                control_commit=commit,
                request=changed,
                expected_actor=YURA.actor,
            )
        )

    def test_projection_proof_binds_content_revision_timestamp_and_actor(self) -> None:
        request = {
            "schema_version": 1,
            "request_id": "request-claim-queue-0001",
            "project_id": "demo",
            "kind": "backlog",
            "expected_revision": 2,
            "operation": {
                "action": "claim",
                "item_id": "BLG-000001",
                "payload": {"branch": "work/yura"},
            },
            "submitted_at": "2026-08-26T10:00:00Z",
        }
        projection = yaml.safe_load((self.docs / "BACKLOG.yaml").read_text(encoding="utf-8"))
        self.assertTrue(
            queue_request_matches_projection(
                self.project,
                request,
                expected_actor=YURA.actor,
                projection_value=projection,
            )
        )
        variants = (
            {**request, "expected_revision": 999},
            {**request, "submitted_at": "2026-08-26T10:00:01Z"},
            {
                **request,
                "operation": {
                    "action": "cancel",
                    "item_id": "BLG-000001",
                    "payload": {"reason": "different request content"},
                },
            },
        )
        for variant in variants:
            self.assertFalse(
                queue_request_matches_projection(
                    self.project,
                    variant,
                    expected_actor=YURA.actor,
                    projection_value=projection,
                )
            )
        self.assertFalse(
            queue_request_matches_projection(
                self.project,
                request,
                expected_actor=ARAM.actor,
                projection_value=projection,
            )
        )
        with self.assertRaises(ConfigurationError):
            queue_request_matches_projection(
                self.project,
                {**request, "operation": {"proof": True}},
                expected_actor=YURA.actor,
                projection_value=projection,
            )

    def test_reused_request_id_with_different_action_is_rejected_not_accepted(self) -> None:
        submitted = enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=999,
            action="block",
            item_id="BLG-000001",
            payload={"reason": "different request content"},
            request_id="request-claim-queue-0001",
            submitted_at="2026-08-26T12:00:00Z",
        )
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action",
            side_effect=WorkflowError("backlog request id was reused with different content"),
        ) as apply:
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["processed"][0]["rejected"])
        self.assertEqual(
            self.queue.read(int(submitted["issue_number"])).state_reason,
            "not_planned",
        )
        apply.assert_called_once()

    def test_activity_projection_proof_uses_full_durable_request_fingerprint(self) -> None:
        request = {
            "schema_version": 1,
            "request_id": "activity-r1-aaaaaaaaaaaaaaaa",
            "project_id": "demo",
            "kind": "activity",
            "expected_revision": 0,
            "operation": {
                "task_id": "BLG-000001",
                "stage": "testing",
                "branch": "work/yura",
                "note": None,
            },
            "submitted_at": "2026-08-26T12:00:00Z",
        }
        event = {
            "schema_version": 1,
            "event_id": request["request_id"],
            "project_id": "demo",
            "task_id": "BLG-000001",
            "actor": {
                "provider": "github",
                "user_id": YURA.actor.user_id,
                "username_snapshot": YURA.actor.username_snapshot,
            },
            "source": "local_aria",
            "stage": "testing",
            "branch": "work/yura",
            "pr_number": None,
            "note": None,
            "observed_at": request["submitted_at"],
        }
        submit_activity_event(
            control_root=self.docs,
            runtime_root=self.project.runtime_root.parent.parent,
            project_id="demo",
            event_value=event,
            request_id=str(request["request_id"]),
            correlation_id=f"correlation-{request['request_id']}",
            expected_revision=0,
            authorized_source="local_aria",
            authorized_provider="github",
            authorized_actor=YURA.actor,
            received_at="2026-08-26T12:00:02Z",
        )
        projection = load_activity(self.docs / "ACTIVITY.yaml")
        self.assertTrue(
            queue_request_matches_projection(
                self.project,
                request,
                expected_actor=YURA.actor,
                projection_value=projection,
            )
        )
        renamed = ProviderActor(YURA.actor.user_id, "yura-renamed")
        self.assertTrue(
            queue_request_matches_projection(
                self.project,
                request,
                expected_actor=renamed,
                projection_value=projection,
            )
        )
        variants = (
            {**request, "expected_revision": 1},
            {**request, "submitted_at": "2026-08-26T12:00:01Z"},
            {
                **request,
                "operation": {**request["operation"], "stage": "implementation"},
            },
        )
        for variant in variants:
            self.assertFalse(
                queue_request_matches_projection(
                    self.project,
                    variant,
                    expected_actor=YURA.actor,
                    projection_value=projection,
                )
            )
        self.assertFalse(
            queue_request_matches_projection(
                self.project,
                request,
                expected_actor=ARAM.actor,
                projection_value=projection,
            )
        )

    def test_legacy_activity_app_receipt_uses_historical_entry_without_reapply(self) -> None:
        submitted = enqueue_activity(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            task_id="BLG-000001",
            stage="testing",
            branch="work/yura",
            event_id="activity-r1-bbbbbbbbbbbbbbbb",
            submitted_at="2026-08-26T12:00:00Z",
        )
        issue = self.queue.read(int(submitted["issue_number"]))
        request = load_queue_request(issue.body)
        event = {
            "schema_version": 1,
            "event_id": request["request_id"],
            "project_id": "demo",
            "task_id": "BLG-000001",
            "actor": {
                "provider": "github",
                "user_id": YURA.actor.user_id,
                "username_snapshot": YURA.actor.username_snapshot,
            },
            "source": "local_aria",
            "stage": "testing",
            "branch": "work/yura",
            "pr_number": None,
            "note": None,
            "observed_at": request["submitted_at"],
        }
        submit_activity_event(
            control_root=self.docs,
            runtime_root=self.project.runtime_root.parent.parent,
            project_id="demo",
            event_value=event,
            request_id=str(request["request_id"]),
            correlation_id=f"correlation-{request['request_id']}",
            expected_revision=0,
            authorized_source="local_aria",
            authorized_provider="github",
            authorized_actor=YURA.actor,
            received_at="2026-08-26T12:00:02Z",
        )
        paths = activity_coordinator_paths(
            control_root=self.docs,
            runtime_root=self.project.runtime_root.parent.parent,
            project_id="demo",
        )
        receipts = json.loads(paths.receipts.read_text(encoding="utf-8"))
        receipts["receipts"][0].pop("request_fingerprint")
        paths.receipts.write_text(
            json.dumps(receipts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(self.docs)], check=True)
        subprocess.run(
            ["git", "-C", str(self.docs), "config", "user.name", "ARIA Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.docs), "config", "user.email", "aria@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.docs), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.docs), "commit", "-q", "-m", "legacy activity"],
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(self.docs), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "-C",
                str(self.docs),
                "update-ref",
                "refs/remotes/origin/aria-control",
                commit,
            ],
            check=True,
        )
        self.queue.comment(
            issue.number,
            "ARIA accepted "
            f"request_id={request['request_id']} body_sha256={issue.body_sha256} "
            f"control_commit={commit}",
        )
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_activity"
        ) as apply:
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertTrue(result["processed"][0]["recovered"])
        self.assertEqual(self.queue.read(issue.number).state_reason, "completed")
        apply.assert_not_called()
        repeated = process_github_request_queue(
            self.project,
            coordinator_adapter=_Adapter(user_id="100", role="admin"),
            queue=self.queue,
            control_writer=object(),
            coordinator_integration_id=9001,
        )
        self.assertEqual(repeated["processed"], [])
        changed = {
            **request,
            "operation": {**request["operation"], "stage": "implementation"},
        }
        self.assertFalse(
            _control_commit_has_request(
                self.project,
                control_commit=commit,
                request=changed,
                expected_actor=YURA.actor,
                allow_legacy_activity_receipt=True,
            )
        )
        self.assertFalse(
            _control_commit_has_request(
                self.project,
                control_commit=commit,
                request=request,
                expected_actor=ARAM.actor,
                allow_legacy_activity_receipt=True,
            )
        )

    def test_archived_legacy_activity_receipt_matches_historical_projection(self) -> None:
        runtime_root = self.project.runtime_root.parent.parent

        def submit(number: int, stage: str) -> tuple[dict[str, object], dict[str, object]]:
            request = {
                "schema_version": 1,
                "request_id": f"activity-r{number}-{number:016x}",
                "project_id": "demo",
                "kind": "activity",
                "expected_revision": number - 1,
                "operation": {
                    "task_id": "BLG-000001",
                    "stage": stage,
                    "branch": "work/yura",
                    "note": None,
                },
                "submitted_at": f"2026-08-26T12:0{number}:00Z",
            }
            event = {
                "schema_version": 1,
                "event_id": request["request_id"],
                "project_id": "demo",
                "task_id": "BLG-000001",
                "actor": {
                    "provider": "github",
                    "user_id": YURA.actor.user_id,
                    "username_snapshot": YURA.actor.username_snapshot,
                },
                "source": "local_aria",
                "stage": stage,
                "branch": "work/yura",
                "pr_number": None,
                "note": None,
                "observed_at": request["submitted_at"],
            }
            submit_activity_event(
                control_root=self.docs,
                runtime_root=runtime_root,
                project_id="demo",
                event_value=event,
                request_id=str(request["request_id"]),
                correlation_id=f"correlation-{request['request_id']}",
                expected_revision=number - 1,
                authorized_source="local_aria",
                authorized_provider="github",
                authorized_actor=YURA.actor,
                received_at=f"2026-08-26T12:0{number}:01Z",
            )
            return request, load_activity(self.docs / "ACTIVITY.yaml")

        first, first_projection = submit(1, "analysis")
        submit(2, "implementation")
        paths = activity_coordinator_paths(
            control_root=self.docs,
            runtime_root=runtime_root,
            project_id="demo",
        )
        receipts = json.loads(paths.receipts.read_text(encoding="utf-8"))
        receipts["receipts"][0].pop("request_fingerprint")
        paths.receipts.write_text(
            json.dumps(receipts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        with mock.patch("aria.activity_coordinator.MAX_ACTIVE_RECEIPTS", 1), mock.patch(
            "aria.activity_coordinator.TARGET_ACTIVE_RECEIPTS", 0
        ):
            submit(3, "testing")
        archive = json.loads(paths.archive.read_text(encoding="utf-8"))
        archived = next(
            event["receipt"]
            for event in archive["events"]
            if event["receipt"]["request_id"] == first["request_id"]
        )
        self.assertNotIn("request_fingerprint", archived)
        self.assertFalse(
            queue_request_matches_projection(
                self.project,
                first,
                expected_actor=YURA.actor,
                projection_value=first_projection,
            )
        )
        self.assertTrue(
            queue_request_matches_projection(
                self.project,
                first,
                expected_actor=YURA.actor,
                projection_value=first_projection,
                allow_legacy_activity_receipt=True,
            )
        )
        self.assertFalse(
            queue_request_matches_projection(
                self.project,
                {**first, "submitted_at": "2026-08-26T12:01:30Z"},
                expected_actor=YURA.actor,
                projection_value=first_projection,
                allow_legacy_activity_receipt=True,
            )
        )
        self.assertFalse(
            queue_request_matches_projection(
                self.project,
                first,
                expected_actor=ARAM.actor,
                projection_value=first_projection,
                allow_legacy_activity_receipt=True,
            )
        )
    def test_activity_enqueue_never_requires_coordinator_app_key(self) -> None:
        result = enqueue_activity(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=3,
            task_id="BLG-000001",
            stage="testing",
            branch="work/yura",
            submitted_at="2026-08-26T12:01:00Z",
        )
        self.assertEqual(result["delivery"], "github-issue-queue")
        self.assertRegex(result["event_id"], r"^activity-r4-[0-9a-f]{16}$")

    def test_activity_offline_outbox_flushes_with_same_authenticated_actor(self) -> None:
        queued = enqueue_activity(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=_OfflineQueue(),
            expected_revision=3,
            task_id="BLG-000001",
            stage="testing",
            branch="work/yura",
            submitted_at="2026-08-26T12:01:00Z",
        )
        self.assertEqual(queued["delivery"], "local-runtime-outbox")
        self.assertEqual(activity_outbox_status(self.project)["pending"], 1)
        flushed = flush_activity_outbox(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
        )
        self.assertEqual(flushed["pending"], 1)
        self.assertEqual(flushed["sync_status"], "pending_sync")
        self.assertEqual(flushed["delivered"][0]["request_id"], queued["event_id"])
        self.assertEqual(self.queue.read(1).author.user_id, "200")
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_activity",
            return_value={"applied": True, "control_commit": "d" * 40},
        ), mock.patch(
            "aria.collaborative_request_queue._control_commit_has_request",
            return_value=True,
        ):
            process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertEqual(activity_outbox_status(self.project)["pending"], 1)
        with mock.patch(
            "aria.activity_outbox.queue_request_applied", return_value=True
        ):
            accepted = flush_activity_outbox(
                self.project,
                adapter=_Adapter(user_id="200", role="contributor"),
                queue=self.queue,
            )
        self.assertEqual(accepted["pending"], 0)
        self.assertEqual(accepted["sync_status"], "accepted")

    def test_cached_identity_queues_when_provider_is_offline(self) -> None:
        enqueue_activity(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=3,
            task_id="BLG-000001",
            stage="testing",
            branch="work/yura",
            submitted_at="2026-08-26T12:01:00Z",
        )
        queued = enqueue_activity(
            self.project,
            adapter=_OfflineAdapter(user_id="200", role="contributor"),
            queue=_OfflineQueue(),
            expected_revision=4,
            task_id="BLG-000001",
            stage="ready_for_pr",
            branch="work/yura",
            submitted_at="2026-08-26T12:02:00Z",
        )
        self.assertEqual(queued["delivery"], "local-runtime-outbox")
        self.assertEqual(activity_outbox_status(self.project)["pending"], 1)

    def test_backlog_request_uses_same_durable_outbox_while_offline(self) -> None:
        enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=3,
            action="add",
            item_id=None,
            payload={
                "title": "Online identity cache",
                "description": "Cache the authenticated actor",
                "priority": "P1",
                "source_id": None,
                "dependencies": [],
                "evidence_required": False,
            },
            submitted_at="2026-08-26T12:01:00Z",
        )
        queued = enqueue_backlog_action(
            self.project,
            adapter=_OfflineAdapter(user_id="200", role="contributor"),
            queue=_OfflineQueue(),
            expected_revision=4,
            action="add",
            item_id=None,
            payload={
                "title": "Offline durable idea",
                "description": "Must survive until GitHub returns",
                "priority": "P1",
                "source_id": None,
                "dependencies": [],
                "evidence_required": False,
            },
            submitted_at="2026-08-26T12:02:00Z",
        )
        self.assertEqual(queued["delivery"], "local-runtime-outbox")
        self.assertEqual(activity_outbox_status(self.project)["pending"], 1)

    def test_outbox_flush_rejects_a_different_github_session(self) -> None:
        enqueue_activity(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=_OfflineQueue(),
            expected_revision=3,
            task_id="BLG-000001",
            stage="testing",
            branch="work/yura",
            submitted_at="2026-08-26T12:01:00Z",
        )
        with self.assertRaisesRegex(
            WorkflowError, "belongs to another GitHub session"
        ):
            flush_activity_outbox(
                self.project,
                adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
            )
        self.assertEqual(activity_outbox_status(self.project)["pending"], 1)

    def test_failed_request_remains_open_for_safe_retry(self) -> None:
        enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            action="claim",
            item_id="BLG-000001",
            payload={},
            submitted_at="2026-08-26T12:00:00Z",
        )
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action",
            side_effect=Exception("unexpected non-ARIA failure"),
        ):
            with self.assertRaisesRegex(Exception, "unexpected"):
                process_github_request_queue(
                    self.project,
                    coordinator_adapter=_Adapter(user_id="100", role="admin"),
                    queue=self.queue,
                    control_writer=object(),
                    coordinator_integration_id=9001,
                )
        self.assertEqual(self.queue.read(1).state, "open")

    def test_transient_workflow_failure_remains_open_for_coordinator_retry(self) -> None:
        enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            action="claim",
            item_id="BLG-000001",
            payload={},
            submitted_at="2026-08-26T12:00:00Z",
        )
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action",
            side_effect=WorkflowError("control worktree Git operation failed: fetch timeout"),
        ):
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["processed"][0]["retryable"])
        self.assertEqual(result["processed"][0]["sync_status"], "pending_sync")
        self.assertEqual(self.queue.read(1).state, "open")

    def test_unavailable_immutable_audit_is_retryable_and_stays_open(self) -> None:
        enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            action="claim",
            item_id="BLG-000001",
            payload={},
            submitted_at="2026-08-26T12:00:00Z",
        )
        with mock.patch.object(
            self.queue,
            "verify_immutable",
            side_effect=WorkflowError("GitHub queue issue edit audit is unavailable"),
        ):
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertTrue(result["processed"][0]["retryable"])
        self.assertEqual(self.queue.read(1).state, "open")

    def test_reopened_terminal_issue_is_closed_without_reapplication(self) -> None:
        enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            action="claim",
            item_id="BLG-000001",
            payload={},
            submitted_at="2026-08-26T12:00:00Z",
        )
        self.queue.close(1, state_reason="not_planned")
        self.transport.issues[1]["state"] = "open"
        self.transport.issues[1]["state_reason"] = "reopened"
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action"
        ) as apply:
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertTrue(result["processed"][0]["rejected"])
        self.assertEqual(self.queue.read(1).state, "closed")
        apply.assert_not_called()
        repeated = process_github_request_queue(
            self.project,
            coordinator_adapter=_Adapter(user_id="100", role="admin"),
            queue=self.queue,
            control_writer=object(),
            coordinator_integration_id=9001,
        )
        self.assertEqual(repeated["processed"], [])

    def test_submitter_closed_issue_is_still_processed_by_coordinator(self) -> None:
        enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            action="claim",
            item_id="BLG-000001",
            payload={},
            submitted_at="2026-08-26T12:00:00Z",
        )
        self.queue.close(1, state_reason="not_planned")
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action",
            return_value={"applied": True, "control_commit": "e" * 40},
        ) as apply, mock.patch(
            "aria.collaborative_request_queue._control_commit_has_request",
            return_value=True,
        ):
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertTrue(result["processed"][0]["ok"])
        self.assertEqual(self.queue.read(1).state_reason, "completed")
        apply.assert_called_once()

    def test_edited_poison_issue_is_durably_rejected_and_closed(self) -> None:
        enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=3,
            action="claim",
            item_id="BLG-000001",
            payload={},
            submitted_at="2026-08-26T12:00:00Z",
        )
        self.transport.issues[1]["lastEditedAt"] = "2026-08-26T12:01:00Z"
        result = process_github_request_queue(
            self.project,
            coordinator_adapter=_Adapter(user_id="100", role="admin"),
            queue=self.queue,
            control_writer=object(),
            coordinator_integration_id=9001,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["processed"][0]["rejected"])
        self.assertEqual(self.queue.read(1).state, "closed")

    def test_applied_history_does_not_starve_new_queue_work(self) -> None:
        request_ids: list[str] = []
        for number in range(1, 22):
            request_id = f"backlog-r1-{number:016x}"
            request_ids.append(request_id)
            enqueue_backlog_action(
                self.project,
                adapter=_Adapter(user_id="200", role="contributor"),
                queue=self.queue,
                expected_revision=0,
                action="claim",
                item_id="BLG-000001",
                payload={},
                request_id=request_id,
                submitted_at="2026-08-26T12:00:00Z",
            )
            if number <= 20:
                issue = self.queue.read(number)
                self.queue.comment(
                    number,
                    "ARIA accepted "
                    f"request_id={request_id} "
                    f"body_sha256={issue.body_sha256} "
                    f"control_commit={'e' * 40}",
                )
                self.queue.close(number)
        self.transport.issues[21]["updated_at"] = "2026-08-26T11:00:00Z"
        with mock.patch(
            "aria.collaborative_request_queue.queue_request_applied",
            side_effect=lambda _project, request, **_kwargs: (
                request["request_id"] in request_ids[:20]
            ),
        ), mock.patch(
            "aria.collaborative_request_queue._control_commit_has_request",
            return_value=True,
        ), mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action",
            return_value={"applied": True, "control_commit": "f" * 40},
        ) as apply:
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=SimpleNamespace(read_head=lambda: "e" * 40),
                coordinator_integration_id=9001,
                max_requests=1,
            )
        self.assertEqual([row["issue_number"] for row in result["processed"]], [21])
        apply.assert_called_once()

    def test_app_terminal_receipts_are_checked_before_any_reapplication(self) -> None:
        accepted = enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            action="claim",
            item_id="BLG-000001",
            payload={},
            request_id="backlog-r1-aaaaaaaaaaaaaaaa",
            submitted_at="2026-08-26T12:00:00Z",
        )
        accepted_issue = self.queue.read(accepted["issue_number"])
        self.queue.comment(
            accepted_issue.number,
            "ARIA accepted "
            f"request_id={accepted['request_id']} "
            f"body_sha256={accepted_issue.body_sha256} control_commit={'a' * 40}",
        )
        self.queue.close(accepted_issue.number)

        rejected = enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=1,
            action="claim",
            item_id="BLG-000001",
            payload={},
            request_id="backlog-r2-bbbbbbbbbbbbbbbb",
            submitted_at="2026-08-26T12:01:00Z",
        )
        rejected_issue = self.queue.read(rejected["issue_number"])
        self.queue.comment(
            rejected_issue.number,
            "ARIA rejected "
            f"request_id={rejected['request_id']} "
            f"body_sha256={rejected_issue.body_sha256} "
            f"without_apply=true reason_sha256={'b' * 64}",
        )
        self.queue.close(rejected_issue.number, state_reason="not_planned")

        with mock.patch(
            "aria.collaborative_request_queue._control_commit_has_request",
            return_value=True,
        ), mock.patch(
            "aria.collaborative_request_queue.queue_request_applied", return_value=False
        ), mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action"
        ) as apply:
            result = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=SimpleNamespace(read_head=lambda: "c" * 40),
                coordinator_integration_id=9001,
            )
        self.assertEqual(result["processed"], [])
        apply.assert_not_called()

    def test_late_activity_without_control_proof_gets_terminal_no_apply_receipt(self) -> None:
        submitted = enqueue_activity(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=3,
            task_id="BLG-000001",
            stage="testing",
            branch="work/yura",
            event_id="activity-r4-cccccccccccccccc",
            submitted_at="2026-08-26T12:01:00Z",
        )
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_activity",
            return_value={
                "applied": False,
                "reason": "late_event",
                "control_commit": "d" * 40,
            },
        ), mock.patch(
            "aria.collaborative_request_queue._control_commit_has_request",
            return_value=False,
        ):
            first = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertTrue(first["processed"][0]["rejected"])
        self.assertFalse(first["processed"][0]["applied"])
        issue = self.queue.read(int(submitted["issue_number"]))
        self.assertEqual(issue.state_reason, "not_planned")
        self.assertIsNotNone(
            self.queue.rejection_receipt(
                issue,
                {
                    "schema_version": 1,
                    "request_id": submitted["event_id"],
                    "project_id": "demo",
                    "kind": "activity",
                    "expected_revision": 3,
                    "operation": {
                        "task_id": "BLG-000001",
                        "stage": "testing",
                        "branch": "work/yura",
                        "note": None,
                    },
                    "submitted_at": "2026-08-26T12:01:00Z",
                },
                coordinator_integration_id=9001,
            )
        )
        repeated = process_github_request_queue(
            self.project,
            coordinator_adapter=_Adapter(user_id="100", role="admin"),
            queue=self.queue,
            control_writer=object(),
            coordinator_integration_id=9001,
        )
        self.assertEqual(repeated["processed"], [])

    def test_durable_rejection_intent_prevents_apply_after_close_crash(self) -> None:
        enqueue_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            expected_revision=0,
            action="claim",
            item_id="BLG-000001",
            payload={},
            submitted_at="2026-08-26T12:00:00Z",
        )
        self.transport.issues[1]["lastEditedAt"] = "2026-08-26T12:01:00Z"
        original_close = self.queue.close
        with mock.patch.object(
            self.queue, "close", side_effect=OSError("simulated stop after ledger")
        ):
            with self.assertRaisesRegex(OSError, "simulated stop"):
                process_github_request_queue(
                    self.project,
                    coordinator_adapter=_Adapter(user_id="100", role="admin"),
                    queue=self.queue,
                    control_writer=object(),
                    coordinator_integration_id=9001,
                )
        self.assertEqual(self.queue.read(1).state, "open")
        self.queue.close = original_close
        with mock.patch(
            "aria.collaborative_request_queue.submit_authenticated_backlog_action"
        ) as apply:
            recovered = process_github_request_queue(
                self.project,
                coordinator_adapter=_Adapter(user_id="100", role="admin"),
                queue=self.queue,
                control_writer=object(),
                coordinator_integration_id=9001,
            )
        self.assertTrue(recovered["processed"][0]["recovered"])
        self.assertEqual(self.queue.read(1).state_reason, "not_planned")
        apply.assert_not_called()

    def test_rejected_outbox_entry_moves_to_terminal_and_next_is_delivered(self) -> None:
        first = enqueue_activity(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=_OfflineQueue(),
            expected_revision=3,
            task_id="BLG-000001",
            stage="testing",
            branch="work/yura",
            event_id="activity-r4-0000000000000001",
            submitted_at="2026-08-26T12:01:00Z",
        )
        enqueue_activity(
            self.project,
            adapter=_OfflineAdapter(user_id="200", role="contributor"),
            queue=_OfflineQueue(),
            expected_revision=4,
            task_id="BLG-000001",
            stage="ready_for_pr",
            branch="work/yura",
            event_id="activity-r5-0000000000000002",
            submitted_at="2026-08-26T12:02:00Z",
        )
        flush_activity_outbox(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
        )
        issue = self.queue.read(1)
        self.queue.comment(
            1,
            "ARIA rejected "
            f"request_id={first['event_id']} "
            f"body_sha256={issue.body_sha256} "
            f"without_apply=true reason_sha256={'a' * 64}",
        )
        self.queue.close(1, state_reason="not_planned")
        result = flush_activity_outbox(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            queue=self.queue,
            maximum=2,
        )
        self.assertEqual(result["rejected"][0]["request_id"], first["event_id"])
        self.assertEqual(result["pending"], 1)
        self.assertEqual(activity_outbox_status(self.project)["terminal"], 1)
        self.assertEqual(self.transport.next_number, 3)


if __name__ == "__main__":
    unittest.main()
