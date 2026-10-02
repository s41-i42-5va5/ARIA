from __future__ import annotations

import unittest

from aria.errors import ConfigurationError, WorkflowError
from aria.github import GitHubApiError, GitHubRepository
from aria.github_control import (
    GitHubControlPlaneWriter,
    PreparedGitHubControlCommit,
)


class _Transport:
    def __init__(self, *, head: str | None = None, repository_id: int = 123456789) -> None:
        self.head = head
        self.repository_id = repository_id
        self.calls: list[tuple[str, str, object | None]] = []
        self.next_tree = "b" * 40
        self.next_commit = "c" * 40

    def get_json(self, path: str):
        self.calls.append(("GET", path, None))
        if path == "/repos/acme/product":
            return {"id": self.repository_id, "full_name": "acme/product"}
        if path == "/repos/acme/product/git/ref/heads/aria-control":
            if self.head is None:
                raise GitHubApiError("missing", status=404)
            return {"ref": "refs/heads/aria-control", "object": {"sha": self.head}}
        raise AssertionError(path)

    def post_json(self, path: str, payload: object):
        self.calls.append(("POST", path, payload))
        if path.endswith("/git/trees"):
            return {"sha": self.next_tree}
        if path.endswith("/git/commits"):
            return {"sha": self.next_commit}
        if path.endswith("/git/refs"):
            self.head = self.next_commit
            return {"ref": "refs/heads/aria-control", "object": {"sha": self.head}}
        raise AssertionError(path)

    def patch_json(self, path: str, payload: object):
        self.calls.append(("PATCH", path, payload))
        self.head = self.next_commit
        return {"ref": "refs/heads/aria-control", "object": {"sha": self.head}}


def _writer(transport: _Transport) -> GitHubControlPlaneWriter:
    return GitHubControlPlaneWriter(
        repository=GitHubRepository("acme", "product"),
        repository_id="123456789",
        control_branch="aria-control",
        transport=transport,
    )


class GitHubControlWriterTests(unittest.TestCase):
    def test_creates_orphan_control_branch_and_reads_back_head(self) -> None:
        transport = _Transport()
        result = _writer(transport).commit_documents(
            documents={"CONTROL.yaml": "schema_version: 1\n"},
            expected_head=None,
            message="ARIA: initialize control plane",
        )
        self.assertTrue(result.created_branch)
        self.assertEqual(result.commit_sha, "c" * 40)
        tree_payload = next(
            payload
            for method, path, payload in transport.calls
            if method == "POST" and path.endswith("/git/trees")
        )
        self.assertEqual(tree_payload["tree"][0]["path"], "CONTROL.yaml")
        commit_payload = next(
            payload
            for method, path, payload in transport.calls
            if method == "POST" and path.endswith("/git/commits")
        )
        self.assertEqual(commit_payload["parents"], [])

    def test_updates_exact_head_without_force(self) -> None:
        original = "a" * 40
        transport = _Transport(head=original)
        result = _writer(transport).commit_documents(
            documents={"ARIA_TEAM.yaml": "schema_version: 2\n"},
            expected_head=original,
            message="ARIA: sync team",
        )
        self.assertFalse(result.created_branch)
        patch_payload = next(
            payload for method, _, payload in transport.calls if method == "PATCH"
        )
        self.assertEqual(patch_payload, {"sha": "c" * 40, "force": False})

    def test_install_prepared_is_idempotent_after_ref_update(self) -> None:
        transport = _Transport(head="c" * 40)
        prepared = PreparedGitHubControlCommit(
            repository_id="123456789",
            branch="aria-control",
            previous_head=None,
            commit_sha="c" * 40,
            files=("CONTROL.yaml",),
        )
        result = _writer(transport).install_prepared(prepared)
        self.assertEqual(result.commit_sha, "c" * 40)
        self.assertFalse(any(method != "GET" for method, _, _ in transport.calls))

    def test_stale_head_and_repository_mismatch_fail_before_mutation(self) -> None:
        transport = _Transport(head="a" * 40)
        with self.assertRaisesRegex(WorkflowError, "Stale"):
            _writer(transport).commit_documents(
                documents={"CONTROL.yaml": "schema_version: 1\n"},
                expected_head="d" * 40,
                message="ARIA: stale",
            )
        self.assertFalse(any(method != "GET" for method, _, _ in transport.calls))
        mismatch = _Transport(repository_id=987654321)
        with self.assertRaisesRegex(WorkflowError, "repository id"):
            _writer(mismatch).commit_documents(
                documents={"CONTROL.yaml": "schema_version: 1\n"},
                expected_head=None,
                message="ARIA: mismatch",
            )

    def test_rejects_non_control_paths_and_oversized_messages(self) -> None:
        writer = _writer(_Transport())
        with self.assertRaisesRegex(ConfigurationError, "non-control"):
            writer.commit_documents(
                documents={"src/backdoor.py": "pass\n"},
                expected_head=None,
                message="ARIA: invalid",
            )
        with self.assertRaisesRegex(ConfigurationError, "message"):
            writer.commit_documents(
                documents={"CONTROL.yaml": "schema_version: 1\n"},
                expected_head=None,
                message="line one\nline two",
            )


if __name__ == "__main__":
    unittest.main()
