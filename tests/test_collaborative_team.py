from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from aria.collaborative_backlog import ProviderIdentity
from aria.collaborative_team import (
    active_team_identities,
    collaborative_team_template,
    dump_collaborative_team,
    load_collaborative_team,
    record_team_invitation,
    sync_collaborative_team,
    validate_collaborative_team,
)
from aria.errors import WorkflowError
from aria.provider import ProviderActor, ProviderMembership, ProviderTeamMember


COORDINATOR = ProviderIdentity(
    "github-app", ProviderActor("900", "aria-coordinator", "ARIA Coordinator")
)


def _member(
    user_id: str,
    username: str,
    display_name: str | None,
    *roles: str,
) -> ProviderTeamMember:
    return ProviderTeamMember(
        provider="github",
        actor=ProviderActor(user_id, username, display_name),
        membership=ProviderMembership(True, tuple(sorted(roles))),
    )


TEAM = (
    _member("100", "aram", "Aram", "admin"),
    _member("200", "yura", "Yura", "contributor"),
    _member("300", "anton", "Anton", "contributor"),
)


def _sync(
    team: dict[str, object],
    members: tuple[ProviderTeamMember, ...] = TEAM,
    *,
    sync_id: str = "team-sync-0001",
    minute: int = 0,
    revision: int | None = None,
) -> dict[str, object]:
    return sync_collaborative_team(
        team,
        members,
        sync_id=sync_id,
        coordinator=COORDINATOR,
        expected_revision=int(team["revision"]) if revision is None else revision,
        checked_at=f"2026-08-26T12:{minute:02d}:00Z",
    )


class CollaborativeTeamTests(unittest.TestCase):
    def test_invitation_is_durable_then_becomes_active_after_provider_readback(self) -> None:
        team = _sync(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            ),
            TEAM[:1],
        )["team"]
        invited = record_team_invitation(
            team,
            target=ProviderIdentity("github", ProviderActor("200", "yura", "Yura")),
            invited_by=ProviderIdentity("github", ProviderActor("100", "aram", "Aram")),
            coordinator=COORDINATOR,
            invitation_id="77",
            request_id="team-invite-0001",
            expected_revision=1,
            invited_at="2026-08-26T12:01:00Z",
        )["team"]
        yura = invited["members"][1]
        self.assertEqual(yura["status"], "invited")
        self.assertFalse(yura["active"])
        self.assertEqual(yura["invited_by"]["user_id"], "100")
        activated = _sync(
            invited,
            TEAM[:2],
            sync_id="team-sync-0002",
            minute=2,
        )["team"]
        yura = activated["members"][1]
        self.assertEqual(yura["status"], "active")
        self.assertTrue(yura["active"])
        self.assertIsNone(yura["invitation_id"])

    def test_first_sync_records_immutable_ids_and_active_members(self) -> None:
        result = _sync(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            )
        )
        self.assertTrue(result["applied"])
        team = result["team"]
        self.assertEqual(team["revision"], 1)
        self.assertEqual(
            [member["user_id"] for member in team["members"]],
            ["100", "200", "300"],
        )
        self.assertEqual(
            [identity.actor.user_id for identity in active_team_identities(team)],
            ["100", "200", "300"],
        )

    def test_username_change_preserves_identity_and_history(self) -> None:
        team = _sync(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            )
        )["team"]
        renamed = (
            TEAM[0],
            _member("200", "yura-renamed", "Yura", "contributor"),
            TEAM[2],
        )
        updated = _sync(
            team, renamed, sync_id="team-sync-0002", minute=1
        )["team"]
        yura = updated["members"][1]
        self.assertEqual(yura["user_id"], "200")
        self.assertEqual(yura["username_snapshot"], "yura-renamed")
        self.assertEqual(
            [row["username"] for row in yura["username_history"]],
            ["yura", "yura-renamed"],
        )
        self.assertEqual(updated["events"][-1]["updated_user_ids"], ["200"])

    def test_missing_collaborator_is_revoked_not_deleted(self) -> None:
        team = _sync(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            )
        )["team"]
        updated = _sync(
            team, TEAM[:2], sync_id="team-sync-0002", minute=1
        )["team"]
        anton = updated["members"][2]
        self.assertFalse(anton["active"])
        self.assertEqual(anton["roles"], [])
        self.assertEqual(anton["revoked_at"], "2026-08-26T12:01:00Z")
        self.assertEqual(updated["events"][-1]["revoked_user_ids"], ["300"])
        self.assertEqual(
            [identity.actor.user_id for identity in active_team_identities(updated)],
            ["100", "200"],
        )

    def test_readded_collaborator_reactivates_existing_identity(self) -> None:
        team = _sync(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            )
        )["team"]
        team = _sync(team, TEAM[:2], sync_id="team-sync-0002", minute=1)["team"]
        team = _sync(team, TEAM, sync_id="team-sync-0003", minute=2)["team"]
        anton = team["members"][2]
        self.assertTrue(anton["active"])
        self.assertIsNone(anton["revoked_at"])
        self.assertEqual(anton["first_seen_at"], "2026-08-26T12:00:00Z")
        self.assertEqual(team["events"][-1]["updated_user_ids"], ["300"])

    def test_duplicate_sync_is_idempotent_but_payload_reuse_fails(self) -> None:
        template = collaborative_team_template(
            "demo", provider="github", repository_id="123456789"
        )
        team = _sync(template)["team"]
        duplicate = _sync(team)
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_sync")
        self.assertEqual(duplicate["team"], team)
        with self.assertRaisesRegex(WorkflowError, "different provider snapshot"):
            _sync(team, TEAM[:2])

    def test_stale_revision_and_older_timestamp_fail_closed(self) -> None:
        team = _sync(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            )
        )["team"]
        with self.assertRaisesRegex(WorkflowError, "Stale"):
            _sync(team, sync_id="team-sync-0002", minute=1, revision=0)
        with self.assertRaisesRegex(WorkflowError, "older"):
            sync_collaborative_team(
                team,
                TEAM,
                sync_id="team-sync-0002",
                coordinator=COORDINATOR,
                expected_revision=1,
                checked_at="2026-08-26T11:59:00Z",
            )

    def test_state_and_event_tampering_are_detected(self) -> None:
        team = _sync(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            )
        )["team"]
        state_tamper = copy.deepcopy(team)
        state_tamper["members"][0]["display_name_snapshot"] = "Mallory"
        with self.assertRaisesRegex(WorkflowError, "state does not match"):
            validate_collaborative_team(state_tamper)
        event_tamper = copy.deepcopy(team)
        event_tamper["events"][0]["added_user_ids"] = ["100"]
        with self.assertRaisesRegex(WorkflowError, "event hash"):
            validate_collaborative_team(event_tamper)

    def test_yaml_round_trip_is_deterministic(self) -> None:
        team = _sync(
            collaborative_team_template(
                "demo", provider="github", repository_id="123456789"
            )
        )["team"]
        content = dump_collaborative_team(team)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ARIA_TEAM.yaml"
            path.write_text(content, encoding="utf-8")
            loaded = load_collaborative_team(path)
        self.assertEqual(loaded, team)
        self.assertEqual(dump_collaborative_team(loaded), content)


if __name__ == "__main__":
    unittest.main()
