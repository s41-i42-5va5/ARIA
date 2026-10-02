from __future__ import annotations

import os
import json
import subprocess
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from aria.github import GitHubRepository
from aria.github_repository import ProvisionedGitHubRepository
from aria.project_connect import (
    _prepare_clone_staging,
    connect_existing_project,
    connect_existing_project_plan,
)
from aria.provider import ProviderActor


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return result.stdout.strip()


class _Provisioner:
    def __init__(self, clone_url: str) -> None:
        self.clone_url = clone_url

    def actor(self) -> ProviderActor:
        return ProviderActor("100", "aram", "Aram")

    def read_existing(self, repository: GitHubRepository) -> ProvisionedGitHubRepository:
        return ProvisionedGitHubRepository(
            repository, "123456789", self.clone_url, True, "legacy"
        )

    def read_branch_heads(self, repository: GitHubRepository) -> dict[str, str]:
        return {"legacy": "a" * 40}

    def ensure_integration_protection(self, **kwargs):
        return {"strict": True, "checks": [{"context": "ARIA integration", "app_id": 9001}]}

    def ensure_queue_labels(self, **kwargs):
        return {"aria:request": {}}


class ExistingProjectConnectTests(unittest.TestCase):
    def test_partial_clone_cleanup_is_limited_to_owned_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / "product"
            staging = root / ".product.aria-connect-0123456789abcdef"
            checkout = _prepare_clone_staging(
                code=code, staging=staging, plan_sha256="a" * 64
            )
            checkout.mkdir()
            (checkout / "partial.txt").write_text("partial", encoding="utf-8")
            code.mkdir()
            (code / "user-important.txt").write_text("keep", encoding="utf-8")
            retried = _prepare_clone_staging(
                code=code, staging=staging, plan_sha256="a" * 64
            )
            self.assertFalse(retried.exists())
            self.assertEqual(
                (code / "user-important.txt").read_text(encoding="utf-8"), "keep"
            )

    def test_plan_is_read_only_and_existing_code_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote.git"
            seed = root / "seed"
            remote.mkdir()
            seed.mkdir()
            _git(remote, "init", "--bare", "-q")
            _git(seed, "init", "-q")
            _git(seed, "config", "user.name", "ARIA Test")
            _git(seed, "config", "user.email", "aria@example.invalid")
            (seed / "product.txt").write_text("do-not-overwrite\n", encoding="utf-8")
            _git(seed, "add", "product.txt")
            _git(seed, "commit", "-m", "existing product")
            _git(seed, "branch", "-M", "legacy")
            _git(seed, "remote", "add", "origin", str(remote))
            _git(seed, "push", "origin", "legacy")
            _git(remote, "symbolic-ref", "HEAD", "refs/heads/legacy")
            repository_url = "https://github.com/acme/product.git"
            provisioner = _Provisioner(repository_url)
            code = root / "product"
            docs = root / "product-aria-control"
            plan = connect_existing_project_plan(
                project_id="demo", repository_url=repository_url,
                code_root=code, docs_root=docs,
                client_id="Iv1.client123", coordinator_integration_id=9001,
                provisioner=provisioner,
            )
            self.assertTrue(plan["ok"])
            self.assertFalse(code.exists())
            self.assertFalse(docs.exists())
            self.assertEqual(plan["observed_branch_heads"], {"legacy": "a" * 40})
            askpass = root / "askpass.exe"
            askpass.write_text("unused", encoding="utf-8")
            file_url = remote.resolve().as_uri()
            environment = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{file_url}.insteadOf",
                "GIT_CONFIG_VALUE_0": repository_url,
            }
            runtime = root / "runtime"
            transaction = runtime / "collaboration-connect" / "demo" / "transaction.json"
            transaction.parent.mkdir(parents=True)
            transaction.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "plan_sha256": plan["plan_sha256"],
                        "project_id": "demo",
                        "repository": {"owner": "acme", "name": "product"},
                        "code_root": str(code.absolute()),
                        "docs_root": str(docs.absolute()),
                        "client_id": "Iv1.client123",
                        "coordinator_integration_id": 9001,
                        "base_branch": "legacy",
                        "repository_receipt": {
                            **asdict(provisioner.read_existing(GitHubRepository("acme", "product"))),
                            "repository": {"owner": "acme", "name": "product"},
                        },
                        "phase": "prepared",
                    }
                ),
                encoding="utf-8",
            )
            enabled = {
                "ok": True,
                "collaboration": "enabled",
                "recovered": False,
            }
            with mock.patch.dict(os.environ, environment), mock.patch(
                "aria.project_connect.build_authenticated_github_adapter",
                return_value=object(),
            ), mock.patch(
                "aria.project_connect.build_github_control_writer",
                return_value=object(),
            ), mock.patch(
                "aria.project_connect.collaboration_plan",
                return_value={"plan_sha256": "b" * 64},
            ), mock.patch(
                "aria.project_connect.enable_collaboration", return_value=enabled,
            ), mock.patch(
                "aria.project_connect.load_project", return_value=object(),
            ), mock.patch(
                "aria.project_connect.run_collaborative_project_doctor",
                return_value={"ok": True},
            ):
                result = connect_existing_project(
                    project_id="demo", repository_url=repository_url,
                    code_root=code, docs_root=docs,
                    client_id="Iv1.client123", coordinator_integration_id=9001,
                    expected_plan_sha256=str(plan["plan_sha256"]), confirm=True,
                    runtime_root=runtime, askpass_path=askpass,
                    provisioner=provisioner,
                )
            original = _git(seed, "rev-parse", "legacy")
            self.assertEqual(_git(remote, "rev-parse", "refs/heads/main"), original)
            self.assertEqual(_git(remote, "rev-parse", "refs/heads/dev"), original)
            self.assertEqual(_git(code, "show", "HEAD:product.txt"), "do-not-overwrite")
            self.assertTrue(result["preserved_existing_code"])
            self.assertTrue(result["doctor_ok"])
            self.assertTrue(result["recovered"])


if __name__ == "__main__":
    unittest.main()
