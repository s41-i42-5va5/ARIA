from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aria.errors import ConfigurationError
from aria.cli import build_parser
from aria.framework_doctor import run_framework_doctor
from aria.project import _framework_root


class InstalledPackageFrameworkTests(unittest.TestCase):
    def test_framework_root_is_a_public_cli_option(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--framework-root", "C:/aria", "doctor"])
        self.assertEqual(args.project_root, Path("C:/aria"))
        self.assertIn("--framework-root", parser.format_help())

    def test_matching_wheel_modules_can_bind_operational_framework_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            installed = base / "venv" / "Lib" / "site-packages"
            framework = base / "aria-codex"
            (installed / "aria").mkdir(parents=True)
            (framework / "aria").mkdir(parents=True)
            for relative, content in {
                "__init__.py": "__version__ = '1.1.0'\n",
                "project.py": "VALUE = 1\n",
            }.items():
                (installed / "aria" / relative).write_text(content, encoding="utf-8")
                (framework / "aria" / relative).write_text(content, encoding="utf-8")
            (framework / ".aria-root").write_text("aria-codex\n", encoding="utf-8")

            with patch("aria.project.__file__", str(installed / "aria" / "project.py")):
                self.assertEqual(_framework_root(framework), framework.resolve())

    def test_wheel_binding_rejects_module_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            installed = base / "venv" / "Lib" / "site-packages"
            framework = base / "aria-codex"
            (installed / "aria").mkdir(parents=True)
            (framework / "aria").mkdir(parents=True)
            (installed / "aria" / "project.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            (framework / "aria" / "project.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            (framework / ".aria-root").write_text("aria-codex\n", encoding="utf-8")

            with patch("aria.project.__file__", str(installed / "aria" / "project.py")):
                with self.assertRaisesRegex(ConfigurationError, "running ARIA framework"):
                    _framework_root(framework)

                with self.assertRaisesRegex(ConfigurationError, "running ARIA framework"):
                    run_framework_doctor(framework)

    def test_wheel_binding_normalizes_unreadable_module_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            installed = base / "venv" / "Lib" / "site-packages"
            framework = base / "aria-codex"
            (installed / "aria").mkdir(parents=True)
            (framework / "aria").mkdir(parents=True)
            for root in (installed, framework):
                (root / "aria" / "project.py").write_text(
                    "VALUE = 1\n", encoding="utf-8"
                )
            (framework / ".aria-root").write_text("aria-codex\n", encoding="utf-8")

            with (
                patch("aria.project.__file__", str(installed / "aria" / "project.py")),
                patch("pathlib.Path.read_bytes", side_effect=PermissionError("denied")),
            ):
                with self.assertRaisesRegex(
                    ConfigurationError, "Cannot verify installed ARIA modules"
                ):
                    _framework_root(framework)


if __name__ == "__main__":
    unittest.main()
