from __future__ import annotations

import copy
import inspect
import unittest

from aria.errors import ConfigurationError, WorkflowError
from aria.github import GitHubRepository
from aria.github_request_queue import (
    GitHubRequestIssue,
    GitHubRequestQueue,
    dump_queue_request,
    load_queue_request,
)
from aria.provider import ProviderActor


ACTOR = ProviderActor("200", "yura")


def _request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_id": "backlog-r1-0123456789abcdef",
        "project_id": "demo",
        "kind": "backlog",
        "expected_revision": 0,
        "operation": {
            "action": "add",
            "item_id": None,
            "payload": {"title": "Idea"},
        },
        "submitted_at": "2026-08-26T12:00:00Z",
    }


class _Transport:
    def __init__(self) -> None:
        self.issues: dict[int, dict[str, object]] = {}
        self.comments: list[tuple[int, str]] = []
        self.comment_rows: dict[int, list[dict[str, object]]] = {}
        self.next_number = 1

    def _issue(
        self, number: int, *, title: str, body: str, state: str = "open",
        labels: list[str] | None = None,
    ):
        return {
            "id": 1000 + number,
            "node_id": f"I_kwDO{number}",
            "number": number,
            "state": state,
            "title": title,
            "body": body,
            "user": {"id": 200, "login": "yura"},
            "updated_at": "2026-08-26T12:00:01Z",
            "labels": [{"name": name} for name in (labels or [])],
        }

    def post_json(self, path: str, payload: object):
        assert isinstance(payload, dict)
        if path == "/graphql":
            node_id = str(payload["variables"]["id"])
            issue = next(row for row in self.issues.values() if row["node_id"] == node_id)
            return {
                "data": {
                    "node": {
                        "id": issue["node_id"],
                        "number": issue["number"],
                        "title": issue["title"],
                        "body": issue["body"],
                        "state": str(issue["state"]).upper(),
                        "lastEditedAt": issue.get("lastEditedAt"),
                        "author": {"login": issue["user"]["login"]},
                    }
                }
            }
        if path.endswith("/issues"):
            number = self.next_number
            self.next_number += 1
            issue = self._issue(
                number, title=str(payload["title"]), body=str(payload["body"]),
                labels=list(payload.get("labels", [])),
            )
            self.issues[number] = issue
            return copy.deepcopy(issue)
        if "/comments" in path:
            number = int(path.split("/")[-2])
            self.comments.append((number, str(payload["body"])))
            row = {
                "id": len(self.comments),
                "body": str(payload["body"]),
                "created_at": "2026-08-26T12:00:02Z",
                "updated_at": "2026-08-26T12:00:02Z",
                "performed_via_github_app": {"id": 9001},
            }
            self.comment_rows.setdefault(number, []).append(row)
            return copy.deepcopy(row)
        raise AssertionError(path)

    def get_json(self, path: str):
        if "/comments?" in path:
            number = int(path.split("/comments", 1)[0].rsplit("/", 1)[1])
            page = int(path.rsplit("page=", 1)[1])
            return copy.deepcopy(self.comment_rows.get(number, [])) if page == 1 else []
        if "?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return list(self.issues.values()) if page == 1 else []
        number = int(path.rsplit("/", 1)[1])
        return copy.deepcopy(self.issues[number])

    def patch_json(self, path: str, payload: object):
        number = int(path.rsplit("/", 1)[1])
        self.issues[number]["state"] = "closed"
        self.issues[number]["state_reason"] = payload["state_reason"]
        return copy.deepcopy(self.issues[number])


class _PagedTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.issue_pages: list[int] = []

    def get_json(self, path: str):
        if "?state=" in path:
            page = int(path.rsplit("page=", 1)[1])
            self.issue_pages.append(page)
            values = sorted(
                self.issues.values(), key=lambda row: int(row["number"]), reverse=True
            )
            start = (page - 1) * 100
            return copy.deepcopy(values[start:start + 100])
        return super().get_json(path)


class GitHubRequestQueueTests(unittest.TestCase):
    def test_issue_labels_field_is_source_compatible(self) -> None:
        self.assertIsNot(
            inspect.signature(GitHubRequestIssue).parameters["labels"].default,
            inspect.Parameter.empty,
        )
        issue = GitHubRequestIssue(
            1,
            1001,
            "I_kwDO1",
            "open",
            "title",
            "body",
            ACTOR,
            "2026-08-26T12:00:01Z",
            None,
        )
        self.assertEqual(issue.labels, ())

    def test_open_app_acceptance_is_intent_but_not_terminal_receipt(self) -> None:
        issue = self.queue.submit(_request(), expected_actor=ACTOR)
        commit = "a" * 40
        self.queue.comment(
            issue.number,
            "ARIA accepted "
            f"request_id={_request()['request_id']} "
            f"body_sha256={issue.body_sha256} control_commit={commit}",
        )
        issue = self.queue.read(issue.number)
        self.assertIsNone(
            self.queue.acceptance_receipt(
                issue,
                _request(),
                coordinator_integration_id=9001,
            )
        )
        self.assertEqual(
            self.queue.acceptance_intent(
                issue,
                _request(),
                coordinator_integration_id=9001,
            ),
            commit,
        )

    def setUp(self) -> None:
        self.transport = _Transport()
        self.queue = GitHubRequestQueue(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
        )

    def test_submit_list_read_comment_close_round_trip(self) -> None:
        issue = self.queue.submit(_request(), expected_actor=ACTOR)
        self.assertEqual(issue.number, 1)
        self.assertEqual(load_queue_request(issue.body), _request())
        self.assertEqual(self.queue.read(1), issue)
        self.assertEqual(issue.labels, ("aria:backlog", "aria:request"))
        self.queue.verify_immutable(issue)
        self.assertEqual(self.queue.list_open(project_id="demo"), (issue,))
        duplicate = self.queue.submit(_request(), expected_actor=ACTOR)
        self.assertEqual(duplicate, issue)
        self.assertEqual(self.transport.next_number, 2)
        self.queue.comment(
            1,
            "ARIA accepted "
            f"request_id={_request()['request_id']} "
            f"body_sha256={issue.body_sha256} "
            f"control_commit={'a' * 40}",
        )
        self.queue.close(1)
        self.assertEqual(self.queue.read(1).state, "closed")
        receipt = self.queue.acceptance_receipt(
            self.queue.read(1), _request(), coordinator_integration_id=9001
        )
        self.assertEqual(receipt, "a" * 40)
        accepted_duplicate = self.queue.submit(_request(), expected_actor=ACTOR)
        self.assertEqual(accepted_duplicate.number, 1)
        self.assertEqual(self.transport.next_number, 2)

    def test_submit_rejects_author_readback_mismatch(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "read-back mismatch"):
            self.queue.submit(
                _request(), expected_actor=ProviderActor("999", "outsider")
            )

    def test_schema_and_body_are_strict(self) -> None:
        changed = _request()
        changed["unexpected"] = True
        with self.assertRaisesRegex(ConfigurationError, "schema"):
            dump_queue_request(changed)
        with self.assertRaisesRegex(ConfigurationError, "valid JSON"):
            load_queue_request("not-json")

    def test_edited_issue_is_rejected_even_when_body_was_restored(self) -> None:
        issue = self.queue.submit(_request(), expected_actor=ACTOR)
        self.transport.issues[issue.number]["lastEditedAt"] = "2026-08-26T12:01:00Z"
        with self.assertRaisesRegex(WorkflowError, "edited after creation"):
            self.queue.verify_immutable(self.queue.read(issue.number))

    def test_reopened_terminal_issue_cannot_be_resubmitted(self) -> None:
        issue = self.queue.submit(_request(), expected_actor=ACTOR)
        self.queue.close(issue.number, state_reason="not_planned")
        self.transport.issues[issue.number]["state"] = "open"
        self.transport.issues[issue.number]["state_reason"] = "reopened"
        with self.assertRaisesRegex(WorkflowError, "reopened"):
            self.queue.submit(_request(), expected_actor=ACTOR)

    def test_queue_paginates_beyond_one_thousand_issues(self) -> None:
        transport = _PagedTransport()
        body = dump_queue_request(_request())
        for number in range(1, 1002):
            transport.issues[number] = transport._issue(
                number,
                title=f"ARIA request demo: backlog-r1-{number:016x}",
                body=body,
            )
        queue = GitHubRequestQueue(
            repository=GitHubRepository("acme", "product"), transport=transport
        )
        issues = queue.list_all(project_id="demo")
        self.assertEqual(len(issues), 1001)
        self.assertEqual(issues[0].number, 1001)
        self.assertEqual(issues[-1].number, 1)
        self.assertEqual(transport.issue_pages, list(range(1, 12)))

    def test_foreign_receipt_prefix_is_ignored_before_valid_app_receipt(self) -> None:
        issue = self.queue.submit(_request(), expected_actor=ACTOR)
        acceptance = (
            "ARIA accepted "
            f"request_id={_request()['request_id']} "
            f"body_sha256={issue.body_sha256} control_commit={'a' * 40}"
        )
        self.queue.comment(issue.number, acceptance)
        self.transport.comment_rows[issue.number].insert(
            0,
            {
                "body": acceptance,
                "created_at": "2026-08-26T12:00:02Z",
                "updated_at": "2026-08-26T12:00:02Z",
                "performed_via_github_app": None,
            },
        )
        self.queue.close(issue.number)
        self.assertEqual(
            self.queue.acceptance_receipt(
                self.queue.read(issue.number),
                _request(),
                coordinator_integration_id=9001,
            ),
            "a" * 40,
        )

        second_request = _request()
        second_request["request_id"] = "backlog-r1-1111111111111111"
        rejected = self.queue.submit(second_request, expected_actor=ACTOR)
        rejection = (
            "ARIA rejected "
            f"request_id={second_request['request_id']} "
            f"body_sha256={rejected.body_sha256} "
            f"without_apply=true reason_sha256={'b' * 64}"
        )
        self.queue.comment(rejected.number, rejection)
        self.transport.comment_rows[rejected.number].insert(
            0,
            {
                "body": rejection,
                "created_at": "2026-08-26T12:00:02Z",
                "updated_at": "2026-08-26T12:00:02Z",
                "performed_via_github_app": None,
            },
        )
        self.queue.close(rejected.number, state_reason="not_planned")
        self.assertEqual(
            self.queue.rejection_receipt(
                self.queue.read(rejected.number),
                second_request,
                coordinator_integration_id=9001,
            ),
            "b" * 64,
        )


if __name__ == "__main__":
    unittest.main()
