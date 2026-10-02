from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aria.cli import _verified_backlog_branch, build_parser, main
from aria.errors import WorkflowError


class Cli15Tests(unittest.TestCase):
    def test_parser_exposes_identity_access_backlog_and_migration(self) -> None:
        parser = build_parser()
        identity = parser.parse_args(
            [
                "identity",
                "enroll",
                "--actor",
                "alice",
                "--device",
                "workstation",
                "--output",
                "request.json",
            ]
        )
        self.assertEqual(identity.identity_action, "enroll")
        access = parser.parse_args(
            [
                "--identity-actor",
                "owner",
                "access",
                "grant",
                "--project",
                "demo",
                "--request",
                "request.json",
                "--permission",
                "backlog.read",
                "--expected-revision",
                "1",
            ]
        )
        self.assertEqual(access.permission, ["backlog.read"])
        backlog = parser.parse_args(
            [
                "--identity-device",
                "workstation",
                "backlog",
                "done",
                "--project",
                "demo",
                "--item",
                "BLG-1",
                "--evidence",
                "run:1",
                "--branch",
                "feature/audit",
                "--expected-revision",
                "2",
            ]
        )
        self.assertEqual(backlog.backlog_action, "done")
        self.assertEqual(backlog.branch, "feature/audit")
        migration = parser.parse_args(["upgrade-1-5", "--project", "demo"])
        self.assertEqual(migration.command, "upgrade-1-5")
        feature = parser.parse_args(
            [
                "feature",
                "--project",
                "demo",
                "--task",
                "Governed change",
                "--backlog-item",
                "BLG-1",
            ]
        )
        self.assertEqual(feature.backlog_item, "BLG-1")
        preflight = parser.parse_args(
            [
                "preflight",
                "--project",
                "demo",
                "--operation",
                "write",
                "--run",
                "20260824T120000Z-a1b2c3d4",
            ]
        )
        self.assertEqual(preflight.operation, "write")
        reconciliation = parser.parse_args(
            [
                "governance",
                "reconcile",
                "--project",
                "demo",
                "--item",
                "BLG-1",
                "--expected-revision",
                "4",
                "--confirm-acceptance",
            ]
        )
        self.assertEqual(reconciliation.governance_action, "reconcile")
        collaboration = parser.parse_args(
            [
                "collaboration",
                "plan",
                "--project",
                "demo",
                "--code-root",
                "product",
                "--provider",
                "github",
                "--repository-id",
                "123456789",
            ]
        )
        self.assertEqual(collaboration.collaboration_action, "plan")
        self.assertEqual(collaboration.integration_branch, "dev")
        self.assertEqual(collaboration.control_branch, "aria-control")
        collaboration_enable = parser.parse_args(
            [
                "collaboration",
                "enable",
                "--project",
                "demo",
                "--code-root",
                "product",
                "--provider",
                "github",
                "--repository-id",
                "123456789",
                "--expected-plan-sha256",
                "0" * 64,
                "--confirm",
            ]
        )
        self.assertTrue(collaboration_enable.confirm)
        migration_plan = parser.parse_args(
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
        self.assertEqual(migration_plan.collaboration_action, "migrate-plan")
        self.assertEqual(migration_plan.actor_map, ["local-owner=100"])
        migration_apply = parser.parse_args(
            [
                "collaboration",
                "migrate",
                "--project",
                "demo",
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
        self.assertTrue(migration_apply.confirm)
        team_sync = parser.parse_args(
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
                "4",
            ]
        )
        self.assertEqual(team_sync.collaboration_action, "team-sync")
        self.assertEqual(team_sync.expected_revision, 4)
        outbox_flush = parser.parse_args(
            [
                "collaboration",
                "activity-outbox-flush",
                "--project",
                "demo",
                "--github-client-id",
                "Iv1.client123",
            ]
        )
        self.assertEqual(outbox_flush.collaboration_action, "activity-outbox-flush")
        self.assertEqual(outbox_flush.max_requests, 20)
        team_status = parser.parse_args(
            ["collaboration", "team-status", "--project", "demo"]
        )
        self.assertEqual(team_status.collaboration_action, "team-status")
        github_app = parser.parse_args(
            [
                "github-app",
                "configure",
                "--app-id",
                "9001",
                "--private-key",
                "coordinator.pem",
            ]
        )
        self.assertEqual(github_app.github_app_action, "configure")
        self.assertEqual(github_app.app_id, 9001)

    def test_identity_cli_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            request = Path(temporary) / "request.json"
            output = io.StringIO()
            with patch("aria.cli.default_runtime_root", return_value=runtime):
                with redirect_stdout(output):
                    code = main(
                        [
                            "identity",
                            "enroll",
                            "--actor",
                            "alice",
                            "--device",
                            "workstation",
                            "--output",
                            str(request),
                        ]
                    )
                self.assertEqual(code, 0)
                enrolled = json.loads(output.getvalue())
                self.assertEqual(enrolled["actor_id"], "alice")
                self.assertTrue(request.is_file())
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(["identity", "whoami", "--actor", "alice"])
                self.assertEqual(code, 0)
                status = json.loads(output.getvalue())
                self.assertEqual(status["count"], 1)
                self.assertNotIn(
                    "protected_private_key", status["identities"][0]
                )

    def test_backlog_branch_is_read_from_git_and_spoofing_is_rejected(self) -> None:
        project = SimpleNamespace(
            code_root=Path("code"),
            git_ignore_prefixes=(),
        )
        with patch(
            "aria.cli.git_snapshot",
            return_value={"branch": "feature/audit"},
        ):
            self.assertEqual(
                _verified_backlog_branch(project, None),
                "feature/audit",
            )
            self.assertEqual(
                _verified_backlog_branch(project, "feature/audit"),
                "feature/audit",
            )
            with self.assertRaisesRegex(WorkflowError, "does not match"):
                _verified_backlog_branch(project, "main")


if __name__ == "__main__":
    unittest.main()
