from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from aria.collaborative_backlog import (
    ProviderIdentity,
    apply_backlog_request,
    backlog_audit_view,
    collaborative_backlog_template,
    dump_collaborative_backlog,
    load_collaborative_backlog,
    next_available_item_number,
    validate_collaborative_backlog,
)
from aria.errors import WorkflowError
from aria.provider import ProviderActor


ARAM = ProviderIdentity("github", ProviderActor("100", "aram", "Aram"))
YURA = ProviderIdentity("github", ProviderActor("200", "yura", "Yura"))
COORDINATOR = ProviderIdentity(
    "github-app", ProviderActor("900", "aria-coordinator", "ARIA Coordinator")
)
MEMBERS = (ARAM, YURA)
ALL_PERMISSIONS = frozenset(
    {
        "backlog.add",
        "backlog.triage",
        "backlog.assign",
        "backlog.claim",
        "backlog.block",
        "backlog.complete",
        "backlog.cancel",
        "backlog.amend_scope",
        "team.sync",
    }
)


def _request(
    *,
    request_id: str,
    action: str,
    item_id: str | None = None,
    payload: dict[str, object] | None = None,
    minute: int = 0,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "correlation_id": f"correlation-{request_id}",
        "project_id": "demo",
        "action": action,
        "item_id": item_id,
        "payload": payload or {},
        "requested_at": f"2026-08-26T10:{minute:02d}:00Z",
    }


def _apply(
    backlog: dict[str, object],
    request: dict[str, object],
    *,
    actor: ProviderIdentity = ARAM,
    permissions: frozenset[str] = ALL_PERMISSIONS,
    revision: int | None = None,
    minute: int = 0,
    acceptance_verified: bool = False,
) -> dict[str, object]:
    return apply_backlog_request(
        backlog,
        request,
        authenticated_actor=actor,
        active_members=MEMBERS,
        permissions=permissions,
        coordinator=COORDINATOR,
        expected_revision=int(backlog["revision"]) if revision is None else revision,
        committed_at=f"2026-08-26T10:{minute:02d}:02Z",
        acceptance_verified=acceptance_verified,
    )


def _add(backlog: dict[str, object]) -> dict[str, object]:
    return _apply(
        backlog,
        _request(
            request_id="request-add-0001",
            action="add",
            payload={
                "title": "Добавить экспорт",
                "description": "Идея без исполнителя",
                "priority": "P1",
                "source_id": "idea-telegram-42",
                "dependencies": [],
                "evidence_required": True,
            },
        ),
    )


def _triage_payload() -> dict[str, object]:
    return {
        "assignee_provider": "github",
        "assignee_user_id": "200",
        "priority": "P1",
        "requirements": ["Экспорт CSV"],
        "acceptance_criteria": ["CI проходит", "Файл скачивается"],
        "dependencies": [],
        "scope_paths": ["src/export", "tests/export"],
        "evidence_required": True,
    }


def _triage(backlog: dict[str, object], *, item_id: str = "BLG-000001") -> dict[str, object]:
    return _apply(
        backlog,
        _request(
            request_id=f"request-triage-{item_id.lower()}",
            action="triage",
            item_id=item_id,
            payload=_triage_payload(),
            minute=1,
        ),
        minute=1,
    )


def _complete_payload() -> dict[str, object]:
    return {
        "evidence_refs": ["ci:run-123"],
        "pull_request": 42,
        "merge_commit": "a" * 40,
        "source_commit": "a" * 40,
        "source_branch": "work/yura",
        "changed_paths": ["src/export/writer.py", "tests/export/test_writer.py"],
    }


def _review_payload() -> dict[str, object]:
    return {
        "pull_request": 42,
        "head_commit": "a" * 40,
        "source_branch": "work/yura",
        "changed_paths": ["src/export/writer.py", "tests/export/test_writer.py"],
    }


def _hash(value: object) -> str:
    content = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _rehash_head(backlog: dict[str, object]) -> None:
    event = backlog["events"][-1]
    event["items_sha256"] = _hash(backlog["items"])
    event["event_sha256"] = _hash(
        {key: value for key, value in event.items() if key != "event_sha256"}
    )


class CollaborativeBacklogTests(unittest.TestCase):
    def test_next_item_number_skips_only_occupied_generated_ids(self) -> None:
        self.assertEqual(next_available_item_number([]), 1)
        self.assertEqual(next_available_item_number([{"id": "BLG-LEGACY-42"}]), 1)
        self.assertEqual(
            next_available_item_number(
                [
                    {"id": "BLG-000001"},
                    {"id": "BLG-LEGACY-42"},
                    {"id": "BLG-000003"},
                ]
            ),
            2,
        )

    def test_migrated_stable_id_does_not_consume_generated_number(self) -> None:
        backlog = _add(collaborative_backlog_template("demo"))["backlog"]
        backlog["items"][0]["id"] = "BLG-LEGACY-42"
        backlog["events"][0]["item_ids"] = ["BLG-LEGACY-42"]
        backlog["next_item_number"] = 1
        _rehash_head(backlog)
        validate_collaborative_backlog(backlog)

        result = _apply(
            backlog,
            _request(
                request_id="request-add-after-migration",
                action="add",
                payload={
                    "title": "Новая идея",
                    "description": "После миграции",
                    "priority": "P2",
                    "source_id": None,
                    "dependencies": [],
                    "evidence_required": False,
                },
            ),
        )
        self.assertEqual(result["item"]["id"], "BLG-000001")
        self.assertEqual(result["backlog"]["next_item_number"], 2)

    def test_unassigned_idea_preserves_real_creator_and_coordinator(self) -> None:
        result = _add(collaborative_backlog_template("demo"))
        item = result["item"]
        self.assertEqual(item["id"], "BLG-000001")
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["kind"], "idea")
        self.assertIsNone(item["assignee"])
        self.assertEqual(item["creator"]["user_id"], "100")
        event = result["backlog"]["events"][0]
        self.assertEqual(event["requested_by"]["user_id"], "100")
        self.assertEqual(event["committed_by"]["user_id"], "900")

    def test_owner_recovers_legacy_active_task_then_amends_and_cancels(self) -> None:
        backlog = _triage(_add(collaborative_backlog_template("demo"))["backlog"])[
            "backlog"
        ]
        item = backlog["items"][0]
        for key in list(item):
            if key not in {
                "id", "title", "description", "priority", "status", "source_id",
                "dependencies", "evidence_required", "evidence_refs", "creator",
                "assignee", "blocked_reason", "created_at", "updated_at",
            }:
                item.pop(key)
        _rehash_head(backlog)
        recovered = _apply(
            backlog,
            _request(
                request_id="request-recover-legacy-0001",
                action="recover",
                item_id=item["id"],
                payload={
                    "requirements": ["Preserve legacy task"],
                    "acceptance_criteria": ["Owner accepts recovery"],
                    "scope_paths": ["src/legacy"],
                    "branch": "work/yura",
                    "target_status": "in_progress",
                },
                minute=2,
            ),
            permissions=frozenset({"team.sync"}),
            minute=2,
        )
        self.assertEqual(recovered["item"]["lease"]["branch"], "work/yura")
        amended = _apply(
            recovered["backlog"],
            _request(
                request_id="request-amend-scope-0001",
                action="amend_scope",
                item_id=item["id"],
                payload={"scope_paths": ["src/legacy", "tests/legacy"]},
                minute=3,
            ),
            minute=3,
        )
        self.assertEqual(amended["item"]["lease"]["scope_paths"], ["src/legacy", "tests/legacy"])
        cancelled = _apply(
            amended["backlog"],
            _request(
                request_id="request-cancel-0001",
                action="cancel",
                item_id=item["id"],
                payload={"reason": "Owner recovery decision"},
                minute=4,
            ),
            minute=4,
        )
        self.assertEqual(cancelled["item"]["status"], "cancelled")
        self.assertIsNone(cancelled["item"]["lease"])

    def test_assign_claim_block_complete_lifecycle_and_audit(self) -> None:
        backlog = _add(collaborative_backlog_template("demo"))["backlog"]
        triaged = _triage(backlog)
        backlog = triaged["backlog"]
        self.assertEqual(triaged["item"]["kind"], "task")
        self.assertEqual(triaged["item"]["scope_paths"], ["src/export", "tests/export"])
        steps = [
            (
                YURA,
                _request(
                    request_id="request-claim-0001",
                    action="claim",
                    item_id="BLG-000001",
                    payload={"branch": "work/yura"},
                    minute=2,
                ),
                "in_progress",
            ),
            (
                YURA,
                _request(
                    request_id="request-block-0001",
                    action="block",
                    item_id="BLG-000001",
                    payload={"reason": "Жду API"},
                    minute=3,
                ),
                "blocked",
            ),
        ]
        for minute, (actor, request, status) in enumerate(steps, start=1):
            result = _apply(backlog, request, actor=actor, minute=minute)
            backlog = result["backlog"]
            self.assertEqual(result["item"]["status"], status)
        reviewed = _apply(
            backlog,
            _request(
                request_id="request-review-0001", action="review",
                item_id="BLG-000001", payload=_review_payload(), minute=4,
            ),
            actor=YURA, minute=4, acceptance_verified=True,
        )
        backlog = reviewed["backlog"]
        self.assertEqual(reviewed["item"]["status"], "in_review")
        completed = _apply(
            backlog,
            _request(
                request_id="request-complete-0001",
                action="complete",
                item_id="BLG-000001",
                payload=_complete_payload(),
                minute=5,
            ),
            actor=YURA,
            minute=5,
            acceptance_verified=True,
        )
        self.assertEqual(completed["item"]["status"], "done")
        audit = backlog_audit_view(completed["backlog"])
        self.assertEqual([row["action"] for row in audit], ["add", "triage", "claim", "block", "review", "complete"])
        self.assertEqual(audit[-1]["requested_by"]["user_id"], "200")
        self.assertEqual(audit[-1]["committed_by"]["user_id"], "900")

    def test_claim_enforces_available_assignee_priority_order(self) -> None:
        backlog = _add(collaborative_backlog_template("demo"))["backlog"]
        backlog = _triage(backlog)["backlog"]
        added = _apply(
            backlog,
            _request(
                request_id="request-add-priority-0002",
                action="add",
                payload={
                    "title": "Срочный P0",
                    "description": "Должен быть начат первым",
                    "priority": "P0",
                    "source_id": None,
                    "dependencies": [],
                    "evidence_required": False,
                },
                minute=2,
            ),
            minute=2,
        )["backlog"]
        payload = {**_triage_payload(), "priority": "P0", "scope_paths": ["src/urgent"]}
        backlog = _apply(
            added,
            _request(
                request_id="request-triage-priority-0002",
                action="triage",
                item_id="BLG-000002",
                payload=payload,
                minute=3,
            ),
            minute=3,
        )["backlog"]
        with self.assertRaisesRegex(WorkflowError, "higher-priority"):
            _apply(
                backlog,
                _request(
                    request_id="request-claim-priority-0001",
                    action="claim",
                    item_id="BLG-000001",
                    payload={"branch": "work/yura"},
                    minute=4,
                ),
                actor=YURA,
                minute=4,
            )
        claimed = _apply(
            backlog,
            _request(
                request_id="request-claim-priority-0002",
                action="claim",
                item_id="BLG-000002",
                payload={"branch": "work/yura"},
                minute=4,
            ),
            actor=YURA,
            minute=4,
        )
        self.assertEqual(claimed["item"]["status"], "in_progress")

    def test_permissions_membership_and_assignee_are_fail_closed(self) -> None:
        backlog = _add(collaborative_backlog_template("demo"))["backlog"]
        backlog = _triage(backlog)["backlog"]
        claim = _request(
            request_id="request-claim-0001",
            action="claim",
            item_id="BLG-000001",
            payload={"branch": "work/yura"},
        )
        with self.assertRaisesRegex(WorkflowError, "not permitted"):
            _apply(backlog, claim, actor=YURA, permissions=frozenset())
        outsider = ProviderIdentity("github", ProviderActor("300", "outsider"))
        with self.assertRaisesRegex(WorkflowError, "not an active"):
            _apply(backlog, claim, actor=outsider)

        with self.assertRaisesRegex(WorkflowError, "assigned to another"):
            _apply(backlog, {**claim, "payload": {"branch": "work/aram"}}, actor=ARAM)

    def test_membership_readback_controls_mutable_username_snapshot(self) -> None:
        spoofed = ProviderIdentity("github", ProviderActor("200", "not-yura"))
        result = _apply(
            collaborative_backlog_template("demo"),
            _request(
                request_id="request-add-0001",
                action="add",
                payload={
                    "title": "Идея Юры",
                    "description": "Проверка identity read-back",
                    "priority": "P2",
                    "source_id": None,
                    "dependencies": [],
                    "evidence_required": False,
                },
            ),
            actor=spoofed,
        )
        self.assertEqual(result["item"]["creator"]["username_snapshot"], "yura")
        self.assertEqual(
            result["backlog"]["events"][0]["requested_by"]["username_snapshot"],
            "yura",
        )

    def test_completion_requires_assignee_and_evidence(self) -> None:
        backlog = _add(collaborative_backlog_template("demo"))["backlog"]
        backlog = _triage(backlog)["backlog"]
        complete = _request(
            request_id="request-complete-0001",
            action="complete",
            item_id="BLG-000001",
            payload={**_complete_payload(), "evidence_refs": []},
        )
        with self.assertRaisesRegex(WorkflowError, "verified PR"):
            _apply(backlog, complete, actor=YURA)
        claimed = _apply(
            backlog,
            _request(
                request_id="request-claim-0001",
                action="claim",
                item_id="BLG-000001",
                payload={"branch": "work/yura"},
            ),
            actor=YURA,
        )["backlog"]
        claimed = _apply(
            claimed,
            _request(
                request_id="request-review-evidence-0001", action="review",
                item_id="BLG-000001", payload=_review_payload(),
            ),
            actor=YURA, acceptance_verified=True,
        )["backlog"]
        with self.assertRaisesRegex(WorkflowError, "requires evidence"):
            _apply(claimed, complete, actor=YURA, acceptance_verified=True)

    def test_branch_scope_dependency_and_merge_boundaries_are_fail_closed(self) -> None:
        backlog = _triage(
            _add(collaborative_backlog_template("demo"))["backlog"]
        )["backlog"]
        wrong_branch = _request(
            request_id="request-claim-wrong-branch",
            action="claim",
            item_id="BLG-000001",
            payload={"branch": "work/aram"},
        )
        with self.assertRaisesRegex(WorkflowError, "work/yura"):
            _apply(backlog, wrong_branch, actor=YURA)
        claimed = _apply(
            backlog,
            {**wrong_branch, "request_id": "request-claim-scope-0001", "payload": {"branch": "work/yura"}},
            actor=YURA,
        )["backlog"]
        outside = _request(
            request_id="request-review-outside-scope",
            action="review",
            item_id="BLG-000001",
            payload={**_review_payload(), "changed_paths": ["README.md"]},
        )
        with self.assertRaisesRegex(WorkflowError, "outside task scope"):
            _apply(claimed, outside, actor=YURA, acceptance_verified=True)

        second = _apply(
            claimed,
            _request(
                request_id="request-add-0002",
                action="add",
                payload={
                    "title": "Вторая идея",
                    "description": "Пересекающаяся работа",
                    "priority": "P1",
                    "source_id": None,
                    "dependencies": [],
                    "evidence_required": False,
                },
            ),
        )["backlog"]
        triage_payload = {
            **_triage_payload(),
            "dependencies": ["BLG-000001"],
            "scope_paths": ["src/export/templates"],
        }
        second = _apply(
            second,
            _request(
                request_id="request-triage-0002",
                action="triage",
                item_id="BLG-000002",
                payload=triage_payload,
            ),
        )["backlog"]
        with self.assertRaisesRegex(WorkflowError, "dependencies are incomplete"):
            _apply(
                second,
                _request(
                    request_id="request-claim-0002",
                    action="claim",
                    item_id="BLG-000002",
                    payload={"branch": "work/yura"},
                ),
                actor=YURA,
            )

        second["items"][1]["dependencies"] = []
        _rehash_head(second)
        with self.assertRaisesRegex(WorkflowError, "scope conflicts"):
            _apply(
                second,
                _request(
                    request_id="request-claim-0002-scope",
                    action="claim",
                    item_id="BLG-000002",
                    payload={"branch": "work/yura"},
                ),
                actor=YURA,
            )

    def test_duplicate_request_is_noop_but_changed_content_is_rejected(self) -> None:
        template = collaborative_backlog_template("demo")
        request = _request(
            request_id="request-add-0001",
            action="add",
            payload={
                "title": "Идея",
                "description": "Описание",
                "priority": "P2",
                "source_id": None,
                "dependencies": [],
                "evidence_required": False,
            },
        )
        backlog = _apply(template, request)["backlog"]
        duplicate = _apply(backlog, request, revision=0)
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_request")
        changed = copy.deepcopy(request)
        changed["payload"]["title"] = "Другая идея"
        with self.assertRaisesRegex(WorkflowError, "different content"):
            _apply(backlog, changed)

        retried_later = copy.deepcopy(request)
        retried_later["requested_at"] = "2026-08-26T10:59:00Z"
        duplicate = _apply(backlog, retried_later, revision=0)
        self.assertFalse(duplicate["applied"])
        self.assertEqual(duplicate["reason"], "duplicate_request")

    def test_legacy_open_item_can_be_safely_triaged_with_explicit_scope(self) -> None:
        backlog = _add(collaborative_backlog_template("demo"))["backlog"]
        for key in (
            "kind", "triage_owner", "requirements", "acceptance_criteria",
            "scope_paths", "lease", "pull_request", "accepted_commit",
        ):
            backlog["items"][0].pop(key)
        _rehash_head(backlog)
        migrated = _apply(
            backlog,
            _request(
                request_id="request-triage-legacy-0001",
                action="triage",
                item_id="BLG-000001",
                payload=_triage_payload(),
            ),
        )["backlog"]
        self.assertEqual(migrated["items"][0]["kind"], "task")
        self.assertEqual(migrated["items"][0]["scope_paths"], ["src/export", "tests/export"])

    def test_stale_revision_rejected(self) -> None:
        backlog = _add(collaborative_backlog_template("demo"))["backlog"]
        with self.assertRaisesRegex(WorkflowError, "Stale collaborative"):
            _apply(
                backlog,
                _request(
                    request_id="request-claim-0001",
                    action="claim",
                    item_id="BLG-000001",
                    payload={"branch": "work/yura"},
                ),
                actor=YURA,
                revision=0,
            )

    def test_tamper_and_round_trip(self) -> None:
        backlog = _add(collaborative_backlog_template("demo"))["backlog"]
        content = dump_collaborative_backlog(backlog)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "BACKLOG.yaml"
            path.write_text(content, encoding="utf-8")
            self.assertEqual(load_collaborative_backlog(path), backlog)
        tampered = copy.deepcopy(backlog)
        tampered["items"][0]["title"] = "Подмена"
        with self.assertRaisesRegex(WorkflowError, "audit head"):
            validate_collaborative_backlog(tampered)
        tampered = copy.deepcopy(backlog)
        tampered["events"][0]["requested_by"]["user_id"] = "999"
        with self.assertRaisesRegex(WorkflowError, "event hash"):
            validate_collaborative_backlog(tampered)


if __name__ == "__main__":
    unittest.main()
