from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import aria.control_worktree_sync as sync_module
from aria.collaboration import build_control_contract
from aria.collaboration_apply import apply_collaboration_transaction
from aria.collaborative_documents import build_initial_collaborative_documents
from aria.control_worktree_sync import (
    prepare_control_mutation,
    recover_pending_control_sync,
    refresh_control_worktree,
    synchronize_control_worktree,
)
from aria.github_control import GitHubControlCommit, PreparedGitHubControlCommit
from aria.project import load_project
from tests.test_collaboration_apply import _LocalRemoteWriter, _run


class _LocalUpdateWriter:
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
        _run(self.creator.parent, "clone", "-q", str(self.remote), str(self.creator))
        _run(self.creator, "config", "user.name", "ARIA Coordinator")
        _run(self.creator, "config", "user.email", "coordinator@example.invalid")
        _run(self.creator, "checkout", "-q", "aria-control")
        if _run(self.creator, "rev-parse", "HEAD") != expected_head:
            raise AssertionError("update writer head mismatch")
        for name, content in documents.items():
            (self.creator / name).write_text(content, encoding="utf-8")
        _run(self.creator, "add", ".")
        _run(self.creator, "commit", "-m", message)
        commit = _run(self.creator, "rev-parse", "HEAD")
        return PreparedGitHubControlCommit(
            repository_id="123456789",
            branch="aria-control",
            previous_head=expected_head,
            commit_sha=commit,
            files=tuple(sorted(documents)),
        )

    def install_prepared(self, prepared):
        if self.read_head() != prepared.commit_sha:
            _run(
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
            created_branch=False,
            files=prepared.files,
        )


class ControlWorktreeSyncTests(unittest.TestCase):
    def test_real_remote_update_and_local_reset_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote.git"
            code = root / "product"
            docs = root / "product-aria-control"
            runtime = root / "runtime"
            remote.mkdir()
            _run(remote, "init", "--bare", "-q")
            code.mkdir()
            _run(code, "init", "-q")
            _run(code, "config", "user.name", "ARIA Test")
            _run(code, "config", "user.email", "aria@example.invalid")
            (code / "README.md").write_text("product\n", encoding="utf-8")
            _run(code, "add", "README.md")
            _run(code, "commit", "-m", "initial")
            _run(code, "branch", "-M", "dev")
            _run(code, "remote", "add", "origin", str(remote))
            _run(code, "push", "-u", "origin", "dev")
            contract = build_control_contract(
                project_id="demo",
                provider="github",
                repository_id="123456789",
            )
            documents = build_initial_collaborative_documents(
                contract, display_name="product"
            )
            apply_collaboration_transaction(
                project_id="demo",
                repository_id="123456789",
                plan_sha256="a" * 64,
                code_root=code,
                docs_root=docs,
                remote="origin",
                control_branch="aria-control",
                document_set=documents,
                writer=_LocalRemoteWriter(
                    remote=remote, creator=root / "initial-creator"
                ),
                runtime_root=runtime,
            )
            project = load_project("demo", runtime_root=runtime)
            activity = docs / "ACTIVITY.yaml"
            activity.write_text(
                activity.read_text(encoding="utf-8") + "# coordinator update\n",
                encoding="utf-8",
            )
            writer = _LocalUpdateWriter(remote=remote, creator=root / "update-creator")
            original_write = sync_module.atomic_write_bytes
            failed = False

            def fail_after_remote(path: Path, content: bytes) -> None:
                nonlocal failed
                payload = json.loads(content.decode("utf-8"))
                if payload.get("phase") == "remote_committed" and not failed:
                    failed = True
                    raise OSError("simulated crash after remote commit")
                original_write(path, content)

            with mock.patch.object(
                sync_module, "atomic_write_bytes", side_effect=fail_after_remote
            ):
                with self.assertRaisesRegex(OSError, "simulated crash"):
                    synchronize_control_worktree(
                        project, writer=writer, operation_id="activity-event-0001"
                    )
            result = synchronize_control_worktree(
                project, writer=writer, operation_id="activity-event-0001"
            )
            self.assertTrue(result["applied"])
            self.assertTrue(result["recovered"])
            self.assertEqual(_run(docs, "status", "--porcelain=v1"), "")
            self.assertEqual(_run(docs, "rev-parse", "HEAD"), writer.read_head())
            duplicate = synchronize_control_worktree(
                project, writer=writer, operation_id="activity-event-0001"
            )
            self.assertFalse(duplicate["applied"])
            self.assertEqual(duplicate["reason"], "already_synchronized")

            prepare_control_mutation(project, operation_id="activity-event-0002")
            activity.write_text(
                activity.read_text(encoding="utf-8") + "# crash before sync call\n",
                encoding="utf-8",
            )
            writer = _LocalUpdateWriter(
                remote=remote, creator=root / "update-creator-recovery"
            )
            recovered_before_refresh = recover_pending_control_sync(
                project, writer=writer
            )
            self.assertIsNotNone(recovered_before_refresh)
            self.assertTrue(recovered_before_refresh["recovered"])
            self.assertEqual(_run(docs, "status", "--porcelain=v1"), "")

            target_documents = {
                name: (docs / name).read_text(encoding="utf-8")
                for name in documents.documents
            }
            target_documents["ACCESS.yaml"] += "# target access upgrade\n"
            target_documents["PROJECT.yaml"] += "# target project upgrade\n"
            prepare_control_mutation(
                project,
                operation_id="backlog-recover-0003",
                target_documents=target_documents,
            )
            (docs / "ACCESS.yaml").write_text(
                target_documents["ACCESS.yaml"], encoding="utf-8"
            )
            writer = _LocalUpdateWriter(
                remote=remote, creator=root / "update-creator-target-recovery"
            )
            recovered_target = recover_pending_control_sync(project, writer=writer)
            self.assertTrue(recovered_target["recovered"])
            self.assertEqual(
                (docs / "PROJECT.yaml").read_text(encoding="utf-8"),
                target_documents["PROJECT.yaml"],
            )
            self.assertEqual(_run(docs, "status", "--porcelain=v1"), "")

            external = root / "external-coordinator"
            _run(root, "clone", "-q", str(remote), str(external))
            _run(external, "config", "user.name", "ARIA Coordinator")
            _run(external, "config", "user.email", "coordinator@example.invalid")
            _run(external, "checkout", "-q", "aria-control")
            activity = external / "ACTIVITY.yaml"
            activity.write_text(
                activity.read_text(encoding="utf-8") + "# remote refresh\n",
                encoding="utf-8",
            )
            _run(external, "add", "ACTIVITY.yaml")
            _run(external, "commit", "-m", "remote coordinator update")
            _run(external, "push", "origin", "aria-control")
            refreshed = refresh_control_worktree(project, writer=writer)
            self.assertTrue(refreshed["updated"])
            self.assertEqual(_run(docs, "rev-parse", "HEAD"), writer.read_head())


if __name__ == "__main__":
    unittest.main()
