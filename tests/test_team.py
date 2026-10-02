from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import yaml

from aria.errors import WorkflowError
from aria.team import (
    claim_task,
    release_task,
    team_status,
    validate_independent_review,
)


class TeamCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        docs = self.root / "docs"
        runtime = self.root / "runtime"
        docs.mkdir()
        payload = {
            "schema_version": 1,
            "actors": [
                {
                    "id": "alice",
                    "type": "human",
                    "roles": ["contributor"],
                },
                {
                    "id": "bob",
                    "type": "human",
                    "roles": ["reviewer"],
                },
                {
                    "id": "ci-release",
                    "type": "service",
                    "roles": ["ci", "release-manager"],
                },
            ],
        }
        (docs / "ARIA_TEAM.yaml").write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        self.project = SimpleNamespace(
            project_id="sample", docs_root=docs, runtime_root=runtime
        )
        self.now = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_two_concurrent_claims_have_exactly_one_owner(self) -> None:
        def attempt(actor_id: str) -> dict[str, object] | str:
            try:
                return claim_task(
                    self.project,
                    task_id="R-1406",
                    actor_id=actor_id,
                    expected_revision=0,
                    ttl_seconds=300,
                    now=self.now,
                )
            except WorkflowError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["alice", "ci-release"]))
        winners = [row for row in results if isinstance(row, dict)]
        losers = [row for row in results if isinstance(row, str)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn("Stale project revision", losers[0])
        status = team_status(self.project, now=self.now)
        self.assertEqual(status["revision"], 1)
        self.assertEqual(len(status["active_leases"]), 1)

    def test_stale_revision_and_wrong_release_token_fail_closed(self) -> None:
        claimed = claim_task(
            self.project,
            task_id="R-1407",
            actor_id="alice",
            expected_revision=0,
            ttl_seconds=300,
            now=self.now,
        )
        with self.assertRaisesRegex(WorkflowError, "Stale project revision"):
            claim_task(
                self.project,
                task_id="R-1408",
                actor_id="ci-release",
                expected_revision=0,
                ttl_seconds=300,
                now=self.now,
            )
        with self.assertRaisesRegex(WorkflowError, "exact token"):
            release_task(
                self.project,
                task_id="R-1407",
                actor_id="alice",
                token="wrong",
                expected_revision=1,
                now=self.now,
            )
        released = release_task(
            self.project,
            task_id="R-1407",
            actor_id="alice",
            token=str(claimed["lease"]["token"]),
            expected_revision=1,
            now=self.now,
        )
        self.assertEqual(released["revision"], 2)

    def test_expired_lease_can_be_reclaimed_at_next_revision(self) -> None:
        claim_task(
            self.project,
            task_id="R-1406",
            actor_id="alice",
            expected_revision=0,
            ttl_seconds=1,
            now=self.now,
        )
        reclaimed = claim_task(
            self.project,
            task_id="R-1406",
            actor_id="ci-release",
            expected_revision=1,
            ttl_seconds=60,
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(reclaimed["lease"]["actor_id"], "ci-release")
        self.assertEqual(reclaimed["revision"], 2)

    def test_independent_review_requires_different_reviewer_actor(self) -> None:
        result = validate_independent_review(
            self.project, contributor_id="alice", reviewer_id="bob"
        )
        self.assertTrue(result["independent"])
        with self.assertRaisesRegex(WorkflowError, "own independent review"):
            validate_independent_review(
                self.project, contributor_id="ci-release", reviewer_id="ci-release"
            )
        with self.assertRaisesRegex(WorkflowError, "required roles"):
            validate_independent_review(
                self.project, contributor_id="ci-release", reviewer_id="alice"
            )


if __name__ == "__main__":
    unittest.main()
