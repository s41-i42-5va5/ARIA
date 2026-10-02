from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import aria.collaborative_backlog_coordinator as coordinator_module
from aria.collaborative_backlog import load_collaborative_backlog
from aria.collaborative_backlog_coordinator import (
    collaborative_backlog_coordinator_paths,
    submit_collaborative_backlog_request,
)
from aria.errors import WorkflowError
from tests.test_collaborative_backlog import (
    ALL_PERMISSIONS,
    ARAM,
    COORDINATOR,
    MEMBERS,
    YURA,
    _request,
    _triage_payload,
)


def _add_request() -> dict[str, object]:
    return _request(
        request_id="request-add-0001",
        action="add",
        payload={
            "title": "Общая идея",
            "description": "Идея без исполнителя",
            "priority": "P1",
            "source_id": "idea-001",
            "dependencies": [],
            "evidence_required": False,
        },
    )


class CollaborativeBacklogCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.control = self.root / "control"
        self.runtime = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _submit(
        self,
        request: dict[str, object],
        *,
        actor=ARAM,
        expected_revision: int = 0,
        minute: int = 0,
    ) -> dict[str, object]:
        return submit_collaborative_backlog_request(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
            request_value=request,
            authenticated_actor=actor,
            active_members=MEMBERS,
            permissions=ALL_PERMISSIONS,
            coordinator=COORDINATOR,
            expected_revision=expected_revision,
            committed_at=f"2026-08-26T10:{minute:02d}:02Z",
        )

    def test_persists_backlog_and_exact_retry_is_noop(self) -> None:
        first = self._submit(_add_request())
        self.assertTrue(first["applied"])
        self.assertEqual(first["revision"], 1)
        paths = collaborative_backlog_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        self.assertEqual(load_collaborative_backlog(paths.backlog), first["backlog"])
        self.assertFalse(paths.transaction.exists())
        duplicate = self._submit(_add_request(), expected_revision=0)
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_request")
        self.assertEqual(duplicate["revision"], 1)

    def test_two_claims_from_same_revision_have_one_winner(self) -> None:
        self._submit(_add_request())
        self._submit(
            _request(
                request_id="request-triage-0001",
                action="triage",
                item_id="BLG-000001",
                payload=_triage_payload(),
            ),
            expected_revision=1,
            minute=1,
        )

        def claim(actor, suffix: str) -> tuple[str, object]:
            try:
                result = self._submit(
                    _request(
                        request_id=f"request-claim-{suffix}",
                        action="claim",
                        item_id="BLG-000001",
                        payload={"branch": "work/yura"},
                        minute=1,
                    ),
                    actor=YURA,
                    expected_revision=2,
                    minute=2,
                )
                return "ok", result
            except WorkflowError as error:
                return "error", str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: claim(*args), [(YURA, "first"), (YURA, "second")]))
        winners = [value for status, value in results if status == "ok"]
        losers = [value for status, value in results if status == "error"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn("Stale collaborative backlog revision", losers[0])
        stored = winners[0]["item"]["assignee"]["user_id"]
        self.assertEqual(stored, "200")

    def test_prepared_transaction_recovers_after_write_failure(self) -> None:
        paths = collaborative_backlog_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        original_install = coordinator_module._install
        failed = False

        def fail_backlog_once(path: Path, content: bytes) -> None:
            nonlocal failed
            if path == paths.backlog and not failed:
                failed = True
                raise OSError("simulated crash before backlog install")
            original_install(path, content)

        with mock.patch.object(coordinator_module, "_install", side_effect=fail_backlog_once):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self._submit(_add_request())
        self.assertTrue(paths.transaction.exists())
        self.assertFalse(paths.backlog.exists())

        recovered = self._submit(_add_request(), expected_revision=0)
        self.assertTrue(recovered["recovered"])
        self.assertFalse(recovered["applied"])
        self.assertEqual(recovered["reason"], "duplicate_request")
        self.assertEqual(recovered["revision"], 1)
        self.assertFalse(paths.transaction.exists())

    def test_recovery_rejects_state_outside_prepared_transaction(self) -> None:
        paths = collaborative_backlog_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        original_install = coordinator_module._install
        failed = False

        def fail_after_backlog(path: Path, content: bytes) -> None:
            nonlocal failed
            original_install(path, content)
            if path == paths.backlog and not failed:
                failed = True
                raise OSError("simulated crash after backlog install")

        with mock.patch.object(coordinator_module, "_install", side_effect=fail_after_backlog):
            with self.assertRaises(OSError):
                self._submit(_add_request())
        paths.backlog.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "outside the prepared transaction"):
            self._submit(_add_request())

    def test_recovery_accepts_already_installed_after_state(self) -> None:
        paths = collaborative_backlog_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        original_install = coordinator_module._install
        failed = False

        def stop_after_backlog(path: Path, content: bytes) -> None:
            nonlocal failed
            original_install(path, content)
            if path == paths.backlog and not failed:
                failed = True
                raise OSError("simulated stop after backlog install")

        with mock.patch.object(coordinator_module, "_install", side_effect=stop_after_backlog):
            with self.assertRaisesRegex(OSError, "simulated stop"):
                self._submit(_add_request())
        self.assertTrue(paths.backlog.exists())
        self.assertTrue(paths.transaction.exists())

        recovered = self._submit(_add_request(), expected_revision=0)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["reason"], "duplicate_request")
        self.assertEqual(recovered["revision"], 1)
        self.assertFalse(paths.transaction.exists())


if __name__ == "__main__":
    unittest.main()
