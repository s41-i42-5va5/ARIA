from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from aria.collaboration import build_control_contract, dump_control_contract
from aria.collaborative_backlog import (
    ProviderIdentity,
    collaborative_backlog_template,
    dump_collaborative_backlog,
)
from aria.collaborative_backlog_runtime import (
    authenticated_backlog_status,
    submit_authenticated_backlog_action,
)
from aria.collaborative_documents import collaborative_access_template
from aria.collaborative_team import (
    collaborative_team_template,
    dump_collaborative_team,
    sync_collaborative_team,
)
from aria.errors import WorkflowError
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

    def __init__(self, *, user_id: str = "100", role: str = "admin") -> None:
        member = next(row for row in TEAM if row.actor.user_id == user_id)
        self.actor = member.actor
        self.role = role

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return ProviderInspection(
            provider="github",
            repository_id=repository_id,
            actor=self.actor,
            membership=ProviderMembership(True, (self.role,)),
            protection=ProviderBranchProtection(
                True, False, "aria-coordinator"
            ),
        )


class CollaborativeBacklogRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prepare_patcher = mock.patch(
            "aria.collaborative_backlog_runtime.prepare_control_mutation"
        )
        self.prepare_patcher.start()
        self.doctor_patcher = mock.patch(
            "aria.collaborative_doctor.run_collaborative_project_doctor",
            return_value={
                "ok": True,
                "checks": [{"id": "repository_contract", "ok": True, "blocking": True}],
            },
        )
        self.doctor_patcher.start()
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
                collaborative_access_template(contract),
                allow_unicode=True,
                sort_keys=False,
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
        (self.docs / "BACKLOG.yaml").write_text(
            dump_collaborative_backlog(collaborative_backlog_template("demo")),
            encoding="utf-8",
        )
        self.project = SimpleNamespace(
            project_id="demo",
            docs_root=self.docs,
            runtime_root=self.runtime,
        )

    def tearDown(self) -> None:
        self.doctor_patcher.stop()
        self.prepare_patcher.stop()
        self.temporary.cleanup()

    def _add(self, **overrides):
        arguments = {
            "adapter": _Adapter(),
            "control_writer": object(),
            "coordinator_integration_id": 9001,
            "expected_revision": 0,
            "action": "add",
            "item_id": None,
            "payload": {
                "title": "Идея Арама",
                "description": "Пока без исполнителя",
                "priority": "P1",
                "source_id": None,
                "dependencies": [],
                "evidence_required": False,
            },
            "requested_at": "2026-08-26T11:00:00Z",
            "committed_at": "2026-08-26T11:00:01Z",
        }
        arguments.update(overrides)
        return submit_authenticated_backlog_action(self.project, **arguments)

    @mock.patch(
        "aria.collaborative_backlog_runtime.synchronize_control_worktree",
        return_value={"control_commit": "c" * 40, "recovered": False},
    )
    def test_add_unassigned_idea_and_retry_preserve_authorship(self, remote_sync) -> None:
        first = self._add()
        self.assertTrue(first["applied"])
        self.assertEqual(first["item"]["creator"]["user_id"], "100")
        self.assertIsNone(first["item"]["assignee"])
        self.assertRegex(first["request_id"], r"^backlog-r1-[0-9a-f]{16}$")
        duplicate = self._add(
            requested_at="2026-08-26T11:01:00Z",
            committed_at="2026-08-26T11:01:01Z",
        )
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_request")
        self.assertEqual(duplicate["request_id"], first["request_id"])
        self.assertEqual(remote_sync.call_count, 2)

    @mock.patch(
        "aria.collaborative_backlog_runtime.synchronize_control_worktree",
        return_value={"control_commit": "c" * 40, "recovered": False},
    )
    def test_live_role_permissions_reject_contributor_triage(self, _remote) -> None:
        self._add()
        with self.assertRaisesRegex(WorkflowError, "not permitted"):
            submit_authenticated_backlog_action(
                self.project,
                adapter=_Adapter(user_id="200", role="contributor"),
                control_writer=object(),
                coordinator_integration_id=9001,
                expected_revision=1,
                action="triage",
                item_id="BLG-000001",
                payload=_triage_payload(),
                requested_at="2026-08-26T11:02:00Z",
                committed_at="2026-08-26T11:02:01Z",
            )

    @mock.patch(
        "aria.collaborative_backlog_runtime.synchronize_control_worktree",
        return_value={"control_commit": "c" * 40, "recovered": False},
    )
    def test_authenticated_mine_filters_by_immutable_user_id(self, _remote) -> None:
        self._add()
        submit_authenticated_backlog_action(
            self.project,
            adapter=_Adapter(),
            control_writer=object(),
            coordinator_integration_id=9001,
            expected_revision=1,
            action="triage",
            item_id="BLG-000001",
            payload=_triage_payload(),
            requested_at="2026-08-26T11:02:00Z",
            committed_at="2026-08-26T11:02:01Z",
        )
        ready = authenticated_backlog_status(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
        )
        self.assertEqual(ready["recommended_order"], ["BLG-000001"])
        submit_authenticated_backlog_action(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
            control_writer=object(),
            coordinator_integration_id=9001,
            expected_revision=2,
            action="claim",
            item_id="BLG-000001",
            payload={},
            requested_at="2026-08-26T11:03:00Z",
            committed_at="2026-08-26T11:03:01Z",
        )
        mine = authenticated_backlog_status(
            self.project,
            adapter=_Adapter(user_id="200", role="contributor"),
        )
        self.assertEqual(mine["actor"]["user_id"], "200")
        self.assertEqual([item["id"] for item in mine["items"]], ["BLG-000001"])
        self.assertEqual(mine["items"][0]["lease"]["branch"], "work/yura")
        self.assertEqual(mine["recommended_order"], [])

    def test_team_projection_identity_must_match_control_contract(self) -> None:
        other = sync_collaborative_team(
            collaborative_team_template(
                "other", provider="github", repository_id="987654321"
            ),
            TEAM,
            sync_id="team-sync-other-0001",
            coordinator=COORDINATOR,
            expected_revision=0,
            checked_at="2026-08-26T10:00:00Z",
        )["team"]
        (self.docs / "ARIA_TEAM.yaml").write_text(
            dump_collaborative_team(other), encoding="utf-8"
        )
        with self.assertRaisesRegex(WorkflowError, "another collaborative project"):
            authenticated_backlog_status(self.project, adapter=_Adapter())

    def test_claim_requires_app_pinned_repository_contract(self) -> None:
        with mock.patch(
            "aria.collaborative_backlog_runtime.synchronize_control_worktree",
            return_value={"control_commit": "c" * 40, "recovered": False},
        ):
            self._add()
            submit_authenticated_backlog_action(
                self.project,
                adapter=_Adapter(),
                control_writer=object(),
                coordinator_integration_id=9001,
                expected_revision=1,
                action="triage",
                item_id="BLG-000001",
                payload=_triage_payload(),
            )
            with mock.patch(
                "aria.collaborative_doctor.run_collaborative_project_doctor",
                return_value={
                    "ok": True,
                    "checks": [{
                        "id": "repository_contract",
                        "ok": False,
                        "blocking": False,
                    }],
                },
            ):
                with self.assertRaisesRegex(WorkflowError, "repository_contract"):
                    submit_authenticated_backlog_action(
                        self.project,
                        adapter=_Adapter(user_id="200", role="contributor"),
                        control_writer=object(),
                        coordinator_integration_id=9001,
                        expected_revision=2,
                        action="claim",
                        item_id="BLG-000001",
                        payload={},
                    )


if __name__ == "__main__":
    unittest.main()
