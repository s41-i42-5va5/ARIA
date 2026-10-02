from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import aria.collaborative_migration as migration_module
from aria.access import bootstrap_access
from aria.backlog import add_backlog_item
from aria.collaborative_migration import (
    collaborative_migration_plan,
    collaborative_migration_status,
    migrate_offline_project,
    parse_actor_mappings,
    rollback_collaborative_migration,
    snapshot_tree,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.github_control import GitHubControlCommit, PreparedGitHubControlCommit
from aria.identity import enroll_identity
from aria.project import load_project, run_project_doctor
from aria.project_init import initialize_project
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
    ProviderTeamMember,
)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


class _Provider:
    provider_id = "github"

    def __init__(self) -> None:
        self.member = ProviderTeamMember(
            provider="github",
            actor=ProviderActor("100", "aram", "Aram"),
            membership=ProviderMembership(True, ("admin",)),
        )

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return ProviderInspection(
            provider="github",
            repository_id=repository_id,
            actor=self.member.actor,
            membership=self.member.membership,
            protection=ProviderBranchProtection(True, False, "aria-coordinator"),
        )

    def list_collaborators(self, *, repository_id: str):
        return (self.member,)


class _Writer:
    def __init__(self, *, remote: Path, creator: Path) -> None:
        self.remote = remote
        self.creator = creator

    def read_head(self) -> str | None:
        result = subprocess.run(
            [
                "git",
                f"--git-dir={self.remote}",
                "rev-parse",
                "--verify",
                "refs/heads/aria-control",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def prepare_documents(self, *, documents, expected_head, message):
        self.creator.mkdir()
        _git(self.creator, "init", "-q")
        _git(self.creator, "config", "user.name", "ARIA Coordinator")
        _git(self.creator, "config", "user.email", "coordinator@example.invalid")
        _git(self.creator, "checkout", "--orphan", "aria-control")
        for name, content in documents.items():
            (self.creator / name).write_text(content, encoding="utf-8")
        _git(self.creator, "add", ".")
        _git(self.creator, "commit", "-m", message)
        sha = _git(self.creator, "rev-parse", "HEAD")
        return PreparedGitHubControlCommit(
            repository_id="123456789",
            branch="aria-control",
            previous_head=expected_head,
            commit_sha=sha,
            files=tuple(sorted(documents)),
        )

    def install_prepared(self, prepared):
        if self.read_head() != prepared.commit_sha:
            _git(self.creator, "remote", "add", "origin", str(self.remote))
            _git(
                self.creator,
                "push",
                "origin",
                f"{prepared.commit_sha}:refs/heads/aria-control",
            )
        return GitHubControlCommit(
            repository_id=prepared.repository_id,
            branch=prepared.branch,
            previous_head=prepared.previous_head,
            commit_sha=prepared.commit_sha,
            created_branch=True,
            files=prepared.files,
        )


class _NoTaskScheduler:
    def inspect(self, *, task_name: str):
        return None


class CollaborativeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.framework = Path(__file__).resolve().parents[1]
        self.code = root / "product"
        self.docs = root / "offline-docs"
        self.control = root / "control-docs"
        self.runtime = root / "runtime"
        self.remote = root / "remote.git"
        self.creator = root / "coordinator"
        self.code.mkdir()
        self.remote.mkdir()
        _git(self.remote, "init", "--bare", "-q")
        _git(self.code, "init", "-q", "-b", "dev")
        _git(self.code, "config", "user.name", "ARIA Tests")
        _git(self.code, "config", "user.email", "tests@example.invalid")
        (self.code / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(self.code, "add", ".")
        _git(self.code, "commit", "-q", "-m", "initial")
        _git(self.code, "remote", "add", "origin", str(self.remote))
        _git(self.code, "push", "-u", "origin", "dev")
        initialize_project(
            "demo",
            code_root=self.code,
            docs_root=self.docs,
            display_name="Demo product",
            runtime_root=self.runtime,
        )
        self.project = load_project(
            "demo", framework_root=self.framework, runtime_root=self.runtime
        )
        identity_request = root / "owner.json"
        enroll_identity(
            self.runtime,
            actor_id="local-owner",
            device_id="owner-pc",
            request_path=identity_request,
        )
        bootstrap_access(
            self.project, actor_id="local-owner", device_id="owner-pc"
        )
        add_backlog_item(
            self.project,
            title="Сохранить идею",
            item_type="feature",
            priority="high",
            target_versions=["1.5.4"],
            acceptance="Идея перенесена без потери авторства",
            source_kind="user-request",
            source_ref="idea-1",
            requirements=["Не менять стабильный ID"],
            expected_revision=0,
            actor_id="local-owner",
            device_id="owner-pc",
        )
        self.provider = _Provider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _plan(self, mappings: dict[str, str]):
        return collaborative_migration_plan(
            project_id="demo",
            provider="github",
            repository_id="123456789",
            actor_mappings=mappings,
            coordinator_integration_id=900,
            provider_adapter=self.provider,
            framework_root=self.framework,
            runtime_root=self.runtime,
            docs_root=self.control,
        )

    def test_actor_mapping_and_source_snapshot_are_fail_closed(self) -> None:
        self.assertEqual(
            parse_actor_mappings(["local-owner=100"]), {"local-owner": "100"}
        )
        with self.assertRaisesRegex(ConfigurationError, "one-to-one"):
            parse_actor_mappings(["owner=100", "reviewer=100"])
        first = snapshot_tree(self.docs)
        second = snapshot_tree(self.docs)
        self.assertEqual(first, second)
        plan = self._plan({})
        self.assertFalse(plan["ready"])
        self.assertIn("ACTOR_MAPPING_MISSING", {row["code"] for row in plan["blockers"]})

    def test_plan_migrate_readback_and_safe_rollback_end_to_end(self) -> None:
        mappings = {"local-owner": "100"}
        plan = self._plan(mappings)
        self.assertTrue(plan["ready"], plan["blockers"])
        self.assertTrue(plan["read_only"])
        self.assertFalse(self.control.exists())
        writer = _Writer(remote=self.remote, creator=self.creator)
        result = migrate_offline_project(
            project_id="demo",
            provider="github",
            repository_id="123456789",
            actor_mappings=mappings,
            coordinator_integration_id=900,
            expected_plan_sha256=str(plan["plan_sha256"]),
            confirm=True,
            provider_adapter=self.provider,
            control_writer=writer,
            framework_root=self.framework,
            runtime_root=self.runtime,
            docs_root=self.control,
        )
        self.assertEqual(result["migration"], "completed")
        migrated = load_project(
            "demo", framework_root=self.framework, runtime_root=self.runtime
        )
        self.assertEqual(migrated.collaboration_mode, "collaborative")
        self.assertTrue(run_project_doctor(migrated)["ok"])
        backlog = yaml.safe_load((self.control / "BACKLOG.yaml").read_text(encoding="utf-8"))
        self.assertEqual(backlog["items"][0]["creator"]["user_id"], "100")
        self.assertTrue(backlog["items"][0]["id"].startswith("BLG-"))
        self.assertEqual(backlog["next_item_number"], 1)
        self.assertTrue(Path(result["backup_path"]).is_dir())
        status = collaborative_migration_status(
            project_id="demo", runtime_root=self.runtime
        )
        self.assertTrue(status["completed"])
        self.assertFalse(status["pending"])

        rolled_back = rollback_collaborative_migration(
            project_id="demo",
            expected_control_commit=result["control_commit"],
            confirm=True,
            control_writer=writer,
            runtime_root=self.runtime,
            scheduler=_NoTaskScheduler(),
        )
        self.assertEqual(rolled_back["migration"], "rolled_back")
        restored = load_project(
            "demo", framework_root=self.framework, runtime_root=self.runtime
        )
        self.assertEqual(restored.collaboration_mode, "offline")
        self.assertEqual(restored.docs_root.resolve(), self.docs.resolve())

        reactivated = migrate_offline_project(
            project_id="demo",
            provider="github",
            repository_id="123456789",
            actor_mappings=mappings,
            coordinator_integration_id=900,
            expected_plan_sha256=str(plan["plan_sha256"]),
            confirm=True,
            provider_adapter=self.provider,
            control_writer=writer,
            framework_root=self.framework,
            runtime_root=self.runtime,
            docs_root=self.control,
        )
        self.assertEqual(reactivated["migration"], "reactivated")
        self.assertEqual(
            load_project(
                "demo", framework_root=self.framework, runtime_root=self.runtime
            ).collaboration_mode,
            "collaborative",
        )

    def test_interrupted_apply_reuses_hash_bound_journal_and_backup(self) -> None:
        mappings = {"local-owner": "100"}
        plan = self._plan(mappings)
        writer = _Writer(remote=self.remote, creator=self.creator)
        arguments = {
            "project_id": "demo",
            "provider": "github",
            "repository_id": "123456789",
            "actor_mappings": mappings,
            "coordinator_integration_id": 900,
            "expected_plan_sha256": str(plan["plan_sha256"]),
            "confirm": True,
            "provider_adapter": self.provider,
            "control_writer": writer,
            "framework_root": self.framework,
            "runtime_root": self.runtime,
            "docs_root": self.control,
        }
        with mock.patch.object(
            migration_module,
            "apply_collaboration_transaction",
            side_effect=WorkflowError("simulated interruption"),
        ):
            with self.assertRaisesRegex(WorkflowError, "simulated interruption"):
                migrate_offline_project(**arguments)
        pending = collaborative_migration_status(
            project_id="demo", runtime_root=self.runtime
        )
        self.assertTrue(pending["pending"])
        self.assertFalse(pending["completed"])

        recovered = migrate_offline_project(**arguments)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["migration"], "completed")
        self.assertFalse(
            collaborative_migration_status(
                project_id="demo", runtime_root=self.runtime
            )["pending"]
        )


if __name__ == "__main__":
    unittest.main()
