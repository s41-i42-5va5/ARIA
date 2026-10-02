from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import aria.activity_coordinator as coordinator_module
from aria.activity import load_activity
from aria.activity_coordinator import (
    activity_coordinator_paths,
    submit_activity_event,
)
from aria.errors import WorkflowError
from aria.provider import ProviderActor


ACTOR = ProviderActor(user_id="42", username_snapshot="yura")


def _event(
    *,
    event_id: str,
    task_id: str = "BLG-101",
    stage: str = "analysis",
    observed_at: str = "2026-08-26T10:00:00Z",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "project_id": "demo",
        "task_id": task_id,
        "actor": {
            "provider": "github",
            "user_id": ACTOR.user_id,
            "username_snapshot": ACTOR.username_snapshot,
        },
        "source": "local_aria",
        "stage": stage,
        "branch": "work/yura",
        "pr_number": None,
        "note": None,
        "observed_at": observed_at,
    }


class ActivityCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.control = self.root / "control"
        self.runtime = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _submit(
        self,
        *,
        event: dict[str, object] | None = None,
        request_id: str = "request-0001",
        correlation_id: str = "correlation-0001",
        expected_revision: int = 0,
        received_at: str = "2026-08-26T10:00:02Z",
        actor: ProviderActor = ACTOR,
    ) -> dict[str, object]:
        return submit_activity_event(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
            event_value=event or _event(event_id="event-0001"),
            request_id=request_id,
            correlation_id=correlation_id,
            expected_revision=expected_revision,
            authorized_source="local_aria",
            authorized_provider="github",
            authorized_actor=actor,
            received_at=received_at,
        )

    def test_persists_activity_and_durable_receipt(self) -> None:
        result = self._submit()
        self.assertTrue(result["applied"])
        self.assertEqual(result["revision"], 1)
        paths = activity_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        self.assertEqual(load_activity(paths.activity), result["snapshot"])
        receipts = json.loads(paths.receipts.read_text(encoding="utf-8"))
        self.assertEqual(len(receipts["receipts"]), 1)
        self.assertEqual(receipts["receipts"][0]["request_id"], "request-0001")
        self.assertRegex(receipts["receipts"][0]["request_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertFalse(paths.transaction.exists())

    def test_duplicate_request_is_noop_and_content_reuse_fails(self) -> None:
        first = self._submit()
        duplicate = self._submit(expected_revision=0)
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_request")
        self.assertEqual(duplicate["revision"], first["revision"])

        renamed = self._submit(
            expected_revision=0,
            actor=ProviderActor(user_id="42", username_snapshot="yura-renamed"),
        )
        self.assertEqual(renamed["reason"], "duplicate_request")

        with self.assertRaisesRegex(WorkflowError, "different content"):
            self._submit(
                event=_event(
                    event_id="event-0001",
                    observed_at="2026-08-26T10:00:01Z",
                ),
                expected_revision=0,
            )
        with self.assertRaisesRegex(WorkflowError, "different content"):
            self._submit(expected_revision=1)

        changed = _event(
            event_id="event-0002",
            stage="planning",
            observed_at="2026-08-26T10:01:00Z",
        )
        with self.assertRaisesRegex(WorkflowError, "different content"):
            self._submit(event=changed, expected_revision=1)

    def test_legacy_receipt_without_request_fingerprint_remains_readable(self) -> None:
        self._submit()
        paths = activity_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        receipts = json.loads(paths.receipts.read_text(encoding="utf-8"))
        receipts["receipts"][0].pop("request_fingerprint")
        paths.receipts.write_text(
            json.dumps(receipts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        duplicate = self._submit(expected_revision=0)
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_request")

    def test_two_same_revision_writers_have_exactly_one_winner(self) -> None:
        def attempt(index: int) -> tuple[str, object]:
            try:
                result = self._submit(
                    event=_event(
                        event_id=f"event-race-{index}",
                        task_id=f"BLG-10{index}",
                    ),
                    request_id=f"request-race-{index}",
                    correlation_id=f"correlation-race-{index}",
                )
                return "ok", result
            except WorkflowError as error:
                return "error", str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [1, 2]))
        winners = [value for status, value in results if status == "ok"]
        losers = [value for status, value in results if status == "error"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn("Stale activity revision", losers[0])
        paths = activity_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        self.assertEqual(load_activity(paths.activity)["revision"], 1)

    def test_prepared_transaction_recovers_after_partial_write(self) -> None:
        paths = activity_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        original_install = coordinator_module._install
        failed = False

        def fail_receipt_once(path: Path, content: bytes) -> None:
            nonlocal failed
            if path == paths.receipts and not failed:
                failed = True
                raise OSError("simulated crash after activity write")
            original_install(path, content)

        with mock.patch.object(
            coordinator_module,
            "_install",
            side_effect=fail_receipt_once,
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self._submit()
        self.assertTrue(paths.transaction.exists())
        self.assertTrue(paths.activity.exists())
        self.assertFalse(paths.receipts.exists())

        recovered = self._submit(expected_revision=0)
        self.assertTrue(recovered["recovered"])
        self.assertFalse(recovered["applied"])
        self.assertEqual(recovered["reason"], "duplicate_request")
        self.assertEqual(recovered["revision"], 1)
        self.assertTrue(paths.receipts.exists())
        self.assertFalse(paths.transaction.exists())

    def test_recovery_fails_closed_on_unrecognized_document_state(self) -> None:
        paths = activity_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        original_install = coordinator_module._install
        failed = False

        def fail_receipt_once(path: Path, content: bytes) -> None:
            nonlocal failed
            if path == paths.receipts and not failed:
                failed = True
                raise OSError("simulated crash")
            original_install(path, content)

        with mock.patch.object(
            coordinator_module,
            "_install",
            side_effect=fail_receipt_once,
        ):
            with self.assertRaises(OSError):
                self._submit()
        paths.activity.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "outside the prepared transaction"):
            self._submit()

    def test_receipt_compaction_keeps_hash_archive_and_replay_protection(self) -> None:
        stages = ("analysis", "planning", "implementation")
        with mock.patch.object(
            coordinator_module, "MAX_ACTIVE_RECEIPTS", 2
        ), mock.patch.object(coordinator_module, "TARGET_ACTIVE_RECEIPTS", 1):
            for index, stage in enumerate(stages):
                self._submit(
                    event=_event(
                        event_id=f"event-compact-{index}",
                        stage=stage,
                        observed_at=f"2026-08-26T10:0{index}:00Z",
                    ),
                    request_id=f"request-compact-{index}",
                    correlation_id=f"correlation-compact-{index}",
                    expected_revision=index,
                    received_at=f"2026-08-26T10:0{index}:01Z",
                )
            paths = activity_coordinator_paths(
                control_root=self.control,
                runtime_root=self.runtime,
                project_id="demo",
            )
            archive = json.loads(paths.archive.read_text(encoding="utf-8"))
            receipts = json.loads(paths.receipts.read_text(encoding="utf-8"))
            self.assertEqual(len(archive["events"]), 2)
            self.assertEqual(len(receipts["receipts"]), 1)
            replay = self._submit(
                event=_event(event_id="event-compact-0", stage="analysis"),
                request_id="request-compact-0",
                correlation_id="correlation-compact-0",
                expected_revision=0,
                received_at="2026-08-26T10:00:01Z",
            )
            self.assertFalse(replay["applied"])
            self.assertEqual(replay["reason"], "duplicate_request")

            archive["events"][0]["receipt"]["event_id"] = "event-tampered"
            paths.archive.write_text(json.dumps(archive), encoding="utf-8")
            with self.assertRaisesRegex(WorkflowError, "archive hash"):
                self._submit(
                    event=_event(
                        event_id="event-after-tamper",
                        stage="testing",
                        observed_at="2026-08-26T10:03:00Z",
                    ),
                    request_id="request-after-tamper",
                    correlation_id="correlation-after-tamper",
                    expected_revision=3,
                    received_at="2026-08-26T10:03:01Z",
                )

    def test_compaction_recovers_archive_written_before_active_ledger(self) -> None:
        paths = activity_coordinator_paths(
            control_root=self.control,
            runtime_root=self.runtime,
            project_id="demo",
        )
        original_install = coordinator_module._install
        archive_written = False

        def fail_compacted_receipts_once(path: Path, content: bytes) -> None:
            nonlocal archive_written
            if path == paths.archive:
                archive_written = True
            if path == paths.receipts and archive_written:
                archive_written = False
                raise OSError("simulated crash during receipt compaction")
            original_install(path, content)

        with mock.patch.object(
            coordinator_module, "MAX_ACTIVE_RECEIPTS", 2
        ), mock.patch.object(coordinator_module, "TARGET_ACTIVE_RECEIPTS", 1):
            for index, stage in enumerate(("analysis", "planning")):
                self._submit(
                    event=_event(
                        event_id=f"event-recover-{index}",
                        stage=stage,
                        observed_at=f"2026-08-26T10:0{index}:00Z",
                    ),
                    request_id=f"request-recover-{index}",
                    correlation_id=f"correlation-recover-{index}",
                    expected_revision=index,
                    received_at=f"2026-08-26T10:0{index}:01Z",
                )
            third = _event(
                event_id="event-recover-2",
                stage="implementation",
                observed_at="2026-08-26T10:02:00Z",
            )
            with mock.patch.object(
                coordinator_module, "_install", side_effect=fail_compacted_receipts_once
            ):
                with self.assertRaisesRegex(OSError, "during receipt compaction"):
                    self._submit(
                        event=third,
                        request_id="request-recover-2",
                        correlation_id="correlation-recover-2",
                        expected_revision=2,
                        received_at="2026-08-26T10:02:01Z",
                    )
            recovered = self._submit(
                event=third,
                request_id="request-recover-2",
                correlation_id="correlation-recover-2",
                expected_revision=2,
                received_at="2026-08-26T10:02:01Z",
            )
            self.assertEqual(recovered["reason"], "duplicate_request")
            archive = json.loads(paths.archive.read_text(encoding="utf-8"))
            receipts = json.loads(paths.receipts.read_text(encoding="utf-8"))
            self.assertEqual(len(archive["events"]), 2)
            self.assertEqual(len(receipts["receipts"]), 1)


if __name__ == "__main__":
    unittest.main()
