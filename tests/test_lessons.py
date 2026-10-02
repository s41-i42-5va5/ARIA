from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aria.lessons import LessonStore


class LessonStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.framework = root / "framework"
        self.runtime = root / "runtime"
        self.framework.mkdir()
        self.runtime.mkdir()
        self.store = LessonStore(self.framework, self.runtime)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_records_only_actionable_lessons_and_selects_relevant_context(self) -> None:
        events = self.store.append(
            project_id="demo",
            run_id="run-1",
            lessons=[
                {
                    "kind": "error",
                    "scope": "project",
                    "trigger": "Closing a build after editing service code",
                    "finding": "The implementation was not committed",
                    "countermeasure": "Commit the complete task delta before closure",
                    "evidence": "Build closure rejected a dirty worktree",
                },
                {
                    "kind": "successful_pattern",
                    "scope": "global",
                    "trigger": "Designing a contract from an external standard",
                    "finding": "Material sources need stable identifiers",
                    "countermeasure": "Record the source once and link its id from research and spec",
                    "evidence": "Trace read-back resolved REF-001 from STATE and HISTORY",
                },
            ],
            resolutions=[],
        )

        self.assertEqual(len(events), 2)
        snapshot = self.store.snapshot(
            project_id="demo",
            task="Design a service contract from an external standard",
        )
        self.assertEqual(snapshot["selected"][0]["kind"], "successful_pattern")
        self.assertNotIn("score", self.store.path.read_text(encoding="utf-8"))
        self.assertNotIn("milestone", self.store.path.read_text(encoding="utf-8"))
        self.assertTrue(self.store.verify()["ok"])

    def test_resolution_removes_lesson_and_append_is_idempotent_per_run(self) -> None:
        lesson = {
            "kind": "correction",
            "scope": "project",
            "trigger": "Publishing an ADR",
            "finding": "The ADR omitted its spec link",
            "countermeasure": "Validate ADR specs before publication",
            "evidence": "Deep design closure rejected the candidate",
        }
        first = self.store.append(
            project_id="demo", run_id="run-1", lessons=[lesson], resolutions=[]
        )
        duplicate = self.store.append(
            project_id="demo", run_id="run-1", lessons=[lesson], resolutions=[]
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(duplicate, [])

        resolution = self.store.append(
            project_id="demo",
            run_id="run-2",
            lessons=[],
            resolutions=[
                {
                    "lesson_id": first[0]["lesson_id"],
                    "evidence": "The validation now passes in the focused test",
                }
            ],
        )

        self.assertEqual(len(resolution), 1)
        snapshot = self.store.snapshot(project_id="demo", task="Publish an ADR")
        self.assertEqual(snapshot["active_count"], 0)
        self.assertEqual(snapshot["selected"], [])
        self.assertEqual(self.store.verify()["events"], 2)


if __name__ == "__main__":
    unittest.main()
