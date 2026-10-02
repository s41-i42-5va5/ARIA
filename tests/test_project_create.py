from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aria.cli import main
from aria.errors import WorkflowError
from aria.github import GitHubRepository
from aria.github_repository import ProvisionedGitHubRepository
from aria.project_create import (
    create_collaborative_project,
    create_collaborative_project_plan,
)
from aria.provider import ProviderActor
from aria.provider import (
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
)
from tests.test_collaboration_apply import _LocalRemoteWriter


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


class _Provisioner:
    def __init__(self) -> None:
        self.create_calls = 0

    def actor(self) -> ProviderActor:
        return ProviderActor("100", "aram", "Aram")

    def exists(self, repository: GitHubRepository) -> bool:
        return False

    def create(
        self, *, repository: GitHubRepository, private: bool, description: str
    ) -> ProvisionedGitHubRepository:
        self.create_calls += 1
        return ProvisionedGitHubRepository(
            repository,
            "123456789",
            f"https://github.com/{repository.owner}/{repository.name}.git",
            private,
        )

    def ensure_integration_protection(self, **kwargs):
        return {"strict": True, "checks": [{"context": "ARIA integration", "app_id": 9001}]}

    def ensure_queue_labels(self, **kwargs):
        return {"aria:request": {}}


class _Adapter:
    provider_id = "github"

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return ProviderInspection(
            provider="github",
            repository_id=repository_id,
            actor=ProviderActor("100", "aram", "Aram"),
            membership=ProviderMembership(True, ("admin",)),
            protection=ProviderBranchProtection(True, False, "aria-coordinator"),
        )


class ProjectCreateTests(unittest.TestCase):
    def test_create_plan_is_read_only_and_reports_existing_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / "product"
            code.mkdir()
            provisioner = _Provisioner()
            plan = create_collaborative_project_plan(
                project_id="demo",
                owner="aram",
                repository_name="product",
                code_root=code,
                docs_root=None,
                private=True,
                client_id="Iv1.client123",
                coordinator_integration_id=9001,
                provisioner=provisioner,
            )
            self.assertFalse(plan["ok"])
            self.assertTrue(plan["read_only"])
            self.assertEqual(plan["blockers"][0]["code"], "CODE_ROOT_EXISTS")
            self.assertEqual(provisioner.create_calls, 0)

    def test_create_cli_passes_confirmation_and_private_default(self) -> None:
        with mock.patch(
            "aria.cli.create_collaborative_project",
            return_value={"ok": True, "collaboration": "enabled"},
        ) as create, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "project", "create", "--project", "demo", "--owner", "aram",
                    "--repository-name", "product", "--code-root", "C:/Projects/product",
                    "--github-client-id", "Iv1.client123",
                    "--coordinator-integration-id", "9001",
                    "--expected-plan-sha256", "a" * 64, "--confirm",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(create.call_args.kwargs["private"])
        self.assertTrue(create.call_args.kwargs["confirm"])

    def test_create_pushes_dev_and_recovers_before_collaboration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote.git"
            remote.mkdir()
            _git(remote, "init", "--bare", "-q")
            code = root / "product"
            docs = root / "product-aria-control"
            runtime = root / "runtime"
            askpass = root / "askpass.exe"
            askpass.write_text("unused", encoding="utf-8")
            provisioner = _Provisioner()
            arguments = {
                "project_id": "demo",
                "owner": "aram",
                "repository_name": "product",
                "code_root": code,
                "docs_root": docs,
                "private": True,
                "client_id": "Iv1.client123",
                "coordinator_integration_id": 9001,
                "provisioner": provisioner,
            }
            plan = create_collaborative_project_plan(**arguments)
            repository_url = "https://github.com/aram/product.git"
            environment = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{remote.resolve().as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": repository_url,
            }
            collaboration = {"plan_sha256": "b" * 64}
            with mock.patch.dict(os.environ, environment), mock.patch(
                "aria.project_create.build_authenticated_github_adapter",
                return_value=object(),
            ), mock.patch(
                "aria.project_create.build_github_control_writer", return_value=object()
            ), mock.patch(
                "aria.project_create.collaboration_plan", return_value=collaboration
            ), mock.patch(
                "aria.project_create.enable_collaboration",
                side_effect=WorkflowError("simulated stop"),
            ):
                with self.assertRaisesRegex(WorkflowError, "simulated stop"):
                    create_collaborative_project(
                        **arguments,
                        expected_plan_sha256=plan["plan_sha256"],
                        confirm=True,
                        runtime_root=runtime,
                        askpass_path=askpass,
                    )
            with mock.patch.dict(os.environ, environment), mock.patch(
                "aria.project_create.build_authenticated_github_adapter",
                return_value=object(),
            ), mock.patch(
                "aria.project_create.build_github_control_writer", return_value=object()
            ), mock.patch(
                "aria.project_create.collaboration_plan", return_value=collaboration
            ), mock.patch(
                "aria.project_create.enable_collaboration",
                return_value={"ok": True, "collaboration": "enabled", "recovered": False},
            ), mock.patch(
                "aria.project_create.load_project", return_value=object()
            ), mock.patch(
                "aria.project_create.run_collaborative_project_doctor",
                return_value={"ok": True, "checks": []},
            ):
                created = create_collaborative_project(
                    **arguments,
                    expected_plan_sha256=plan["plan_sha256"],
                    confirm=True,
                    runtime_root=runtime,
                    askpass_path=askpass,
                )
            self.assertTrue(created["ok"])
            self.assertTrue(created["recovered"])
            self.assertEqual(provisioner.create_calls, 1)
            self.assertEqual(_git(code, "branch", "--show-current"), "dev")
            self.assertEqual(
                _git(remote, "rev-parse", "refs/heads/main"),
                _git(remote, "rev-parse", "refs/heads/dev"),
            )
            self.assertEqual(_git(code, "rev-parse", "HEAD"), _git(remote, "rev-parse", "refs/heads/dev"))
            self.assertFalse((runtime / "collaboration-create" / "demo" / "transaction.json").exists())

    def test_create_end_to_end_builds_control_worktree_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote.git"
            remote.mkdir()
            _git(remote, "init", "--bare", "-q")
            code = root / "product"
            docs = root / "product-aria-control"
            runtime = root / "runtime"
            askpass = root / "askpass.exe"
            askpass.write_text("unused", encoding="utf-8")
            provisioner = _Provisioner()
            arguments = {
                "project_id": "demo",
                "owner": "aram",
                "repository_name": "product",
                "code_root": code,
                "docs_root": docs,
                "private": True,
                "client_id": "Iv1.client123",
                "coordinator_integration_id": 9001,
                "provisioner": provisioner,
            }
            plan = create_collaborative_project_plan(**arguments)
            repository_url = "https://github.com/aram/product.git"
            environment = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{remote.resolve().as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": repository_url,
            }
            writer = _LocalRemoteWriter(remote=remote, creator=root / "coordinator")
            with mock.patch.dict(os.environ, environment), mock.patch(
                "aria.project_create.build_authenticated_github_adapter",
                return_value=_Adapter(),
            ), mock.patch(
                "aria.project_create.build_github_control_writer", return_value=writer
            ):
                created = create_collaborative_project(
                    **arguments,
                    expected_plan_sha256=plan["plan_sha256"],
                    confirm=True,
                    runtime_root=runtime,
                    askpass_path=askpass,
                )
            self.assertTrue(created["ok"])
            self.assertEqual(created["collaboration"], "enabled")
            self.assertTrue(created["doctor_ok"])
            self.assertEqual(_git(code, "branch", "--show-current"), "dev")
            self.assertEqual(_git(docs, "branch", "--show-current"), "aria-control")
            self.assertTrue((docs / "CONTROL.yaml").is_file())
            self.assertTrue((runtime / "projects.toml").is_file())


if __name__ == "__main__":
    unittest.main()
