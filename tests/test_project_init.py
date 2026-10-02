from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from aria.errors import ConfigurationError
from aria.project import load_project, run_project_doctor
from aria.project_init import initialize_project


class ProjectInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.code = self.root / "product"
        self.docs = self.root / "product-aria"
        self.runtime = self.root / "runtime"
        self.framework = Path(__file__).resolve().parents[1]
        (self.code / "src").mkdir(parents=True)
        (self.code / "pyproject.toml").write_text(
            "[project]\nname='semantic-demo'\nversion='0.1.0'\n",
            encoding="utf-8",
        )
        (self.code / "src" / "service.py").write_text(
            "def value():\n    return 1\n", encoding="utf-8"
        )
        self._git("init", "-q")
        self._git("config", "user.email", "tests@example.invalid")
        self._git("config", "user.name", "ARIA Tests")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "fixture")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.code), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_init_derives_deterministic_docs_registers_and_requires_semantic_review(
        self,
    ) -> None:
        result = initialize_project(
            "semantic-demo",
            code_root=self.code,
            docs_root=self.docs,
            runtime_root=self.runtime,
        )
        self.assertFalse(result["semantic_bootstrap"])
        self.assertTrue(result["semantic_review_required"])
        self.assertEqual(result["bootstrap_kind"], "deterministic_git_inventory")
        self.assertIn("identity enroll", result["next_action"])
        self.assertIn("access bootstrap", result["next_action"])
        self.assertIn("--identity-actor", result["next_action"])
        self.assertEqual(result["manifests"], ["pyproject.toml"])
        project = load_project(
            "semantic-demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
        )
        doctor = run_project_doctor(project)
        self.assertTrue(doctor["ok"], doctor)
        system_map = (self.docs / "SYSTEM_MAP.yaml").read_text(encoding="utf-8")
        self.assertIn("src/**", system_map)
        self.assertIn("Task-specific", system_map)
        stack = (self.docs / "STACK.md").read_text(encoding="utf-8")
        self.assertIn("Deterministic bootstrap", stack)
        self.assertIn("inspect the real code", stack)
        self.assertNotIn("unittest discover", stack)

    def test_init_refuses_to_overwrite_docs(self) -> None:
        self.docs.mkdir()
        with self.assertRaisesRegex(ConfigurationError, "refuses to overwrite"):
            initialize_project(
                "semantic-demo",
                code_root=self.code,
                docs_root=self.docs,
                runtime_root=self.runtime,
            )

    def test_project_doctor_requires_team_and_trust_documents(self) -> None:
        initialize_project(
            "semantic-demo",
            code_root=self.code,
            docs_root=self.docs,
            runtime_root=self.runtime,
        )
        project = load_project(
            "semantic-demo",
            framework_root=self.framework,
            runtime_root=self.runtime,
        )
        (self.docs / "TRUST.yaml").unlink()
        result = run_project_doctor(project)
        self.assertFalse(result["ok"])
        failed = {row["id"] for row in result["checks"] if row["ok"] is False}
        self.assertIn("required:TRUST.yaml", failed)

    def test_init_rejects_invalid_identity_before_writing_docs(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "Invalid project id"):
            initialize_project(
                "INVALID ID",
                code_root=self.code,
                docs_root=self.docs,
                runtime_root=self.runtime,
            )
        self.assertFalse(self.docs.exists())

    def test_init_generates_structured_python_node_rust_and_go_adapters(self) -> None:
        (self.code / "pyproject.toml").write_text(
            "[project]\nname='semantic-demo'\nversion='0.1.0'\n"
            "[tool.pytest.ini_options]\naddopts='-q'\n",
            encoding="utf-8",
        )
        (self.code / "package.json").write_text(
            '{"name":"semantic-demo","scripts":{"test":"node --test"}}',
            encoding="utf-8",
        )
        (self.code / "Cargo.toml").write_text(
            "[package]\nname='semantic-demo'\nversion='0.1.0'\n",
            encoding="utf-8",
        )
        (self.code / "go.mod").write_text(
            "module example.invalid/semantic-demo\n\ngo 1.22\n",
            encoding="utf-8",
        )
        self._git("add", ".")
        self._git("commit", "-q", "-m", "add verification manifests")

        result = initialize_project(
            "semantic-demo",
            code_root=self.code,
            docs_root=self.docs,
            runtime_root=self.runtime,
        )

        verification = yaml.safe_load(
            (self.docs / "VERIFY.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {row["adapter"] for row in verification["commands"]},
            {"python", "node", "rust", "go"},
        )
        self.assertEqual(
            {tuple(row["classes"]) for row in verification["commands"]},
            {("focused",)},
        )
        self.assertEqual(len(result["verification_commands"]), 4)
        project = load_project(
            "semantic-demo", framework_root=self.framework, runtime_root=self.runtime
        )
        team = yaml.safe_load(
            (self.docs / "ARIA_TEAM.yaml").read_text(encoding="utf-8")
        )
        trust = yaml.safe_load(
            (self.docs / "TRUST.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project.project_path.name,
            "PROJECT.yaml",
        )
        self.assertEqual(
            {actor["id"] for actor in team["actors"]},
            {"local-owner", "github-actions"},
        )
        self.assertEqual(trust["policies"]["release"]["minimum_trust_level"], "ci-signed")
        self.assertEqual(trust["keys"], [])
        self.assertTrue(run_project_doctor(project)["ok"])

    def test_init_rolls_back_only_its_new_docs_when_registration_fails(self) -> None:
        with patch(
            "aria.project_init.register_project",
            side_effect=ConfigurationError("registry race"),
        ), self.assertRaisesRegex(ConfigurationError, "registry race"):
            initialize_project(
                "semantic-demo",
                code_root=self.code,
                docs_root=self.docs,
                runtime_root=self.runtime,
            )
        self.assertFalse(self.docs.exists())


if __name__ == "__main__":
    unittest.main()
