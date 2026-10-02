from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aria.collaboration import build_control_contract, dump_control_contract
from aria.collaborative_team_runtime import (
    collaborative_team_status,
    invite_authenticated_member,
    revoke_authenticated_member,
    sync_authenticated_team,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.github_team import GitHubInvitationReceipt
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
)
from tests.test_collaborative_team import TEAM


class _Adapter:
    provider_id = "github"

    def __init__(self, *, actor: ProviderActor | None = None) -> None:
        self.actor = actor or TEAM[0].actor
        self.calls: list[tuple[str, str]] = []

    def inspect_collaboration(
        self, *, repository_id: str, control_branch: str
    ) -> ProviderInspection:
        self.calls.append(("inspect", repository_id))
        return ProviderInspection(
            provider="github",
            repository_id=repository_id,
            actor=self.actor,
            membership=ProviderMembership(True, ("admin",)),
            protection=ProviderBranchProtection(True, False, "aria-coordinator"),
        )

    def list_collaborators(self, *, repository_id: str):
        self.calls.append(("list", repository_id))
        return TEAM


class _UnprotectedAdapter(_Adapter):
    def inspect_collaboration(
        self, *, repository_id: str, control_branch: str
    ) -> ProviderInspection:
        inspection = super().inspect_collaboration(
            repository_id=repository_id, control_branch=control_branch
        )
        return ProviderInspection(
            provider=inspection.provider,
            repository_id=inspection.repository_id,
            actor=inspection.actor,
            membership=inspection.membership,
            protection=ProviderBranchProtection(False, True, None),
        )


class _Manager:
    def __init__(self, actor: ProviderActor) -> None:
        self.actor = actor
        self.calls: list[tuple[str, str]] = []

    def invite(self, *, username: str, permission: str) -> GitHubInvitationReceipt:
        self.calls.append((username, permission))
        return GitHubInvitationReceipt(
            actor=self.actor,
            state="invited",
            invitation_id="771",
            permission=permission,
        )

    def revoke(self, *, username: str) -> ProviderActor:
        self.calls.append((username, "revoke"))
        return self.actor


class _SnapshotAdapter(_Adapter):
    def __init__(self, members) -> None:
        super().__init__()
        self.members = tuple(members)

    def list_collaborators(self, *, repository_id: str):
        self.calls.append(("list", repository_id))
        return self.members


class CollaborativeTeamRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prepare_patcher = mock.patch(
            "aria.collaborative_team_runtime.prepare_control_mutation"
        )
        self.prepare_patcher.start()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.docs = root / "docs"
        self.docs.mkdir()
        self.runtime = root / "runtime"
        (self.runtime / "projects" / "demo").mkdir(parents=True)
        contract = build_control_contract(
            project_id="demo",
            provider="github",
            repository_id="123456789",
        )
        (self.docs / "CONTROL.yaml").write_text(
            dump_control_contract(contract), encoding="utf-8"
        )
        self.project = SimpleNamespace(
            project_id="demo",
            docs_root=self.docs,
            runtime_root=self.runtime / "projects" / "demo",
        )

    def tearDown(self) -> None:
        self.prepare_patcher.stop()
        self.temporary.cleanup()

    def test_status_before_and_after_authenticated_sync(self) -> None:
        empty = collaborative_team_status(self.project)
        self.assertEqual(empty["revision"], 0)
        self.assertEqual(empty["active_count"], 0)
        adapter = _Adapter()
        with mock.patch(
            "aria.collaborative_team_runtime.synchronize_control_worktree",
            return_value={"control_commit": "c" * 40, "recovered": False},
        ) as remote_sync:
            synced = sync_authenticated_team(
                self.project,
                adapter=adapter,
                coordinator_integration_id=9001,
                expected_revision=0,
                checked_at="2026-08-26T14:00:00Z",
                control_writer=object(),
            )
        self.assertTrue(synced["applied"])
        self.assertEqual(synced["active_count"], 3)
        self.assertEqual(
            adapter.calls,
            [("inspect", "123456789"), ("list", "123456789")],
        )
        self.assertRegex(synced["sync_id"], r"^team-sync-r1-[0-9a-f]{16}$")
        self.assertEqual(
            remote_sync.call_args.kwargs["operation_id"], synced["sync_id"]
        )
        status = collaborative_team_status(self.project)
        self.assertEqual(status["revision"], 1)
        self.assertEqual(status["members"][1]["user_id"], "200")

    def test_authenticated_actor_must_be_in_provider_snapshot(self) -> None:
        adapter = _Adapter(actor=ProviderActor("999", "outsider"))
        with self.assertRaisesRegex(WorkflowError, "absent"):
            sync_authenticated_team(
                self.project,
                adapter=adapter,
                coordinator_integration_id=9001,
                expected_revision=0,
                sync_id="team-sync-runtime-0001",
                checked_at="2026-08-26T14:00:00Z",
                control_writer=object(),
            )

    def test_sync_requires_coordinator_only_branch_protection(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "coordinator-only"):
            sync_authenticated_team(
                self.project,
                adapter=_UnprotectedAdapter(),
                coordinator_integration_id=9001,
                expected_revision=0,
                sync_id="team-sync-runtime-0001",
                checked_at="2026-08-26T14:00:00Z",
                control_writer=object(),
            )

    def test_invitation_requires_admin_and_records_provider_receipt(self) -> None:
        manager = _Manager(ProviderActor("400", "new-developer", "New Developer"))
        with mock.patch(
            "aria.collaborative_team_runtime.synchronize_control_worktree",
            return_value={"control_commit": "d" * 40, "recovered": False},
        ):
            result = invite_authenticated_member(
                self.project,
                adapter=_Adapter(),
                manager=manager,
                username="new-developer",
                permission="push",
                coordinator_integration_id=9001,
                expected_revision=0,
                request_id="team-invite-runtime-0001",
                invited_at="2026-09-02T10:00:00Z",
                control_writer=object(),
            )
        self.assertEqual(result["invitation_state"], "invited")
        self.assertEqual(manager.calls, [("new-developer", "push")])
        status = collaborative_team_status(self.project)
        self.assertEqual(status["members"][0]["status"], "invited")
        self.assertEqual(status["members"][0]["invitation_id"], "771")

    def test_stale_invitation_revision_has_no_external_side_effect(self) -> None:
        manager = _Manager(ProviderActor("400", "new-developer"))
        with self.assertRaisesRegex(WorkflowError, "Stale collaborative team revision"):
            invite_authenticated_member(
                self.project,
                adapter=_Adapter(),
                manager=manager,
                username="new-developer",
                permission="push",
                coordinator_integration_id=9001,
                expected_revision=7,
                request_id="team-invite-runtime-stale",
                control_writer=object(),
            )
        self.assertEqual(manager.calls, [])

    def test_invalid_invitation_identity_is_rejected_before_provider_access(self) -> None:
        manager = _Manager(ProviderActor("400", "new-developer"))
        adapter = _Adapter()
        with self.assertRaises(ConfigurationError):
            invite_authenticated_member(
                self.project,
                adapter=adapter,
                manager=manager,
                username="../outside",
                permission="owner",
                coordinator_integration_id=9001,
                expected_revision=0,
                request_id="../unsafe-request",
                control_writer=object(),
            )
        self.assertEqual(adapter.calls, [])
        self.assertEqual(manager.calls, [])

    def test_invitation_retry_uses_external_journal_without_second_mutation(self) -> None:
        manager = _Manager(ProviderActor("400", "new-developer"))
        arguments = dict(
            adapter=_Adapter(),
            manager=manager,
            username="new-developer",
            permission="push",
            coordinator_integration_id=9001,
            expected_revision=0,
            request_id="team-invite-runtime-recovery",
            invited_at="2026-09-02T10:00:00Z",
            control_writer=object(),
        )
        with mock.patch(
            "aria.collaborative_team_runtime.synchronize_control_worktree",
            side_effect=WorkflowError("remote unavailable"),
        ):
            with self.assertRaisesRegex(WorkflowError, "remote unavailable"):
                invite_authenticated_member(self.project, **arguments)
        with mock.patch(
            "aria.collaborative_team_runtime.recover_pending_control_sync",
            return_value={"control_commit": "e" * 40, "recovered": True},
        ):
            result = invite_authenticated_member(self.project, **arguments)
        self.assertEqual(result["invitation_state"], "invited")
        self.assertEqual(manager.calls, [("new-developer", "push")])

    def test_revocation_is_confirmed_by_provider_snapshot(self) -> None:
        with mock.patch(
            "aria.collaborative_team_runtime.synchronize_control_worktree",
            return_value={"control_commit": "c" * 40, "recovered": False},
        ):
            sync_authenticated_team(
                self.project,
                adapter=_Adapter(),
                coordinator_integration_id=9001,
                expected_revision=0,
                checked_at="2026-09-02T10:00:00Z",
                control_writer=object(),
            )
            manager = _Manager(TEAM[1].actor)
            result = revoke_authenticated_member(
                self.project,
                adapter=_SnapshotAdapter((TEAM[0], TEAM[2])),
                manager=manager,
                username=TEAM[1].actor.username_snapshot,
                coordinator_integration_id=9001,
                expected_revision=1,
                request_id="team-revoke-runtime-0001",
                checked_at="2026-09-02T10:01:00Z",
                control_writer=object(),
            )
        self.assertEqual(result["revoked_actor"]["user_id"], TEAM[1].actor.user_id)
        member = next(
            item for item in result["members"] if item["user_id"] == TEAM[1].actor.user_id
        )
        self.assertEqual(member["status"], "revoked")
        self.assertFalse(member["active"])

    def test_control_project_identity_is_enforced(self) -> None:
        other = build_control_contract(
            project_id="other",
            provider="github",
            repository_id="123456789",
        )
        (self.docs / "CONTROL.yaml").write_text(
            dump_control_contract(other), encoding="utf-8"
        )
        with self.assertRaisesRegex(WorkflowError, "another project"):
            collaborative_team_status(self.project)


if __name__ == "__main__":
    unittest.main()
