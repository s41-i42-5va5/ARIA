from __future__ import annotations

import copy
import unittest

from aria.errors import WorkflowError
from aria.github import GitHubApiError, GitHubRepository
from aria.github_integration import GitHubIntegrationVerifier


SHA = "a" * 40
BASE_SHA = "b" * 40


class _Transport:
    def __init__(self) -> None:
        self.values = {
            "/repos/acme/product": {
                "id": 123456789,
                "full_name": "acme/product",
            },
            "/repos/acme/product/branches/dev": {
                "name": "dev",
                "protected": True,
                "commit": {"sha": SHA},
            },
            "/repos/acme/product/pulls/17": {
                "number": 17,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-08-26T12:00:00Z",
                "merge_commit_sha": SHA,
                "body": "Implements the task.\n\nARIA-Backlog: BLG-000001\n",
                "base": {"ref": "dev", "sha": BASE_SHA, "repo": {"id": 123456789}},
                "head": {"ref": "work/yura", "sha": SHA, "repo": {"id": 123456789}},
                "user": {"id": 200, "login": "yura"},
            },
            "/repos/acme/product/pulls/17/files?per_page=100&page=1": [
                {"filename": "src/export/writer.py"},
                {"filename": "tests/export/test_writer.py"},
            ],
            "/repos/acme/product/pulls/19": {
                "number": 19,
                "state": "open",
                "updated_at": "2026-08-26T13:00:00Z",
                "body": "ARIA-Backlog: BLG-000002",
                "base": {"ref": "dev", "sha": BASE_SHA, "repo": {"id": 123456789}},
                "head": {"ref": "work/anton", "sha": SHA, "repo": {"id": 123456789}},
                "user": {"id": 201, "login": "anton"},
            },
            f"/repos/acme/product/compare/{BASE_SHA}...{SHA}": {
                "status": "ahead",
                "total_commits": 1,
                "commits": [{
                    "author": {"id": 201, "login": "anton"},
                    "committer": {"id": 201, "login": "anton"},
                    "commit": {"verification": {"verified": True, "reason": "valid"}},
                }],
                "files": [{"filename": "src/export/writer.py"}],
            },
            "/repos/acme/product/branches/dev/protection/required_status_checks": {
                "strict": True,
                "checks": [
                    {"context": "ARIA integration", "app_id": 9001},
                    {"context": "unit-tests", "app_id": None},
                ],
            },
            f"/repos/acme/product/commits/{SHA}/check-runs?filter=latest&per_page=100": {
                "total_count": 2,
                "check_runs": [
                    {
                        "name": "ARIA integration",
                        "status": "completed",
                        "conclusion": "success",
                        "app": {"id": 9001},
                    },
                    {
                        "name": "unit-tests",
                        "status": "completed",
                        "conclusion": "success",
                        "app": {"id": 8001},
                    },
                ],
            },
            f"/repos/acme/product/commits/{SHA}/status": {
                "state": "success",
                "statuses": [],
            },
            "/repos/acme/product/pulls?state=closed&base=dev&sort=updated&direction=asc&per_page=100&page=1": [
                {
                    "number": 18,
                    "merged_at": None,
                    "body": "ARIA-Backlog: BLG-000002",
                },
                {
                    "number": 17,
                    "merged_at": "2026-08-26T12:00:00Z",
                    "body": "ARIA-Backlog: BLG-000001",
                    "user": {"id": 200, "login": "yura"},
                    "head": {"ref": "work/yura"},
                },
                {
                    "number": 16,
                    "merged_at": "2026-08-26T11:00:00Z",
                    "body": "No ARIA task",
                },
            ],
            "/repos/acme/product/pulls?state=open&base=dev&sort=updated&direction=asc&per_page=100&page=1": [
                {
                    "number": 19,
                    "updated_at": "2026-08-26T13:00:00Z",
                    "body": "ARIA-Backlog: BLG-000002",
                    "user": {"id": 201, "login": "anton"},
                    "head": {"ref": "work/anton"},
                },
                {
                    "number": 20,
                    "updated_at": "2026-08-26T14:00:00Z",
                    "body": "No binding",
                    "user": {"id": 202, "login": "dmitry"},
                    "head": {"ref": "work/dmitry"},
                },
            ],
        }

    def get_json(self, path: str):
        value = self.values.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise AssertionError(path)
        return copy.deepcopy(value)

    def post_json(self, path: str, payload: object):
        if path == "/repos/acme/product/check-runs":
            return {"name": payload["name"], "head_sha": payload["head_sha"]}
        raise AssertionError(path)

    def patch_json(self, path: str, payload: object):
        raise AssertionError("unused")


class GitHubIntegrationVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = _Transport()
        self.verifier = GitHubIntegrationVerifier(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
        )

    def verify(self):
        return self.verifier.verify(
            repository_id="123456789",
            integration_branch="dev",
            pull_request_number=17,
        )

    def test_exact_merged_head_and_required_checks_are_accepted(self) -> None:
        result = self.verify()
        self.assertEqual(result.merge_commit, SHA)
        self.assertEqual(result.pull_request_author.user_id, "200")
        self.assertEqual(result.backlog_item_id, "BLG-000001")
        self.assertEqual(result.source_branch, "work/yura")
        self.assertEqual(
            result.changed_paths,
            ("src/export/writer.py", "tests/export/test_writer.py"),
        )
        self.assertEqual(
            [check.name for check in result.required_checks],
            ["ARIA integration", "unit-tests"],
        )

    def test_bound_merged_pull_requests_are_filtered_and_sorted(self) -> None:
        candidates = self.verifier.list_bound_merged_pull_requests(
            repository_id="123456789", integration_branch="dev"
        )
        self.assertEqual([candidate.number for candidate in candidates], [17])
        self.assertEqual(candidates[0].backlog_item_id, "BLG-000001")
        self.assertEqual(candidates[0].pull_request_author.user_id, "200")
        self.assertEqual(candidates[0].branch, "work/yura")

    def test_bound_open_pull_requests_include_verified_author_and_branch(self) -> None:
        candidates = self.verifier.list_bound_open_pull_requests(
            repository_id="123456789", integration_branch="dev"
        )
        self.assertEqual([candidate.number for candidate in candidates], [19])
        self.assertEqual(candidates[0].backlog_item_id, "BLG-000002")
        self.assertEqual(candidates[0].pull_request_author.user_id, "201")
        self.assertEqual(candidates[0].branch, "work/anton")

    def test_open_pr_is_checked_on_exact_signed_head_before_merge(self) -> None:
        verifier = GitHubIntegrationVerifier(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
            coordinator_integration_id=9001,
        )
        result = verifier.verify_open(
            repository_id="123456789",
            integration_branch="dev",
            pull_request_number=19,
            backlog_item_id="BLG-000002",
            expected_actor_id="201",
            expected_branch="work/anton",
            scope_paths=["src/export"],
        )
        self.assertEqual(result.head_sha, SHA)
        self.assertEqual(result.changed_paths, ("src/export/writer.py",))

    def test_rename_from_outside_scope_is_rejected(self) -> None:
        self.transport.values[
            f"/repos/acme/product/compare/{BASE_SHA}...{SHA}"
        ]["files"][0]["previous_filename"] = "secrets/token.txt"
        verifier = GitHubIntegrationVerifier(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(WorkflowError, "outside task scope"):
            verifier.verify_open(
                repository_id="123456789",
                integration_branch="dev",
                pull_request_number=19,
                backlog_item_id="BLG-000002",
                expected_actor_id="201",
                expected_branch="work/anton",
                scope_paths=["src/export"],
            )

    def test_unsigned_or_foreign_commit_cannot_receive_required_app_check(self) -> None:
        commits = self.transport.values[
            f"/repos/acme/product/compare/{BASE_SHA}...{SHA}"
        ]["commits"]
        commits[0]["commit"]["verification"]["verified"] = False
        verifier = GitHubIntegrationVerifier(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(WorkflowError, "validly signed"):
            verifier.verify_open(
                repository_id="123456789",
                integration_branch="dev",
                pull_request_number=19,
                backlog_item_id="BLG-000002",
                expected_actor_id="201",
                expected_branch="work/anton",
                scope_paths=["src/export"],
            )

    def test_saturated_or_truncated_compare_response_fails_closed(self) -> None:
        compare_path = f"/repos/acme/product/compare/{BASE_SHA}...{SHA}"
        for case in ("commit_count", "commit_page", "file_page"):
            with self.subTest(case=case):
                transport = _Transport()
                comparison = transport.values[compare_path]
                if case == "commit_count":
                    comparison["total_commits"] = 2
                elif case == "commit_page":
                    comparison["commits"] = [
                        copy.deepcopy(comparison["commits"][0]) for _ in range(250)
                    ]
                    comparison["total_commits"] = 250
                else:
                    comparison["files"] = [
                        {"filename": f"src/export/file-{index}.py"}
                        for index in range(300)
                    ]
                verifier = GitHubIntegrationVerifier(
                    repository=GitHubRepository("acme", "product"),
                    transport=transport,
                    coordinator_integration_id=9001,
                )
                with self.assertRaisesRegex(WorkflowError, "unavailable or too large"):
                    verifier.verify_open(
                        repository_id="123456789",
                        integration_branch="dev",
                        pull_request_number=19,
                        backlog_item_id="BLG-000002",
                        expected_actor_id="201",
                        expected_branch="work/anton",
                        scope_paths=["src/export"],
                    )

    def test_foreign_committer_cannot_receive_required_app_check(self) -> None:
        commits = self.transport.values[
            f"/repos/acme/product/compare/{BASE_SHA}...{SHA}"
        ]["commits"]
        commits[0]["committer"] = {"id": 999, "login": "attacker"}
        verifier = GitHubIntegrationVerifier(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(WorkflowError, "validly signed"):
            verifier.verify_open(
                repository_id="123456789",
                integration_branch="dev",
                pull_request_number=19,
                backlog_item_id="BLG-000002",
                expected_actor_id="201",
                expected_branch="work/anton",
                scope_paths=["src/export"],
            )

    def test_branch_protection_must_pin_coordinator_app_check(self) -> None:
        checks = self.transport.values[
            "/repos/acme/product/branches/dev/protection/required_status_checks"
        ]["checks"]
        checks[0]["app_id"] = 7777
        verifier = GitHubIntegrationVerifier(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
            coordinator_integration_id=9001,
        )
        with self.assertRaisesRegex(WorkflowError, "coordinator App"):
            verifier.verify_open(
                repository_id="123456789",
                integration_branch="dev",
                pull_request_number=19,
                backlog_item_id="BLG-000002",
                expected_actor_id="201",
                expected_branch="work/anton",
                scope_paths=["src/export"],
            )

    def test_open_or_unreachable_pull_request_is_rejected(self) -> None:
        self.transport.values["/repos/acme/product/pulls/17"]["merged"] = False
        with self.assertRaisesRegex(WorkflowError, "not merged into"):
            self.verify()
        self.transport = _Transport()
        self.transport.values["/repos/acme/product/branches/dev"]["commit"]["sha"] = "b" * 40
        self.transport.values[
            f"/repos/acme/product/compare/{SHA}...{'b' * 40}"
        ] = {
            "status": "diverged",
            "ahead_by": 1,
            "merge_base_commit": {"sha": "c" * 40},
        }
        self.verifier = GitHubIntegrationVerifier(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
        )
        with self.assertRaisesRegex(WorkflowError, "absent from remote"):
            self.verify()

    def test_pull_request_must_use_same_repository_work_branch(self) -> None:
        self.transport.values["/repos/acme/product/pulls/17"]["head"]["repo"]["id"] = 987
        with self.assertRaisesRegex(WorkflowError, "not merged into"):
            self.verify()

    def test_failed_required_check_does_not_publish(self) -> None:
        runs = self.transport.values[
            f"/repos/acme/product/commits/{SHA}/check-runs?filter=latest&per_page=100"
        ]["check_runs"]
        runs[0]["conclusion"] = "failure"
        with self.assertRaisesRegex(WorkflowError, "did not pass: ARIA integration"):
            self.verify()

    def test_missing_or_non_strict_required_checks_do_not_publish(self) -> None:
        path = "/repos/acme/product/branches/dev/protection/required_status_checks"
        self.transport.values[path] = GitHubApiError("missing", status=404)
        with self.assertRaisesRegex(WorkflowError, "no required status checks"):
            self.verify()
        self.transport = _Transport()
        self.transport.values[path]["strict"] = False
        self.verifier = GitHubIntegrationVerifier(
            repository=GitHubRepository("acme", "product"),
            transport=self.transport,
        )
        with self.assertRaisesRegex(WorkflowError, "not strict"):
            self.verify()

    def test_pull_request_requires_one_exact_backlog_binding(self) -> None:
        self.transport.values["/repos/acme/product/pulls/17"]["body"] = "No binding"
        with self.assertRaisesRegex(WorkflowError, "ARIA-Backlog"):
            self.verify()

    def test_legacy_commit_status_can_satisfy_unbound_context(self) -> None:
        runs_path = (
            f"/repos/acme/product/commits/{SHA}/check-runs?filter=latest&per_page=100"
        )
        self.transport.values[runs_path]["check_runs"] = [
            self.transport.values[runs_path]["check_runs"][0]
        ]
        self.transport.values[f"/repos/acme/product/commits/{SHA}/status"]["statuses"] = [
            {"context": "unit-tests", "state": "success"}
        ]
        self.assertEqual(self.verify().merge_commit, SHA)

    def test_previous_accepted_head_must_be_ancestor(self) -> None:
        previous = "b" * 40
        path = f"/repos/acme/product/compare/{previous}...{SHA}"
        self.transport.values[path] = {
            "status": "diverged",
            "ahead_by": 1,
            "merge_base_commit": {"sha": "c" * 40},
        }
        with self.assertRaisesRegex(WorkflowError, "does not descend"):
            self.verifier.verify(
                repository_id="123456789",
                integration_branch="dev",
                pull_request_number=17,
                previous_accepted_head=previous,
            )


if __name__ == "__main__":
    unittest.main()
