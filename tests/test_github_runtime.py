from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aria.cli import main
from aria.github_runtime import (
    build_authenticated_github_adapter,
    build_github_control_writer,
)
from aria.github_session import GitHubSessionError


class _Backend:
    def read(self, target: str) -> bytes | None:
        return None

    def write(self, target: str, secret: bytes) -> None:
        raise AssertionError("runtime builder must not write credentials")

    def delete(self, target: str) -> None:
        return None


class GitHubRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.git_environment = {"GIT_ASKPASS": "aria-github-askpass"}
        patcher = mock.patch(
            "aria.cli.build_github_git_environment",
            return_value=self.git_environment,
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_adapter_uses_session_vault_before_any_network_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/acme/product.git",
                ],
                cwd=root,
                check=True,
            )
            adapter = build_authenticated_github_adapter(
                code_root=root,
                remote="origin",
                client_id="Iv1.client123",
                coordinator_integration_id=9001,
                credential_backend=_Backend(),
            )
            with self.assertRaisesRegex(GitHubSessionError, "login is required"):
                adapter.inspect_collaboration(
                    repository_id="123456789",
                    control_branch="aria-control",
                )

    def test_control_writer_requires_configured_app_key_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/acme/product.git",
                ],
                cwd=root,
                check=True,
            )
            writer = build_github_control_writer(
                code_root=root,
                remote="origin",
                repository_id="123456789",
                app_id=9001,
                credential_backend=_Backend(),
            )
            with self.assertRaisesRegex(Exception, "not configured"):
                writer.read_head()

    def test_collaboration_cli_passes_authenticated_adapter_to_plan(self) -> None:
        adapter = object()
        with mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ) as builder, mock.patch(
            "aria.cli.collaboration_plan",
            return_value={"ok": True, "plan_sha256": "a" * 64},
        ) as plan, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "plan",
                    "--project",
                    "demo",
                    "--code-root",
                    "C:/project",
                    "--provider",
                    "github",
                    "--repository-id",
                    "123456789",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                ]
            )
        self.assertEqual(code, 0)
        builder.assert_called_once()
        self.assertIs(plan.call_args.kwargs["provider_adapter"], adapter)

    def test_collaboration_migration_cli_uses_registered_project_and_actor_map(self) -> None:
        adapter = object()
        project = SimpleNamespace(code_root=Path("C:/project"))
        with mock.patch(
            "aria.cli.load_project", return_value=project
        ), mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ), mock.patch(
            "aria.cli.collaborative_migration_plan",
            return_value={"ok": True, "ready": True, "plan_sha256": "b" * 64},
        ) as plan, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "migrate-plan",
                    "--project",
                    "demo",
                    "--repository-id",
                    "123456789",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--actor-map",
                    "local-owner=100",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(plan.call_args.kwargs["provider_adapter"], adapter)
        self.assertEqual(
            plan.call_args.kwargs["actor_mappings"], {"local-owner": "100"}
        )

    def test_collaboration_enable_uses_user_and_app_sessions_separately(self) -> None:
        user_adapter = object()
        control_writer = object()
        with mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=user_adapter
        ), mock.patch(
            "aria.cli.build_github_control_writer", return_value=control_writer
        ) as writer_builder, mock.patch(
            "aria.cli.enable_collaboration",
            return_value={"ok": True, "collaboration": "enabled"},
        ) as enable, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "enable",
                    "--project",
                    "demo",
                    "--code-root",
                    "C:/project",
                    "--provider",
                    "github",
                    "--repository-id",
                    "123456789",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--expected-plan-sha256",
                    "a" * 64,
                    "--confirm",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(enable.call_args.kwargs["provider_adapter"], user_adapter)
        self.assertIs(enable.call_args.kwargs["control_writer"], control_writer)
        self.assertIs(
            enable.call_args.kwargs["git_environment"], self.git_environment
        )
        self.assertEqual(writer_builder.call_args.kwargs["app_id"], 9001)

    def test_team_sync_cli_uses_registered_project_and_saved_session(self) -> None:
        project = SimpleNamespace(
            code_root=Path("C:/project"),
            docs_root=Path("C:/control"),
        )
        control = SimpleNamespace(
            remote="origin",
            repository_id="123456789",
            control_branch="aria-control",
        )
        adapter = object()
        control_writer = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.load_control_contract", return_value=control
        ), mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ) as builder, mock.patch(
            "aria.cli.build_github_control_writer", return_value=control_writer
        ), mock.patch(
            "aria.cli.sync_authenticated_team",
            return_value={"ok": True, "revision": 1},
        ) as sync, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "team-sync",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--expected-revision",
                    "0",
                    "--sync-id",
                    "team-sync-cli-0001",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(builder.call_args.kwargs["code_root"], project.code_root)
        self.assertIs(sync.call_args.kwargs["adapter"], adapter)
        self.assertIs(sync.call_args.kwargs["control_writer"], control_writer)
        self.assertIs(sync.call_args.kwargs["git_environment"], self.git_environment)
        self.assertEqual(sync.call_args.kwargs["expected_revision"], 0)

    def test_team_status_cli_is_read_only_and_needs_no_github_session(self) -> None:
        project = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.collaborative_team_status",
            return_value={"ok": True, "revision": 2},
        ) as status, mock.patch(
            "aria.cli.build_authenticated_github_adapter"
        ) as builder, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                ["collaboration", "team-status", "--project", "demo"]
            )
        self.assertEqual(code, 0)
        status.assert_called_once_with(project)
        builder.assert_not_called()

    def test_state_sync_cli_builds_separate_user_and_app_boundaries(self) -> None:
        project = SimpleNamespace(
            code_root=Path("C:/project"), docs_root=Path("C:/control")
        )
        control = SimpleNamespace(
            remote="origin",
            repository_id="123456789",
            control_branch="aria-control",
        )
        adapter = object()
        writer = object()
        verifier = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.load_control_contract", return_value=control
        ), mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ), mock.patch(
            "aria.cli.build_github_control_writer", return_value=writer
        ), mock.patch(
            "aria.cli.build_github_app_integration_verifier", return_value=verifier
        ), mock.patch(
            "aria.cli.sync_accepted_pull_request",
            return_value={"ok": True, "state_revision": 1},
        ) as sync, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "state-sync",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--item",
                    "BLG-000001",
                    "--pull-request",
                    "17",
                    "--expected-backlog-revision",
                    "2",
                    "--expected-state-revision",
                    "0",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(sync.call_args.kwargs["coordinator_adapter"], adapter)
        self.assertIs(sync.call_args.kwargs["control_writer"], writer)
        self.assertIs(sync.call_args.kwargs["verifier"], verifier)
        self.assertIs(sync.call_args.kwargs["git_environment"], self.git_environment)
        self.assertEqual(sync.call_args.kwargs["pull_request_number"], 17)

    def test_state_status_cli_is_local_read_only(self) -> None:
        project = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.collaborative_state_status",
            return_value={"ok": True, "revision": 1},
        ) as status, mock.patch(
            "aria.cli.build_github_app_integration_verifier"
        ) as verifier, contextlib.redirect_stdout(io.StringIO()):
            code = main(["collaboration", "state-status", "--project", "demo"])
        self.assertEqual(code, 0)
        status.assert_called_once_with(project)
        verifier.assert_not_called()

    def test_collaborative_backlog_add_uses_user_and_app_sessions(self) -> None:
        project = SimpleNamespace(
            code_root=Path("C:/project"), docs_root=Path("C:/control")
        )
        control = SimpleNamespace(
            remote="origin",
            repository_id="123456789",
            control_branch="aria-control",
        )
        adapter = object()
        writer = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.load_control_contract", return_value=control
        ), mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ), mock.patch(
            "aria.cli.build_github_control_writer", return_value=writer
        ), mock.patch(
            "aria.cli.submit_authenticated_backlog_action",
            return_value={"ok": True, "revision": 1},
        ) as submit, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "backlog-add",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--expected-revision",
                    "0",
                    "--delivery",
                    "coordinator-local",
                    "--title",
                    "Новая идея",
                    "--description",
                    "Без исполнителя",
                    "--priority",
                    "P1",
                    "--dependency",
                    "BLG-000002",
                    "--dependency",
                    "BLG-000001",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(submit.call_args.kwargs["adapter"], adapter)
        self.assertIs(submit.call_args.kwargs["control_writer"], writer)
        self.assertIs(
            submit.call_args.kwargs["git_environment"], self.git_environment
        )
        self.assertEqual(submit.call_args.kwargs["action"], "add")
        self.assertIsNone(submit.call_args.kwargs["item_id"])
        self.assertEqual(
            submit.call_args.kwargs["payload"]["dependencies"],
            ["BLG-000001", "BLG-000002"],
        )

    def test_collaborative_backlog_list_is_local_read_only(self) -> None:
        project = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.collaborative_backlog_status",
            return_value={"ok": True, "revision": 2, "items": []},
        ) as status, mock.patch(
            "aria.cli.build_authenticated_github_adapter"
        ) as adapter, mock.patch(
            "aria.cli.build_github_control_writer"
        ) as writer, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "backlog-list",
                    "--project",
                    "demo",
                    "--include-done",
                ]
            )
        self.assertEqual(code, 0)
        status.assert_called_once_with(project, include_done=True)
        adapter.assert_not_called()
        writer.assert_not_called()

    def test_collaborative_activity_set_uses_user_and_app_sessions(self) -> None:
        project = SimpleNamespace(
            code_root=Path("C:/project"), docs_root=Path("C:/control")
        )
        control = SimpleNamespace(
            remote="origin",
            repository_id="123456789",
            control_branch="aria-control",
        )
        adapter = object()
        writer = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.load_control_contract", return_value=control
        ), mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ), mock.patch(
            "aria.cli.build_github_control_writer", return_value=writer
        ), mock.patch(
            "aria.cli.submit_authenticated_activity",
            return_value={"ok": True, "revision": 1},
        ) as submit, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "activity-set",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--expected-revision",
                    "0",
                    "--delivery",
                    "coordinator-local",
                    "--task",
                    "BLG-000001",
                    "--stage",
                    "implementation",
                    "--branch",
                    "work/yura",
                    "--note",
                    "Пишу код",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(submit.call_args.kwargs["adapter"], adapter)
        self.assertIs(submit.call_args.kwargs["control_writer"], writer)
        self.assertIs(
            submit.call_args.kwargs["git_environment"], self.git_environment
        )
        self.assertEqual(submit.call_args.kwargs["task_id"], "BLG-000001")
        self.assertEqual(submit.call_args.kwargs["stage"], "implementation")

    def test_backlog_add_defaults_to_user_github_queue_without_app_key(self) -> None:
        project = SimpleNamespace(
            code_root=Path("C:/project"), docs_root=Path("C:/control")
        )
        control = SimpleNamespace(remote="origin")
        adapter = object()
        queue = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.load_control_contract", return_value=control
        ), mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ), mock.patch(
            "aria.cli.build_authenticated_github_request_queue", return_value=queue
        ) as queue_builder, mock.patch(
            "aria.cli.build_github_control_writer"
        ) as writer_builder, mock.patch(
            "aria.cli.enqueue_backlog_action",
            return_value={"ok": True, "issue_number": 7},
        ) as enqueue, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "backlog-add",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--expected-revision",
                    "0",
                    "--title",
                    "Идея",
                    "--description",
                    "Без ключа App",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(enqueue.call_args.kwargs["queue"], queue)
        queue_builder.assert_called_once()
        writer_builder.assert_not_called()

    def test_queue_process_builds_app_queue_and_control_writer(self) -> None:
        project = SimpleNamespace(
            code_root=Path("C:/project"), docs_root=Path("C:/control")
        )
        control = SimpleNamespace(
            remote="origin",
            repository_id="123456789",
            control_branch="aria-control",
        )
        adapter = object()
        queue = object()
        writer = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.load_control_contract", return_value=control
        ), mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ), mock.patch(
            "aria.cli.build_github_app_request_queue", return_value=queue
        ), mock.patch(
            "aria.cli.build_github_control_writer", return_value=writer
        ), mock.patch(
            "aria.cli.process_github_request_queue",
            return_value={"ok": True, "processed": []},
        ) as process, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "queue-process",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--max-requests",
                    "5",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(process.call_args.kwargs["queue"], queue)
        self.assertIs(process.call_args.kwargs["control_writer"], writer)
        self.assertIs(
            process.call_args.kwargs["git_environment"], self.git_environment
        )
        self.assertEqual(process.call_args.kwargs["max_requests"], 5)

    def test_coordinator_run_once_builds_queue_writer_and_verifier(self) -> None:
        project = SimpleNamespace(
            code_root=Path("C:/project"), docs_root=Path("C:/control")
        )
        control = SimpleNamespace(
            remote="origin", repository_id="123456789", control_branch="aria-control"
        )
        adapter = object()
        queue = object()
        writer = object()
        verifier = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.load_control_contract", return_value=control
        ), mock.patch(
            "aria.cli.build_authenticated_github_adapter", return_value=adapter
        ), mock.patch(
            "aria.cli.build_github_app_request_queue", return_value=queue
        ), mock.patch(
            "aria.cli.build_github_control_writer", return_value=writer
        ), mock.patch(
            "aria.cli.build_github_app_integration_verifier", return_value=verifier
        ), mock.patch(
            "aria.cli.run_collaborative_coordinator_once",
            return_value={"ok": True, "pull_requests": {"processed": []}},
        ) as run_once, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "collaboration",
                    "coordinator-run-once",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--max-requests",
                    "5",
                    "--max-pull-requests",
                    "7",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(run_once.call_args.kwargs["request_queue"], queue)
        self.assertIs(run_once.call_args.kwargs["control_writer"], writer)
        self.assertIs(run_once.call_args.kwargs["integration_verifier"], verifier)
        self.assertIs(
            run_once.call_args.kwargs["git_environment"], self.git_environment
        )
        self.assertEqual(run_once.call_args.kwargs["max_pull_requests"], 7)

    def test_collaborative_activity_list_is_local_read_only(self) -> None:
        project = object()
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.collaborative_activity_status",
            return_value={"ok": True, "revision": 2, "active_work": []},
        ) as status, mock.patch(
            "aria.cli.build_github_control_writer"
        ) as writer, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                ["collaboration", "activity-list", "--project", "demo"]
            )
        self.assertEqual(code, 0)
        status.assert_called_once_with(project)
        writer.assert_not_called()

    def test_coordinator_install_requires_credentials_and_dispatches_schedule(self) -> None:
        project = SimpleNamespace(runtime_root=Path("C:/runtime"))
        login_service = mock.Mock()
        login_service.status.return_value = {"ok": True, "logged_in": True}
        executable = Path("C:/install/aria.exe")
        with mock.patch("aria.cli.load_project", return_value=project), mock.patch(
            "aria.cli.default_github_login_service", return_value=login_service
        ), mock.patch(
            "aria.cli.github_app_key_status",
            return_value={"ok": True, "configured": True},
        ), mock.patch(
            "aria.cli.resolve_aria_executable", return_value=executable
        ), mock.patch(
            "aria.cli.install_coordinator_schedule",
            return_value={"ok": True, "installed": True},
        ) as install, contextlib.redirect_stdout(io.StringIO()):
            code = main(
                [
                    "coordinator",
                    "install",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--runtime-root",
                    "C:/runtime",
                    "--interval-minutes",
                    "2",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIs(install.call_args.args[0], project)
        self.assertEqual(install.call_args.kwargs["interval_minutes"], 2)
        self.assertEqual(install.call_args.kwargs["aria_executable"], executable)

    def test_coordinator_install_rejects_missing_app_key(self) -> None:
        login_service = mock.Mock()
        login_service.status.return_value = {"ok": True, "logged_in": True}
        output = io.StringIO()
        with mock.patch("aria.cli.load_project", return_value=object()), mock.patch(
            "aria.cli.default_github_login_service", return_value=login_service
        ), mock.patch(
            "aria.cli.github_app_key_status",
            return_value={"ok": True, "configured": False},
        ), mock.patch(
            "aria.cli.install_coordinator_schedule"
        ) as install, contextlib.redirect_stdout(output):
            code = main(
                [
                    "coordinator",
                    "install",
                    "--project",
                    "demo",
                    "--github-client-id",
                    "Iv1.client123",
                    "--coordinator-integration-id",
                    "9001",
                    "--runtime-root",
                    "C:/runtime",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("requires the GitHub App key", output.getvalue())
        install.assert_not_called()

    def test_coordinator_status_remove_trigger_and_run_dispatch(self) -> None:
        commands = (
            (
                ["coordinator", "status", "--project", "demo"],
                "coordinator_schedule_status",
            ),
            (
                ["coordinator", "remove", "--project", "demo"],
                "remove_coordinator_schedule",
            ),
            (
                ["coordinator", "trigger", "--project", "demo"],
                "trigger_coordinator_schedule",
            ),
            (
                [
                    "coordinator",
                    "run",
                    "--config",
                    "C:/runtime/coordinator/demo.json",
                    "--expected-config-sha256",
                    "a" * 64,
                ],
                "run_configured_coordinator",
            ),
        )
        for argv, target in commands:
            with self.subTest(action=argv[1]), mock.patch(
                f"aria.cli.{target}", return_value={"ok": True}
            ) as operation, contextlib.redirect_stdout(io.StringIO()):
                code = main(argv)
            self.assertEqual(code, 0)
            operation.assert_called_once()

    def test_github_app_status_uses_os_credential_backend(self) -> None:
        backend = object()
        with mock.patch(
            "aria.cli.WindowsCredentialBackend", return_value=backend
        ), mock.patch(
            "aria.cli.github_app_key_status",
            return_value={"ok": True, "configured": True},
        ) as status, contextlib.redirect_stdout(io.StringIO()):
            code = main(["github-app", "status", "--app-id", "9001"])
        self.assertEqual(code, 0)
        status.assert_called_once_with(backend=backend, app_id=9001)


if __name__ == "__main__":
    unittest.main()
