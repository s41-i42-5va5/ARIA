from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

from aria.errors import WorkflowError
from aria.migration_1_4 import upgrade_project_to_1_4


class Migration14Tests(unittest.TestCase):
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
                    "framework_version": "1.3.0",
                    "documents": {"state": "STATE.yaml"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.project = SimpleNamespace(
            project_id="sample",
            project_path=self.project_path,
            docs_root=self.docs,
            runtime_root=self.runtime,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migration_is_recoverable_and_idempotent(self) -> None:
        migrated = upgrade_project_to_1_4(self.project)
        self.assertTrue(migrated["ok"])
        self.assertFalse(migrated["idempotent"])
        project = yaml.safe_load(self.project_path.read_text(encoding="utf-8"))
        self.assertEqual(project["framework_version"], "1.4.0")
        self.assertEqual(project["documents"]["team"], "ARIA_TEAM.yaml")
        self.assertTrue((self.docs / "ARIA_TEAM.yaml").is_file())
        self.assertTrue((self.docs / "TRUST.yaml").is_file())
        configured = self.docs / "governance"
        configured.mkdir()
        (self.docs / "ARIA_TEAM.yaml").rename(configured / "team.yaml")
        (self.docs / "TRUST.yaml").rename(configured / "trust.yaml")
        project["documents"]["team"] = "governance/team.yaml"
        project["documents"]["trust"] = "governance/trust.yaml"
        self.project_path.write_text(
            yaml.safe_dump(project, sort_keys=False), encoding="utf-8"
        )
        open_run = self.runtime / "runs" / "20260728T080000Z-1234abcd"
        open_run.mkdir(parents=True)
        (open_run / "manifest.json").write_text(
            '{"status":"started"}', encoding="utf-8"
        )
        repeated = upgrade_project_to_1_4(self.project)
        self.assertTrue(repeated["idempotent"])
        preserved = yaml.safe_load(
            self.project_path.read_text(encoding="utf-8")
        )
        self.assertEqual(preserved["documents"]["team"], "governance/team.yaml")
        self.assertEqual(preserved["documents"]["trust"], "governance/trust.yaml")

    def test_migration_blocks_open_legacy_run(self) -> None:
        run = self.runtime / "runs" / "20260728T080000Z-1234abcd"
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(
            '{"status":"started"}', encoding="utf-8"
        )
        with self.assertRaisesRegex(WorkflowError, "open pre-1.4 runs"):
            upgrade_project_to_1_4(self.project)
        project = yaml.safe_load(self.project_path.read_text(encoding="utf-8"))
        self.assertEqual(project["framework_version"], "1.3.0")


if __name__ == "__main__":
    unittest.main()
