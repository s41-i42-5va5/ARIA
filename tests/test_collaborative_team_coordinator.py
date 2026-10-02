from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import aria.collaborative_team_coordinator as coordinator_module
from aria.collaborative_team import load_collaborative_team
from aria.collaborative_team_coordinator import (
    collaborative_team_coordinator_paths,
    sync_collaborative_team_snapshot,
)
from aria.errors import WorkflowError
from tests.test_collaborative_team import COORDINATOR, TEAM


class CollaborativeTeamCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.control = self.root / "control"
        self.runtime = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _sync(
        self,
        *,
        sync_id: str = "team-sync-0001",
        expected_revision: int = 0,
        minute: int = 0,
    ) -> dict[str, object]:
        return sync_collaborative_team_snapshot(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
            provider="github",
            repository_id="123456789",
            provider_members=TEAM,
            sync_id=sync_id,
            coordinator=COORDINATOR,
            expected_revision=expected_revision,
            checked_at=f"2026-08-26T13:{minute:02d}:00Z",
        )

    def test_persists_team_and_exact_retry_is_noop(self) -> None:
        first = self._sync()
        self.assertTrue(first["applied"])
        paths = collaborative_team_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        self.assertEqual(load_collaborative_team(paths.team), first["team"])
        self.assertFalse(paths.transaction.exists())
        duplicate = self._sync()
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_sync")
        self.assertEqual(duplicate["revision"], 1)

    def test_two_syncs_from_same_revision_have_one_winner(self) -> None:
        self._sync()

        def update(suffix: str) -> tuple[str, object]:
            try:
                return "ok", self._sync(
                    sync_id=f"team-sync-{suffix}", expected_revision=1, minute=1
                )
            except WorkflowError as error:
                return "error", str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ["0002", "0003"]))
        self.assertEqual(len([value for status, value in results if status == "ok"]), 1)
        errors = [value for status, value in results if status == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Stale collaborative team revision", errors[0])

    def test_prepared_transaction_recovers_after_write_failure(self) -> None:
        paths = collaborative_team_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        original_install = coordinator_module._install
        failed = False

        def fail_team_once(path: Path, content: bytes) -> None:
            nonlocal failed
            if path == paths.team and not failed:
                failed = True
                raise OSError("simulated crash before team install")
            original_install(path, content)

        with mock.patch.object(coordinator_module, "_install", side_effect=fail_team_once):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self._sync()
        self.assertTrue(paths.transaction.exists())
        self.assertFalse(paths.team.exists())
        recovered = self._sync()
        self.assertTrue(recovered["recovered"])
        self.assertFalse(recovered["applied"])
        self.assertFalse(paths.transaction.exists())

    def test_recovery_rejects_state_outside_prepared_transaction(self) -> None:
        paths = collaborative_team_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        original_install = coordinator_module._install
        failed = False

        def fail_after_team(path: Path, content: bytes) -> None:
            nonlocal failed
            original_install(path, content)
            if path == paths.team and not failed:
                failed = True
                raise OSError("simulated crash after team install")

        with mock.patch.object(coordinator_module, "_install", side_effect=fail_after_team):
            with self.assertRaises(OSError):
                self._sync()
        paths.team.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "outside the prepared transaction"):
            self._sync()

    def test_repository_identity_cannot_change(self) -> None:
        self._sync()
        with self.assertRaisesRegex(WorkflowError, "does not match"):
            sync_collaborative_team_snapshot(
                control_root=self.control,
                runtime_root=self.runtime,
                project_id="demo",
                provider="github",
                repository_id="987654321",
                provider_members=TEAM,
                sync_id="team-sync-0002",
                coordinator=COORDINATOR,
                expected_revision=1,
                checked_at="2026-08-26T13:01:00Z",
            )


if __name__ == "__main__":
    unittest.main()
