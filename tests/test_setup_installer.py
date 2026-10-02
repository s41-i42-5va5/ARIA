from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.ps1"
UNINSTALL = ROOT / "uninstall.ps1"
RELEASE = ROOT / "releases" / "1.5.4"


class WindowsSetupInstallerTests(unittest.TestCase):
    def test_release_manifest_matches_every_offline_artifact(self) -> None:
        manifest = json.loads((RELEASE / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["version"], "1.5.4")
        self.assertEqual(manifest["release_status"], "candidate")
        self.assertEqual(manifest["verification"]["status"], "accepted")
        self.assertEqual(manifest["verification"]["scope"], "local-artifacts")
        self.assertEqual(manifest["verification"]["acceptance_checks"], 91)
        self.assertEqual(manifest["verification"]["source_tests"], 389)
        self.assertEqual(manifest["verification"]["github_live_acceptance"], "pending")
        self.assertEqual(
            manifest["wheel"], "aria_codex-1.5.4-py3-none-any.whl"
        )
        self.assertEqual(manifest["wheel_sha256"], manifest["files"][0]["sha256"])
        self.assertEqual(manifest["python_runtime"]["name"], "python.3.12.10.nupkg")
        self.assertEqual(manifest["python_runtime"]["version"], "3.12.10")
        self.assertNotIn("provider", manifest)
        names = {entry["name"] for entry in manifest["files"]}
        self.assertEqual(
            names,
            {
                "aria_codex-1.5.4-py3-none-any.whl",
                "cffi-2.1.0-cp312-cp312-win_amd64.whl",
                "cryptography-46.0.7-cp311-abi3-win_amd64.whl",
                "packaging-25.0-py3-none-any.whl",
                "pycparser-3.0-py3-none-any.whl",
                "python.3.12.10.nupkg",
                "pyyaml-6.0.3-cp312-cp312-win_amd64.whl",
                "setuptools-84.0.0-py3-none-any.whl",
                "wheel-0.48.0-py3-none-any.whl",
            },
        )
        for entry in manifest["files"]:
            artifact = RELEASE / entry["name"]
            self.assertTrue(artifact.is_file(), artifact)
            self.assertEqual(
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                entry["sha256"],
                artifact,
            )
            self.assertEqual(artifact.stat().st_size, entry["size_bytes"], artifact)

    def test_setup_is_fail_closed_and_covers_complete_install_flow(self) -> None:
        script = SETUP.read_text(encoding="utf-8")

        required_patterns = {
            "strict errors": r"\$ErrorActionPreference\s*=\s*'Stop'",
            "Python 3.12": r"Python 3\.12",
            "manifest hash verification": r"Get-FileHash.+SHA256",
            "manifest-bound runtime": r"Python runtime is not hash-bound",
            "explicit bundle manifest": r"\[string\]\$ManifestPath",
            "accepted release": r"verification\.status.+accepted",
            "offline install": r"'--no-index'",
            "local wheelhouse": r"'--find-links'",
            "dependency validation": r"'pip', 'check'",
            "isolated import": r"\$venvPython\s+'-I'\s+'-c'",
            "provenance boundary": r"Test-PathInside.+\$probe\.aria_file",
            "runtime boundary": r"\$env:ARIA_RUNTIME_ROOT\s*=",
            "framework root": r"'--framework-root'",
            "doctor": r"'doctor'",
            "safe recreation": r"Assert-SafeVenvTarget",
            "separate install root": r"InstallRoot must be outside",
            "separate runtime root": r"RuntimeRoot must be outside",
            "Git check": r"Git for Windows was not found",
            "registered Python discovery": r"PythonCore\\3\.12\\InstallPath",
            "bundled Python provenance": r"Get-AuthenticodeSignature",
            "side-by-side Python": r"System\.IO\.Compression\.ZipFile",
            "signed runtime executable": r"Bundled Python executable failed provenance",
            "Codex skill": r"'codex', 'install'",
            "CLI shim": r"aria\.cmd",
            "user PATH": r"EnvironmentVariableTarget\]::User",
            "install receipt": r"install-receipt\.json",
            "component ownership": r"codex_skill_owned",
        }
        for label, pattern in required_patterns.items():
            with self.subTest(label=label):
                self.assertRegex(script, re.compile(pattern, re.DOTALL))

        self.assertNotIn("Invoke-Expression", script)
        self.assertNotIn("--index-url", script)
        self.assertNotIn("--extra-index-url", script)

    def test_uninstaller_preserves_projects_runtime_and_provider_profile(self) -> None:
        script = UNINSTALL.read_text(encoding="utf-8")
        required_patterns = {
            "receipt required": r"install-receipt\.json",
            "exact target boundary": r"Test-PathInside",
            "coordinator task removal": r"coordinator remove --project",
            "Codex skill removal": r"'codex', 'remove'",
            "owned skill only": r"codex_skill_owned",
            "runtime preserved": r"runtime_preserved = \$true",
            "projects preserved": r"projects_preserved = \$true",
            "provider profile preserved": r"provider_profile_preserved = \$true",
            "safe runtime removal": r"bundled_python_kind.+nuget",
            "explicit modified skill force": r"ForceCodexSkill",
            "uninstall receipt": r"uninstall-receipt\.json",
        }
        for label, pattern in required_patterns.items():
            with self.subTest(label=label):
                self.assertRegex(script, re.compile(pattern, re.DOTALL))

        self.assertNotIn("Remove-Item -LiteralPath $runtimeRoot", script)
        self.assertNotIn("Invoke-Expression", script)


if __name__ == "__main__":
    unittest.main()
