from __future__ import annotations

import itertools
import tempfile
import unittest
from pathlib import Path

from aria.activity import (
    activity_template,
    apply_activity_event,
    dump_activity,
    load_activity,
    mark_stale_activity,
    parse_activity_event,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.provider import ProviderActor


ACTORS = {
    "yura": ProviderActor(user_id="42", username_snapshot="yura"),
    "anton": ProviderActor(user_id="77", username_snapshot="anton"),
    "dmitry": ProviderActor(user_id="91", username_snapshot="dmitry"),
}


def _event(
    *,
    event_id: str,
    task_id: str = "BLG-101",
    actor: ProviderActor = ACTORS["yura"],
    source: str = "local_aria",
    stage: str = "analysis",
    observed_at: str = "2026-08-26T10:00:00Z",
    pr_number: int | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "project_id": "demo",
        "task_id": task_id,
        "actor": {
            "provider": "github",
            "user_id": actor.user_id,
            "username_snapshot": actor.username_snapshot,
        },
        "source": source,
        "stage": stage,
        "branch": f"work/{actor.username_snapshot}",
        "pr_number": pr_number,
        "note": None,
        "observed_at": observed_at,
    }


def _apply(
    snapshot: dict[str, object],
    event: dict[str, object],
    *,
    actor: ProviderActor = ACTORS["yura"],
    authorized_source: str | None = None,
    received_at: str = "2026-08-26T10:00:02Z",
) -> dict[str, object]:
    result = apply_activity_event(
        snapshot,
        event,
        authorized_source=authorized_source or str(event["source"]),
        authorized_provider="github",
        authorized_actor=actor,
        received_at=received_at,
    )
    return result


class ActivityContractTests(unittest.TestCase):
    def test_template_and_active_snapshot_have_deterministic_round_trip(self) -> None:
        snapshot = activity_template("demo")
        self.assertEqual(dump_activity(snapshot), dump_activity(snapshot))
        applied = _apply(snapshot, _event(event_id="event-0001"))
        self.assertTrue(applied["applied"])
        content = dump_activity(applied["snapshot"])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ACTIVITY.yaml"
            path.write_text(content, encoding="utf-8")
            self.assertEqual(load_activity(path), applied["snapshot"])

    def test_source_cannot_claim_a_stage_it_does_not_authorize(self) -> None:
        event = _event(event_id="event-0001", stage="completed")
        with self.assertRaisesRegex(ConfigurationError, "not allowed"):
            parse_activity_event(event)
        event = _event(
            event_id="event-0002",
            source="github",
            stage="in_review",
            pr_number=None,
        )
        with self.assertRaisesRegex(ConfigurationError, "requires a PR"):
            parse_activity_event(event)
        event["pr_number"] = 12
        with self.assertRaisesRegex(WorkflowError, "authorized source"):
            _apply(
                activity_template("demo"),
                event,
                authorized_source="local_aria",
            )

    def test_unsafe_branch_and_obvious_secret_note_are_rejected(self) -> None:
        event = _event(event_id="event-0001")
        event["branch"] = "dev/../aria-control"
        with self.assertRaisesRegex(ConfigurationError, "safe Git branch"):
            parse_activity_event(event)
        event = _event(event_id="event-0002")
        event["note"] = "Use github_pat_DO_NOT_STORE"
        with self.assertRaisesRegex(ConfigurationError, "contain a secret"):
            parse_activity_event(event)

    def test_actor_and_project_substitution_fail_closed(self) -> None:
        snapshot = activity_template("demo")
        event = _event(event_id="event-0001")
        with self.assertRaisesRegex(WorkflowError, "authorized identity"):
            _apply(snapshot, event, actor=ACTORS["anton"])
        event["project_id"] = "foreign"
        with self.assertRaisesRegex(WorkflowError, "another project"):
            _apply(snapshot, event)


class ActivityTransitionTests(unittest.TestCase):
    def test_duplicate_and_late_events_do_not_change_revision(self) -> None:
        first = _apply(
            activity_template("demo"),
            _event(event_id="event-0001"),
        )["snapshot"]
        duplicate = _apply(first, _event(event_id="event-0001"))
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_event")
        self.assertEqual(duplicate["snapshot"]["revision"], 1)

        newer = _apply(
            first,
            _event(
                event_id="event-0002",
                stage="implementation",
                observed_at="2026-08-26T10:10:00Z",
            ),
            received_at="2026-08-26T10:10:02Z",
        )["snapshot"]
        late = _apply(
            newer,
            _event(
                event_id="event-0003",
                stage="planning",
                observed_at="2026-08-26T10:05:00Z",
            ),
            received_at="2026-08-26T10:11:00Z",
        )
        self.assertFalse(late["applied"])
        self.assertEqual(late["reason"], "late_event")
        self.assertEqual(late["snapshot"]["revision"], 2)

    def test_local_github_coordinator_lifecycle_removes_completed_activity(self) -> None:
        snapshot = activity_template("demo")
        stages = [
            ("local_aria", "analysis", None),
            ("local_aria", "implementation", None),
            ("local_aria", "testing", None),
            ("local_aria", "ready_for_pr", None),
            ("github", "in_review", 12),
            ("github", "waiting_for_ci", 12),
            ("coordinator", "completed", 12),
        ]
        for index, (source, stage, pr_number) in enumerate(stages, start=1):
            stamp = f"2026-08-26T10:{index:02d}:00Z"
            result = _apply(
                snapshot,
                _event(
                    event_id=f"event-{index:04d}",
                    source=source,
                    stage=stage,
                    observed_at=stamp,
                    pr_number=pr_number,
                ),
                received_at=stamp,
            )
            self.assertTrue(result["applied"])
            snapshot = result["snapshot"]
        self.assertEqual(snapshot["revision"], 7)
        self.assertEqual(snapshot["active_work"], [])

    def test_three_developers_do_not_overwrite_each_other(self) -> None:
        events = [
            _event(event_id="event-yura", task_id="BLG-101", actor=ACTORS["yura"]),
            _event(event_id="event-anton", task_id="BLG-102", actor=ACTORS["anton"]),
            _event(event_id="event-dmitry", task_id="BLG-103", actor=ACTORS["dmitry"]),
        ]
        for order in itertools.permutations(events):
            snapshot = activity_template("demo")
            for event in order:
                actor = next(
                    value
                    for value in ACTORS.values()
                    if value.user_id == event["actor"]["user_id"]
                )
                snapshot = _apply(snapshot, event, actor=actor)["snapshot"]
            self.assertEqual(
                [row["task_id"] for row in snapshot["active_work"]],
                ["BLG-101", "BLG-102", "BLG-103"],
            )
            self.assertEqual(snapshot["revision"], 3)

    def test_stale_flag_changes_only_after_threshold(self) -> None:
        snapshot = _apply(
            activity_template("demo"),
            _event(event_id="event-0001"),
        )["snapshot"]
        fresh = mark_stale_activity(
            snapshot,
            now="2026-08-26T10:04:00Z",
            stale_after_seconds=300,
        )
        self.assertFalse(fresh["applied"])
        stale = mark_stale_activity(
            snapshot,
            now="2026-08-26T10:06:00Z",
            stale_after_seconds=300,
        )
        self.assertTrue(stale["applied"])
        self.assertTrue(stale["snapshot"]["active_work"][0]["stale"])
        with self.assertRaisesRegex(WorkflowError, "cannot move backwards"):
            mark_stale_activity(
                stale["snapshot"],
                now="2026-08-26T10:05:00Z",
                stale_after_seconds=300,
            )


if __name__ == "__main__":
    unittest.main()
