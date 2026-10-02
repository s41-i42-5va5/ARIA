from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

import yaml

from aria.activity import validate_activity_snapshot
from aria.collaboration import build_control_contract, parse_control_contract
from aria.collaborative_backlog import validate_collaborative_backlog
from aria.collaborative_documents import (
    LEGACY_ROLE_PERMISSIONS,
    ROLE_PERMISSIONS,
    TRIAGE_ROLE_PERMISSIONS,
    build_initial_collaborative_documents,
    validate_collaborative_access,
    upgrade_legacy_collaborative_documents,
)
from aria.collaborative_team import validate_collaborative_team
from aria.github_control import CONTROL_DOCUMENTS


class CollaborativeDocumentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = build_control_contract(
            project_id="demo",
            provider="github",
            repository_id="123456789",
            integration_branch="dev",
            control_branch="aria-control",
        )

    def test_initial_set_is_complete_deterministic_and_empty(self) -> None:
        first = build_initial_collaborative_documents(
            self.contract, display_name="Demo Product"
        )
        second = build_initial_collaborative_documents(
            self.contract, display_name="Demo Product"
        )
        self.assertEqual(first, second)
        self.assertEqual(set(first.documents), CONTROL_DOCUMENTS)
        parse_control_contract(yaml.safe_load(first.documents["CONTROL.yaml"]))
        backlog = validate_collaborative_backlog(
            yaml.safe_load(first.documents["BACKLOG.yaml"])
        )
        activity = validate_activity_snapshot(
            yaml.safe_load(first.documents["ACTIVITY.yaml"])
        )
        team = validate_collaborative_team(
            yaml.safe_load(first.documents["ARIA_TEAM.yaml"])
        )
        self.assertEqual(backlog["revision"], 0)
        self.assertEqual(activity["revision"], 0)
        self.assertEqual(team["revision"], 0)

    def test_state_does_not_claim_unaccepted_code(self) -> None:
        documents = build_initial_collaborative_documents(
            self.contract, display_name="Demo Product"
        ).documents
        state = yaml.safe_load(documents["STATE.yaml"])
        self.assertEqual(state["schema_version"], 2)
        self.assertIsNone(state["accepted_head"])
        self.assertIsNone(state["accepted_at"])
        self.assertEqual(state["events"], [])
        self.assertEqual(documents["HISTORY.jsonl"], "\n")

    def test_access_policy_maps_provider_roles_to_least_privilege(self) -> None:
        documents = build_initial_collaborative_documents(
            self.contract, display_name="Demo Product"
        ).documents
        access = yaml.safe_load(documents["ACCESS.yaml"])
        self.assertEqual(access["role_permissions"], ROLE_PERMISSIONS)
        self.assertEqual(access["role_permissions"]["viewer"], ["state.read"])
        self.assertNotIn("team.sync", access["role_permissions"]["contributor"])
        self.assertIn("team.sync", access["role_permissions"]["admin"])

    def test_existing_154_access_policy_remains_readable_until_explicit_upgrade(self) -> None:
        documents = build_initial_collaborative_documents(
            self.contract, display_name="Demo Product"
        ).documents
        access = yaml.safe_load(documents["ACCESS.yaml"])
        access["role_permissions"] = LEGACY_ROLE_PERMISSIONS
        self.assertEqual(
            validate_collaborative_access(access, self.contract)["role_permissions"],
            LEGACY_ROLE_PERMISSIONS,
        )
        access["role_permissions"] = TRIAGE_ROLE_PERMISSIONS
        self.assertEqual(
            validate_collaborative_access(access, self.contract)["role_permissions"],
            TRIAGE_ROLE_PERMISSIONS,
        )

    def test_project_explicitly_declares_collaborative_mode(self) -> None:
        documents = build_initial_collaborative_documents(
            self.contract, display_name="Demo Product"
        ).documents
        project = yaml.safe_load(documents["PROJECT.yaml"])
        self.assertEqual(project["collaboration_mode"], "collaborative")
        self.assertEqual(project["documents"]["backlog"], "BACKLOG.yaml")
        self.assertEqual(
            project["repository"],
            {
                "provider": "github",
                "repository_id": "123456789",
                "remote": "origin",
                "main_branch": "main",
                "integration_branch": "dev",
                "working_branch_template": "work/{github_username}",
                "control_branch": "aria-control",
            },
        )

    def test_owner_recovery_upgrades_legacy_access_and_project_contract(self) -> None:
        documents = build_initial_collaborative_documents(
            self.contract, display_name="Demo Product"
        ).documents
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in documents.items():
                (root / name).write_text(content, encoding="utf-8")
            access = yaml.safe_load((root / "ACCESS.yaml").read_text(encoding="utf-8"))
            access["role_permissions"] = LEGACY_ROLE_PERMISSIONS
            (root / "ACCESS.yaml").write_text(
                yaml.safe_dump(access, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            project = yaml.safe_load((root / "PROJECT.yaml").read_text(encoding="utf-8"))
            project.pop("repository")
            (root / "PROJECT.yaml").write_text(
                yaml.safe_dump(project, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            changed = upgrade_legacy_collaborative_documents(
                root, contract=self.contract, coordinator_integration_id=9001
            )
            self.assertEqual(changed, {"access": True, "project": True})
            upgraded_access = yaml.safe_load(
                (root / "ACCESS.yaml").read_text(encoding="utf-8")
            )
            upgraded_project = yaml.safe_load(
                (root / "PROJECT.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(upgraded_access["role_permissions"], ROLE_PERMISSIONS)
            self.assertEqual(
                upgraded_project["repository"]["required_checks"],
                [{"context": "ARIA integration", "app_id": 9001}],
            )

    def test_owner_recovery_upgrades_immediate_predecessor_policy_in_place(self) -> None:
        documents = build_initial_collaborative_documents(
            self.contract, display_name="Demo Product"
        ).documents
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in documents.items():
                (root / name).write_text(content, encoding="utf-8")
            access = yaml.safe_load((root / "ACCESS.yaml").read_text(encoding="utf-8"))
            access["role_permissions"] = TRIAGE_ROLE_PERMISSIONS
            (root / "ACCESS.yaml").write_text(
                yaml.safe_dump(access, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            changed = upgrade_legacy_collaborative_documents(
                root, contract=self.contract, coordinator_integration_id=9001
            )
            self.assertEqual(changed, {"access": True, "project": True})
            upgraded_project = yaml.safe_load(
                (root / "PROJECT.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                upgraded_project["repository"]["required_checks"],
                [{"context": "ARIA integration", "app_id": 9001}],
            )


if __name__ == "__main__":
    unittest.main()
