from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aria.errors import ConfigurationError
from aria.framework_doctor import run_framework_doctor
from aria.registry import list_registered_projects, register_project


class RegistryTests(unittest.TestCase):
    def test_public_registration_cannot_bypass_shadow_canary_cutover(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            docs = root / "docs"
            code = root / "code"
            docs.mkdir()
            code.mkdir()
            (docs / "PROJECT.yaml").write_text(
                "schema_version: 1\nproject_id: demo\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ConfigurationError, "shadow-only"):
                register_project(
                    "demo",
                    docs_root=docs,
                    code_root=code,
                    mode="active",
                    runtime_root=root / "runtime",
                    engine_sha256="0" * 64,
                )

    def test_register_is_atomic_and_preserves_multiple_projects(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runtime = root / "runtime"
            for project_id in ("alpha", "beta"):
                docs = root / f"{project_id}-docs"
                code = root / f"{project_id}-code"
                docs.mkdir()
                code.mkdir()
                (docs / "PROJECT.yaml").write_text(
                    f"schema_version: 1\nproject_id: {project_id}\n",
                    encoding="utf-8",
                )
                register_project(
                    project_id,
                    docs_root=docs,
                    code_root=code,
                    runtime_root=runtime,
                )
            listed = list_registered_projects(runtime_root=runtime)
            self.assertTrue(listed["ok"])
            self.assertEqual(
                [row["project"] for row in listed["projects"]], ["alpha", "beta"]
            )
            self.assertIn("[projects.alpha]", (runtime / "projects.toml").read_text())
            self.assertIn("[projects.beta]", (runtime / "projects.toml").read_text())

    def test_framework_doctor_checks_runtime_registry_without_product_env(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            runtime = Path(name)
            docs = runtime / "docs"
            code = runtime / "code"
            docs.mkdir()
            code.mkdir()
            (docs / "PROJECT.yaml").write_text(
                "schema_version: 1\nproject_id: demo\n", encoding="utf-8"
            )
            register_project(
                "demo", docs_root=docs, code_root=code, runtime_root=runtime
            )
            framework = Path(__file__).resolve().parents[1]
            with patch(
                "aria.framework_doctor.default_runtime_root", return_value=runtime
            ):
                result = run_framework_doctor(framework)
            self.assertTrue(result["ok"])
            self.assertEqual(result["version"], "1.5.5")

    def test_framework_doctor_accepts_clean_install_before_first_registration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as name:
            runtime = Path(name)
            framework = Path(__file__).resolve().parents[1]
            with patch(
                "aria.framework_doctor.default_runtime_root", return_value=runtime
            ):
                result = run_framework_doctor(framework)
            self.assertTrue(result["ok"], result)
            registry = next(
                row for row in result["checks"] if row["id"] == "project_registry"
            )
            self.assertTrue(registry["ok"])
            self.assertIn("projects=0", registry["detail"])


if __name__ == "__main__":
    unittest.main()
