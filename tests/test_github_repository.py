from __future__ import annotations

import unittest
import urllib.parse

from aria.errors import ConfigurationError, WorkflowError
from aria.github import GitHubApiError, GitHubRepository
from aria.github_repository import GitHubRepositoryProvisioner


class _Transport:
    def __init__(self) -> None:
        self.created: tuple[str, object] | None = None
        self.protection: tuple[str, object] | None = None
        self.protection_patch: tuple[str, object] | None = None
        self.labels: dict[str, dict[str, object]] = {}

    def get_json(self, path: str):
        if path == "/user":
            return {"id": 100, "login": "aram", "name": "Aram"}
        if path.endswith("/protection") and self.protection:
            return self.protection[1]
        if path.endswith("/protection/required_status_checks") and self.protection:
            checks = self.protection[1].get("required_status_checks")
            if checks is None:
                raise GitHubApiError("not found", status=404)
            return checks
        if "/labels/" in path:
            name = urllib.parse.unquote(path.rsplit("/", 1)[1])
            if name in self.labels:
                return self.labels[name]
        raise GitHubApiError("not found", status=404)

    def post_json(self, path: str, payload: object):
        if path.endswith("/labels"):
            assert isinstance(payload, dict)
            row = dict(payload)
            self.labels[str(row["name"])] = row
            return row
        self.created = (path, payload)
        return {
            "id": 123456789,
            "full_name": "aram/product",
            "clone_url": "https://github.com/aram/product.git",
            "private": True,
        }

    def patch_json(self, path: str, payload: object):
        self.protection_patch = (path, payload)
        assert self.protection is not None
        self.protection = (
            self.protection[0],
            {**self.protection[1], "required_status_checks": payload},
        )
        return payload

    def put_json(self, path: str, payload: object):
        self.protection = (path, payload)
        return {}


class GitHubRepositoryProvisionerTests(unittest.TestCase):
    def test_queue_labels_are_created_and_read_back_idempotently(self) -> None:
        transport = _Transport()
        provisioner = GitHubRepositoryProvisioner(transport=transport)
        repository = GitHubRepository("aram", "product")
        first = provisioner.ensure_queue_labels(repository=repository)
        second = provisioner.ensure_queue_labels(repository=repository)
        self.assertEqual(first, second)
        self.assertEqual(
            set(transport.labels),
            {"aria:request", "aria:backlog", "aria:activity"},
        )

    def test_dev_protection_pins_required_check_to_coordinator_app(self) -> None:
        transport = _Transport()
        provisioner = GitHubRepositoryProvisioner(transport=transport)
        result = provisioner.ensure_integration_protection(
            repository=GitHubRepository("aram", "product"),
            branch="dev",
            coordinator_integration_id=9001,
        )
        self.assertTrue(result["strict"])
        self.assertEqual(
            transport.protection[1]["required_status_checks"]["checks"],
            [{"context": "ARIA integration", "app_id": 9001}],
        )

    def test_existing_dev_checks_are_preserved_when_aria_check_is_added(self) -> None:
        transport = _Transport()
        transport.protection = (
            "/repos/aram/product/branches/dev/protection",
            {
                "required_status_checks": {
                    "strict": False,
                    "checks": [{"context": "unit-tests", "app_id": 7001}],
                }
            },
        )
        provisioner = GitHubRepositoryProvisioner(transport=transport)
        result = provisioner.ensure_integration_protection(
            repository=GitHubRepository("aram", "product"),
            branch="dev",
            coordinator_integration_id=9001,
        )
        self.assertEqual(
            result["checks"],
            [
                {"context": "unit-tests", "app_id": 7001},
                {"context": "ARIA integration", "app_id": 9001},
            ],
        )
        self.assertIsNotNone(transport.protection_patch)

    def test_existing_protection_without_checks_is_not_overwritten(self) -> None:
        transport = _Transport()
        reviews = {
            "dismiss_stale_reviews": True,
            "required_approving_review_count": 2,
        }
        transport.protection = (
            "/repos/aram/product/branches/dev/protection",
            {
                "required_status_checks": None,
                "required_pull_request_reviews": reviews,
                "restrictions": {"users": [{"login": "release-manager"}]},
            },
        )
        provisioner = GitHubRepositoryProvisioner(transport=transport)
        provisioner.ensure_integration_protection(
            repository=GitHubRepository("aram", "product"),
            branch="dev",
            coordinator_integration_id=9001,
        )
        self.assertIsNotNone(transport.protection_patch)
        self.assertEqual(
            transport.protection[1]["required_pull_request_reviews"], reviews
        )
        self.assertEqual(
            transport.protection[1]["restrictions"],
            {"users": [{"login": "release-manager"}]},
        )
    def test_create_private_user_repository_exact_readback(self) -> None:
        transport = _Transport()
        provisioner = GitHubRepositoryProvisioner(transport=transport)
        repository = GitHubRepository("aram", "product")
        self.assertFalse(provisioner.exists(repository))
        created = provisioner.create(
            repository=repository,
            private=True,
            description="ARIA collaborative project demo",
        )
        self.assertEqual(created.repository_id, "123456789")
        self.assertEqual(transport.created[0], "/user/repos")
        self.assertTrue(transport.created[1]["has_issues"])
        self.assertFalse(transport.created[1]["auto_init"])

    def test_existing_repository_requires_exact_identity_readback(self) -> None:
        transport = _Transport()
        transport.get_json = lambda path: {
            "id": 123456789,
            "full_name": "attacker/product",
        }
        provisioner = GitHubRepositoryProvisioner(transport=transport)
        with self.assertRaisesRegex(ConfigurationError, "read-back is invalid"):
            provisioner.exists(GitHubRepository("aram", "product"))

    def test_read_existing_requires_admin_and_preserves_default_branch(self) -> None:
        transport = _Transport()
        transport.get_json = lambda path: {
            "id": 123456789,
            "full_name": "aram/product",
            "clone_url": "https://github.com/aram/product.git",
            "private": True,
            "default_branch": "legacy",
            "permissions": {"admin": True},
        }

        provisioner = GitHubRepositoryProvisioner(transport=transport)
        existing = provisioner.read_existing(GitHubRepository("aram", "product"))
        self.assertEqual(existing.repository_id, "123456789")
        self.assertEqual(existing.default_branch, "legacy")
        self.assertTrue(existing.private)

    def test_read_existing_fails_closed_without_admin_permission(self) -> None:
        transport = _Transport()
        transport.get_json = lambda path: {
            "id": 123456789,
            "full_name": "aram/product",
            "clone_url": "https://github.com/aram/product.git",
            "private": True,
            "default_branch": "main",
            "permissions": {"admin": False},
        }
        provisioner = GitHubRepositoryProvisioner(transport=transport)
        with self.assertRaisesRegex(WorkflowError, "admin permission"):
            provisioner.read_existing(GitHubRepository("aram", "product"))


if __name__ == "__main__":
    unittest.main()
