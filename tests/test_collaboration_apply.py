from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import aria.collaboration_apply as apply_module
from aria.collaboration import build_control_contract
from aria.collaboration_apply import (
    apply_collaboration_transaction,
    collaboration_apply_paths,
)
from aria.collaborative_documents import build_initial_collaborative_documents
from aria.errors import WorkflowError
from aria.github_control import GitHubControlCommit, PreparedGitHubControlCommit
from aria.project import load_project, run_project_doctor


SHA = "c" * 40


class _Writer:
    def __init__(self, *, head: str | None = None) -> None:
        self.head = head
        self.prepare_calls = 0
        self.install_calls = 0

    def read_head(self) -> str | None:
        return self.head

    def prepare_documents(self, *, documents, expected_head, message):
        self.prepare_calls += 1
        if self.head != expected_head:
            raise WorkflowError("stale writer")
        return PreparedGitHubControlCommit(
            repository_id="123456789",
            branch="aria-control",
            previous_head=expected_head,
            commit_sha=SHA,
            files=tuple(sorted(documents)),
        )

    def install_prepared(self, prepared):
        self.install_calls += 1
        if self.head not in {prepared.previous_head, prepared.commit_sha}:
            raise WorkflowError("stale install")
        self.head = prepared.commit_sha
        return GitHubControlCommit(
            repository_id=prepared.repository_id,
            branch=prepared.branch,
            previous_head=prepared.previous_head,
            commit_sha=prepared.commit_sha,
            created_branch=True,
            files=prepared.files,
        )


def _run(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


class _LocalRemoteWriter:
    def __init__(self, *, remote: Path, creator: Path) -> None:
        self.remote = remote
        self.creator = creator
        self.prepared: PreparedGitHubControlCommit | None = None

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
        _run(self.creator, "init", "-q")
        _run(self.creator, "config", "user.name", "ARIA Coordinator")
        _run(self.creator, "config", "user.email", "coordinator@example.invalid")
        _run(self.creator, "checkout", "--orphan", "aria-control")
        for name, content in documents.items():
            (self.creator / name).write_text(content, encoding="utf-8")
        _run(self.creator, "add", ".")
        _run(self.creator, "commit", "-m", message)
        sha = _run(self.creator, "rev-parse", "HEAD")
        self.prepared = PreparedGitHubControlCommit(
            repository_id="123456789",
            branch="aria-control",
            previous_head=expected_head,
            commit_sha=sha,
            files=tuple(sorted(documents)),
        )
        return self.prepared

    def install_prepared(self, prepared):
        if self.read_head() != prepared.commit_sha:
            if not _run(self.creator, "remote"):
                _run(self.creator, "remote", "add", "origin", str(self.remote))
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
            created_branch=True,
            files=prepared.files,
        )


class CollaborationApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.code = root / "code"
        self.code.mkdir()
        self.docs = root / "control"
        self.runtime = root / "runtime"
        self.contract = build_control_contract(
            project_id="demo",
            provider="github",
            repository_id="123456789",
        )
        self.documents = build_initial_collaborative_documents(
            self.contract, display_name="Demo"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(
        self,
        code_root: Path,
        *args: str,
        environment: dict[str, str] | None = None,
    ) -> str:
        if args[0] == "fetch":
            return ""
        if args[:2] == ("rev-parse", "refs/heads/aria-control"):
            return SHA
        if args[:2] == ("worktree", "add"):
            self.docs.mkdir()
            (self.docs / ".git").write_text("gitdir: test\n", encoding="utf-8")
            for name, content in self.documents.documents.items():
                (self.docs / name).write_text(content, encoding="utf-8")
            return ""
        raise AssertionError(args)

    def _apply(self, writer: _Writer):
        return apply_collaboration_transaction(
            project_id="demo",
            repository_id="123456789",
            plan_sha256="a" * 64,
            code_root=self.code,
            docs_root=self.docs,
            remote="origin",
            control_branch="aria-control",
            document_set=self.documents,
            writer=writer,
            runtime_root=self.runtime,
            git_environment={"GIT_ASKPASS": "aria-github-askpass"},
        )

    def test_remote_fetch_worktree_and_registration_converge(self) -> None:
        writer = _Writer()
        with mock.patch.object(apply_module, "_git", side_effect=self._git), mock.patch.object(
            apply_module,
            "register_project",
            return_value={"ok": True, "project": "demo", "mode": "shadow"},
        ) as register:
            result = self._apply(writer)
        self.assertEqual(result["collaboration"], "enabled")
        self.assertEqual(result["control_commit"], SHA)
        self.assertFalse(result["recovered"])
        self.assertEqual(writer.prepare_calls, 1)
        register.assert_called_once()
        paths = collaboration_apply_paths(
            runtime_root=self.runtime, project_id="demo"
        )
        self.assertFalse(paths.transaction.exists())

    def test_crash_after_remote_ref_update_recovers_idempotently(self) -> None:
        writer = _Writer()
        original_save = apply_module._save
        failed = False

        def fail_remote_phase(path: Path, journal: dict[str, object]) -> None:
            nonlocal failed
            if journal["phase"] == "remote_committed" and not failed:
                failed = True
                raise OSError("simulated crash after remote update")
            original_save(path, journal)

        with mock.patch.object(apply_module, "_save", side_effect=fail_remote_phase):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                self._apply(writer)
        self.assertEqual(writer.head, SHA)
        with mock.patch.object(apply_module, "_git", side_effect=self._git), mock.patch.object(
            apply_module,
            "register_project",
            return_value={"ok": True, "project": "demo", "mode": "shadow"},
        ):
            recovered = self._apply(writer)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(writer.install_calls, 2)

    def test_unmanaged_existing_branch_fails_before_journal(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "unmanaged"):
            self._apply(_Writer(head="d" * 40))
        paths = collaboration_apply_paths(
            runtime_root=self.runtime, project_id="demo"
        )
        self.assertFalse(paths.transaction.exists())

    def test_real_git_remote_worktree_and_registry_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote.git"
            code = root / "product"
            docs = root / "product-aria-control"
            creator = root / "creator"
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
            result = apply_collaboration_transaction(
                project_id="demo",
                repository_id="123456789",
                plan_sha256="a" * 64,
                code_root=code,
                docs_root=docs,
                remote="origin",
                control_branch="aria-control",
                document_set=documents,
                writer=_LocalRemoteWriter(remote=remote, creator=creator),
                runtime_root=runtime,
            )
            self.assertEqual(result["collaboration"], "enabled")
            self.assertTrue((docs / "CONTROL.yaml").is_file())
            self.assertEqual(_run(docs, "branch", "--show-current"), "aria-control")
            project = load_project("demo", runtime_root=runtime)
            self.assertEqual(project.collaboration_mode, "collaborative")
            self.assertEqual(project.docs_root.resolve(), docs.resolve())
            self.assertEqual(project.code_root.resolve(), code.resolve())
            doctor = run_project_doctor(project)
            self.assertTrue(doctor["ok"], doctor)


if __name__ == "__main__":
    unittest.main()
