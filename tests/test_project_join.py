from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import contextlib
import io
from pathlib import Path
from unittest import mock

from aria.collaboration import build_control_contract
from aria.collaborative_backlog import ProviderIdentity
from aria.collaborative_documents import build_initial_collaborative_documents
from aria.collaborative_team import (
    collaborative_team_template,
    dump_collaborative_team,
    sync_collaborative_team,
)
from aria.project_join import _journal, _prepare_clone_staging, join_collaborative_project
from aria.cli import main
from aria.errors import ConfigurationError
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
)
from tests.test_collaborative_team import TEAM


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


class _Adapter:
    provider_id = "github"

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return ProviderInspection(
            provider="github",
            repository_id=repository_id,
            actor=ProviderActor("200", "yura", "Yura"),
            membership=ProviderMembership(True, ("contributor",)),
            protection=ProviderBranchProtection(True, False, "aria-coordinator"),
        )


class _Provisioner:
    def actor(self) -> ProviderActor:
        return ProviderActor("200", "yura", "Yura")


class ProjectJoinTests(unittest.TestCase):
    def test_immediate_predecessor_join_journal_without_staging_is_accepted(self) -> None:
        identity = {
            "project_id": "demo",
            "repository_url_identity": "github:acme/product",
            "code_root": "C:\\Projects\\product",
            "docs_root": "C:\\Projects\\product-aria-control",
            "code_branch": "work/yura",
            "client_id": "Iv1.client123",
            "coordinator_integration_id": 9001,
        }
        legacy = {"schema_version": 1, **identity, "phase": "cloned"}
        self.assertIs(_journal(legacy, identity), legacy)

    def test_partial_join_clone_cleanup_is_limited_to_owned_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / "product"
            staging = root / ".product.aria-join-0123456789abcdef"
            checkout = _prepare_clone_staging(
                code=code,
                staging=staging,
                project_id="demo",
                repository_identity="github:acme/product",
            )
            checkout.mkdir()
            (checkout / "partial.txt").write_text("partial", encoding="utf-8")
            code.mkdir()
            (code / "user-important.txt").write_text("keep", encoding="utf-8")
            retried = _prepare_clone_staging(
                code=code,
                staging=staging,
                project_id="demo",
                repository_identity="github:acme/product",
            )
            self.assertFalse(retried.exists())
            self.assertEqual(
                (code / "user-important.txt").read_text(encoding="utf-8"), "keep"
            )

    def test_project_join_cli_passes_safe_public_inputs(self) -> None:
        with mock.patch(
            "aria.cli.join_collaborative_project",
            return_value={"ok": True, "collaboration": "joined"},
        ) as join, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "project",
                    "join",
                    "--project",
                    "demo",
                    "--repository-url",
                    "https://github.com/acme/product.git",
                    "--code-root",
                    "C:/Projects/product",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIsNone(join.call_args.kwargs["code_branch"])
        self.assertNotIn("token", join.call_args.kwargs)

    def test_explicit_branch_cannot_impersonate_another_github_user(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "parent").mkdir()
            with self.assertRaisesRegex(
                ConfigurationError,
                "exactly match work/<authenticated-github-username>",
            ):
                join_collaborative_project(
                    project_id="demo",
                    repository_url="https://github.com/acme/product.git",
                    code_root=root / "parent" / "product",
                    docs_root=root / "parent" / "product-aria-control",
                    code_branch="work/other-user",
                    client_id="Iv1.client123",
                    coordinator_integration_id=9001,
                    runtime_root=root / "runtime",
                    provisioner=_Provisioner(),
                )

    def test_real_clone_control_worktree_registration_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote.git"
            creator = root / "creator"
            remote.mkdir()
            _git(remote, "init", "--bare", "-q")
            creator.mkdir()
            _git(creator, "init", "-q")
            _git(creator, "config", "user.name", "ARIA Test")
            _git(creator, "config", "user.email", "aria@example.invalid")
            (creator / "README.md").write_text("product\n", encoding="utf-8")
            _git(creator, "add", "README.md")
            _git(creator, "commit", "-m", "initial code")
            _git(creator, "branch", "-M", "dev")
            _git(creator, "remote", "add", "origin", str(remote))
            _git(creator, "push", "origin", "dev")
            _git(remote, "symbolic-ref", "HEAD", "refs/heads/dev")
            _git(creator, "checkout", "--orphan", "aria-control")
            _git(creator, "rm", "-rf", ".")
            contract = build_control_contract(
                project_id="demo",
                provider="github",
                repository_id="123456789",
            )
            documents = build_initial_collaborative_documents(
                contract, display_name="product"
            ).documents
            coordinator = ProviderIdentity(
                "github-app",
                ProviderActor("9001", "aria-coordinator", "ARIA Coordinator"),
            )
            team = sync_collaborative_team(
                collaborative_team_template(
                    "demo", provider="github", repository_id="123456789"
                ),
                TEAM,
                sync_id="team-sync-join-0001",
                coordinator=coordinator,
                expected_revision=0,
                checked_at="2026-08-26T10:00:00Z",
            )["team"]
            documents["ARIA_TEAM.yaml"] = dump_collaborative_team(team)
            for name, content in documents.items():
                (creator / name).write_text(content, encoding="utf-8")
            _git(creator, "add", ".")
            _git(creator, "commit", "-m", "control plane")
            _git(creator, "push", "origin", "aria-control")

            code = root / "product"
            docs = root / "product-aria-control"
            runtime = root / "runtime"
            askpass = root / "askpass.exe"
            askpass.write_text("unused", encoding="utf-8")
            repository_url = "https://github.com/acme/product.git"
            file_url = remote.resolve().as_uri()
            environment = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{file_url}.insteadOf",
                "GIT_CONFIG_VALUE_0": repository_url,
            }
            original_register = __import__(
                "aria.project_join", fromlist=["register_project"]
            ).register_project
            failed = False

            def fail_once(*args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("simulated stop before registration")
                return original_register(*args, **kwargs)

            with mock.patch.dict(os.environ, environment), mock.patch(
                "aria.project_join.build_authenticated_github_adapter",
                return_value=_Adapter(),
            ), mock.patch(
                "aria.project_join.register_project", side_effect=fail_once
            ):
                with self.assertRaisesRegex(OSError, "simulated stop"):
                    join_collaborative_project(
                        project_id="demo",
                        repository_url=repository_url,
                        code_root=code,
                        docs_root=docs,
                        code_branch=None,
                        client_id="Iv1.client123",
                        coordinator_integration_id=9001,
                        runtime_root=runtime,
                        askpass_path=askpass,
                        provisioner=_Provisioner(),
                    )
            with mock.patch.dict(os.environ, environment), mock.patch(
                "aria.project_join.build_authenticated_github_adapter",
                return_value=_Adapter(),
            ):
                joined = join_collaborative_project(
                    project_id="demo",
                    repository_url=repository_url,
                    code_root=code,
                    docs_root=docs,
                    code_branch=None,
                    client_id="Iv1.client123",
                    coordinator_integration_id=9001,
                    runtime_root=runtime,
                    askpass_path=askpass,
                    provisioner=_Provisioner(),
                )
            self.assertTrue(joined["ok"])
            self.assertTrue(joined["recovered"])
            self.assertTrue(joined["doctor_ok"])
            self.assertEqual(_git(code, "branch", "--show-current"), "work/yura")
            self.assertEqual(
                _git(code, "rev-parse", "HEAD"),
                _git(remote, "rev-parse", "refs/heads/work/yura"),
            )
            self.assertEqual(_git(docs, "branch", "--show-current"), "aria-control")
            self.assertEqual(joined["actor"]["user_id"], "200")


if __name__ == "__main__":
    unittest.main()
