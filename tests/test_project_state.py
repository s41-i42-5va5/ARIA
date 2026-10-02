from __future__ import annotations

import unittest

import yaml

from aria.project_state import (
    projection_yaml,
    select_next_task,
    validate_state_model,
)


def roadmap_state() -> dict[str, object]:
    return {
        "schema_version": 2,
        "project_id": "demo",
        "profile": "roadmap",
        "focus": {
            "id": "release",
            "goal": "Finish release",
            "stage_id": "stage-1",
            "ordered_tasks": ["done-task", "deferred-task", "next-task"],
        },
        "current": {
            "task_id": None,
            "task_summary": None,
            "stage_id": None,
            "status": None,
        },
        "stages": [
            {
                "id": "stage-1",
                "title": "Stage 1",
                "status": "in_progress",
                "exit_criteria": ["All required tasks are done"],
                "tasks": [
                    {
                        "id": "done-task",
                        "title": "Done",
                        "status": "done",
                        "priority": 1,
                        "depends_on": [],
                        "spec": None,
                    },
                    {
                        "id": "deferred-task",
                        "title": "Deferred",
                        "status": "deferred",
                        "priority": 2,
                        "depends_on": ["done-task"],
                        "spec": None,
                    },
                    {
                        "id": "next-task",
                        "title": "Next useful task",
                        "status": "not_started",
                        "priority": 50,
                        "depends_on": ["done-task"],
                        "spec": "specs/archive/legacy/next-task.md",
                    },
                    {
                        "id": "higher-priority-outside-focus",
                        "title": "Priority fallback",
                        "status": "backlog",
                        "priority": 1,
                        "depends_on": [],
                        "spec": None,
                    },
                ],
            }
        ],
        "issues": {"blockers": [], "bugs": [], "questions": [], "inbox": []},
        "last_verified": None,
        "last_completed": None,
        "history_checkpoint": {"sequence": 1, "event_sha256": "abc"},
    }


class ProjectStateTests(unittest.TestCase):
    def test_focus_order_skips_done_and_deferred_before_priority_fallback(self) -> None:
        selected = select_next_task(roadmap_state())
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["task_id"], "next-task")
        self.assertEqual(selected["source"], "focus")
        self.assertEqual(
            selected["skipped"],
            [
                {"task_id": "done-task", "reason": "done"},
                {"task_id": "deferred-task", "reason": "deferred"},
            ],
        )

    def test_validator_rejects_dependency_cycle(self) -> None:
        state = roadmap_state()
        tasks = state["stages"][0]["tasks"]
        tasks[0]["depends_on"] = ["next-task"]
        validation = validate_state_model(
            state, project_id="demo", expected_profile="roadmap"
        )
        self.assertFalse(validation["ok"])
        self.assertTrue(
            any("dependency cycle" in error for error in validation["errors"]),
            validation,
        )

    def test_projection_keeps_summary_and_selected_task_not_full_roadmap(self) -> None:
        state = roadmap_state()
        projection = yaml.safe_load(
            projection_yaml(state, selected=select_next_task(state))
        )
        self.assertEqual(projection["selected_task"]["task_id"], "next-task")
        self.assertEqual(projection["stages"][0]["task_counts"]["done"], 1)
        self.assertNotIn("tasks", projection["stages"][0])

    def test_blocked_current_requires_explicit_unblocking_action(self) -> None:
        state = roadmap_state()
        current = state["current"]
        current.update(
            {
                "task_id": "next-task",
                "task_summary": "Next useful task",
                "stage_id": "stage-1",
                "status": "blocked",
                "next_action": "Obtain hardware",
            }
        )
        state["stages"][0]["tasks"][2]["status"] = "blocked"
        selected = select_next_task(state)
        self.assertTrue(selected["blocked"])
        self.assertEqual(selected["reason"], "Obtain hardware")

    def test_planned_stage_requires_explicit_stage_gate(self) -> None:
        state = roadmap_state()
        state["focus"] = {
            "id": None,
            "goal": None,
            "stage_id": None,
            "ordered_tasks": [],
        }
        state["stages"][0]["status"] = "done"
        for task in state["stages"][0]["tasks"]:
            task["status"] = "done"
        state["stages"].append(
            {
                "id": "stage-2",
                "title": "Stage 2",
                "status": "planned",
                "exit_criteria": [],
                "tasks": [
                    {
                        "id": "planned-task",
                        "title": "Must wait for stage gate",
                        "status": "not_started",
                        "priority": 1,
                        "depends_on": [],
                        "spec": None,
                    }
                ],
            }
        )
        self.assertIsNone(select_next_task(state))

    def test_legacy_trace_requires_honest_commit_pointer_metadata(self) -> None:
        state = roadmap_state()
        task = state["stages"][0]["tasks"][0]
        task["trace"] = {
            "spec": None,
            "adrs": [],
            "research": [],
            "references": [],
            "legacy_implementation": {
                "source": "imported STATE.commit",
                "completeness": "pointer_only",
                "commits": ["a" * 40],
            },
        }
        validation = validate_state_model(
            state, project_id="demo", expected_profile="roadmap"
        )
        self.assertTrue(validation["ok"], validation)

        task["trace"]["legacy_implementation"]["commits"] = []
        invalid = validate_state_model(
            state, project_id="demo", expected_profile="roadmap"
        )
        self.assertFalse(invalid["ok"])
        self.assertTrue(
            any("legacy_implementation.commits" in row for row in invalid["errors"]),
            invalid,
        )


if __name__ == "__main__":
    unittest.main()
