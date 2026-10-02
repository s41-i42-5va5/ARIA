from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from aria.errors import ConfigurationError, WorkflowError
from aria.migration_1_4 import _team_template, _trust_template
from aria.migration_1_5 import _migration_journal, upgrade_project_to_1_5


class Migration15Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.docs = self.root / "docs"
        self.runtime = self.root / "runtime"
        self.docs.mkdir()
        self.project_path = self.docs / "PROJECT.yaml"
        self.project_path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "project_id": "sample",
                    "framework_version": "1.4.0",
                    "documents": {
                        "team": "ARIA_TEAM.yaml",
                        "trust": "TRUST.yaml",
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (self.docs / "ARIA_TEAM.yaml").write_bytes(_team_template())
        (self.docs / "TRUST.yaml").write_bytes(_trust_template())
        self.project = SimpleNamespace(
            project_id="sample",
            project_path=self.project_path,
            docs_root=self.docs,
            runtime_root=self.runtime,
            registry_path=self.runtime / "projects.toml",
            files=SimpleNamespace(team="ARIA_TEAM.yaml", trust="TRUST.yaml"),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migration_adds_governance_documents_and_is_idempotent(self) -> None:
        result = upgrade_project_to_1_5(self.project)
        self.assertTrue(result["ok"])
        self.assertFalse(result["idempotent"])
        project = yaml.safe_load(self.project_path.read_text(encoding="utf-8"))
        self.assertEqual(project["framework_version"], "1.5.5")
        self.assertEqual(project["documents"]["access"], "ACCESS.yaml")
        self.assertEqual(project["documents"]["backlog"], "BACKLOG.yaml")
        team = yaml.safe_load(
            (self.docs / "ARIA_TEAM.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(team["schema_version"], 2)
        self.assertEqual(team["project_id"], "sample")
        trust = yaml.safe_load(
            (self.docs / "TRUST.yaml").read_text(encoding="utf-8")
        )
        self.assertIn("access", trust["policies"])

        repeated = upgrade_project_to_1_5(self.project)
        self.assertTrue(repeated["idempotent"])

    def test_existing_1_5_project_adds_missing_executable_governance(self) -> None:
        upgrade_project_to_1_5(self.project)
        project = yaml.safe_load(self.project_path.read_text(encoding="utf-8"))
        project.pop("governance")
        self.project_path.write_text(
            yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
        )

        upgraded = upgrade_project_to_1_5(self.project)

        self.assertFalse(upgraded["idempotent"])
        written = yaml.safe_load(self.project_path.read_text(encoding="utf-8"))
        self.assertEqual(written["governance"]["status_authority"], "BACKLOG.yaml")
        self.assertTrue(
            written["governance"]["require_active_run_for_writes"]
        )

    def test_existing_1_5_3_project_upgrades_to_1_5_4_governance(self) -> None:
        upgrade_project_to_1_5(self.project)
        project = yaml.safe_load(self.project_path.read_text(encoding="utf-8"))
        project["framework_version"] = "1.5.3"
        project.pop("governance")
        self.project_path.write_text(
            yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
        )

        upgraded = upgrade_project_to_1_5(self.project)

        self.assertFalse(upgraded["idempotent"])
        written = yaml.safe_load(self.project_path.read_text(encoding="utf-8"))
        self.assertEqual(written["framework_version"], "1.5.5")
        self.assertEqual(written["governance"]["status_authority"], "BACKLOG.yaml")
        self.assertTrue(written["governance"]["require_active_run_for_writes"])

    def test_migration_blocks_open_run_without_mutating_project(self) -> None:
        run = self.runtime / "runs" / "20260729T080000Z-1234abcd"
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(
            '{"status":"started"}', encoding="utf-8"
        )
        before = self.project_path.read_bytes()
        with self.assertRaisesRegex(WorkflowError, "Close or discard open"):
            upgrade_project_to_1_5(self.project)
        self.assertEqual(self.project_path.read_bytes(), before)
        self.assertFalse((self.docs / "ACCESS.yaml").exists())

    def test_malformed_team_fails_before_writes(self) -> None:
        (self.docs / "ARIA_TEAM.yaml").write_text(
            "schema_version: 1\nactors: invalid\n", encoding="utf-8"
        )
        before = self.project_path.read_bytes()
        with self.assertRaises(ConfigurationError):
            upgrade_project_to_1_5(self.project)
        self.assertEqual(self.project_path.read_bytes(), before)
        self.assertFalse((self.docs / "BACKLOG.yaml").exists())

    def test_migration_rejects_colliding_document_pointers(self) -> None:
        project = yaml.safe_load(self.project_path.read_text(encoding="utf-8"))
        project["documents"]["access"] = "ARIA_TEAM.yaml"
        self.project_path.write_text(
            yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
        )
        team_before = (self.docs / "ARIA_TEAM.yaml").read_bytes()
        with self.assertRaisesRegex(ConfigurationError, "must be distinct"):
            upgrade_project_to_1_5(self.project)
        self.assertEqual((self.docs / "ARIA_TEAM.yaml").read_bytes(), team_before)
        self.assertFalse((self.docs / "BACKLOG.yaml").exists())

    def test_migration_recovers_every_partial_write_window(self) -> None:
        paths = [
            self.docs / "ARIA_TEAM.yaml",
            self.docs / "TRUST.yaml",
            self.docs / "ACCESS.yaml",
            self.docs / "BACKLOG.yaml",
            self.project_path,
        ]
        before = {
            path: path.read_bytes() if path.is_file() else None for path in paths
        }
        upgrade_project_to_1_5(self.project)
        after = {path: path.read_bytes() for path in paths}
        journal_path = _migration_journal(self.project)

        for written_count in range(1, len(paths) + 1):
            for path, content in before.items():
                if content is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_bytes(content)
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            journal_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_id": "sample",
                        "phase": "prepared",
                        "targets": [
                            {
                                "path": str(path.resolve(strict=False)),
                                "existed": content is not None,
                                "content_base64": (
                                    base64.b64encode(content).decode("ascii")
                                    if content is not None
                                    else None
                                ),
                            }
                            for path, content in before.items()
                        ],
                    }
                ),
                encoding="utf-8",
            )
            for path in paths[:written_count]:
                path.write_bytes(after[path])

            recovered = upgrade_project_to_1_5(self.project)

            self.assertFalse(recovered["idempotent"])
            self.assertFalse(journal_path.exists())
            self.assertEqual(
                {path: path.read_bytes() for path in paths},
                after,
                f"failed recovery after {written_count} partial writes",
            )

    def test_migration_recovery_rejects_path_outside_project_docs(self) -> None:
        journal_path = _migration_journal(self.project)
        journal_path.parent.mkdir(parents=True)
        escaped = self.root / "outside.txt"
        journal_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "sample",
                    "phase": "prepared",
                    "targets": [
                        {
                            "path": str(escaped.resolve()),
                            "existed": False,
                            "content_base64": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(WorkflowError, "escapes project docs"):
            upgrade_project_to_1_5(self.project)


if __name__ == "__main__":
    unittest.main()
