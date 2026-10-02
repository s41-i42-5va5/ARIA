from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aria.errors import ConfigurationError
from aria.release_check import (
    _candidate_git_identity,
    _candidate_state,
    _expect_failure,
    _run,
    _run_claim_collision,
    _release_environment,
    _temporary_release_workspace,
    _validate_run_policy,
    _wheel_reproducibility_check,
)


class ReleaseCheckTests(unittest.TestCase):
    def test_candidate_git_identity_requires_clean_committed_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "ARIA Tests"],
                cwd=root,
                check=True,
            )
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "candidate"], cwd=root, check=True
            )

            clean = _candidate_git_identity(root)
            self.assertTrue(clean["clean"])
            self.assertEqual(len(str(clean["head"])), 40)
            self.assertTrue(str(clean["commit_timestamp"]).isdigit())

            source.write_text("VALUE = 2\n", encoding="utf-8")
            dirty = _candidate_git_identity(root)
            self.assertFalse(dirty["clean"])
            self.assertTrue(dirty["changes"])

    def test_candidate_state_detects_tracked_file_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            source = root / "candidate.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.py"], cwd=root, check=True)
            before = _candidate_state(root)

            source.write_text("VALUE = 2\n", encoding="utf-8")
            after = _candidate_state(root)

            self.assertEqual(before["count"], after["count"])
            self.assertNotEqual(before["sha256"], after["sha256"])

    def test_release_environment_isolated_from_python_and_global_pip_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "acceptance"
            scratch = Path(directory) / "scratch"
            inherited = {
                **os.environ,
                "PYTHONPATH": "shadow-package",
                "PYTHONHOME": "shadow-runtime",
                "PYTHONSTARTUP": "startup.py",
                "PIP_CACHE_DIR": "global-cache",
                "PIP_INDEX_URL": "https://example.invalid/simple",
                "PIP_EXTRA_INDEX_URL": "https://extra.example.invalid/simple",
            }
            with patch.dict(os.environ, inherited, clear=True):
                env = _release_environment(
                    output,
                    temp_root=scratch,
                    source_date_epoch="1700000000",
                )

            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("PYTHONHOME", env)
            self.assertNotIn("PYTHONSTARTUP", env)
            self.assertNotIn("PIP_INDEX_URL", env)
            self.assertNotIn("PIP_EXTRA_INDEX_URL", env)
            self.assertEqual(env["PIP_CACHE_DIR"], str(output / "pip-cache"))
            self.assertEqual(env["TEMP"], str(scratch))
            self.assertEqual(env["TMP"], str(scratch))
            self.assertEqual(env["PIP_DISABLE_PIP_VERSION_CHECK"], "1")
            self.assertEqual(env["PIP_NO_INDEX"], "1")
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")
            self.assertEqual(env["SOURCE_DATE_EPOCH"], "1700000000")
            self.assertTrue((output / "pip-cache").is_dir())
            self.assertTrue(scratch.is_dir())

            with self.assertRaisesRegex(ConfigurationError, "SOURCE_DATE_EPOCH"):
                _release_environment(
                    output,
                    temp_root=scratch,
                    source_date_epoch="not-a-timestamp",
                )

    def test_wheel_reproducibility_requires_one_byte_identical_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary" / "aria_codex-1.5.5-py3-none-any.whl"
            rebuilt = root / "rebuilt" / primary.name
            primary.parent.mkdir()
            rebuilt.parent.mkdir()
            primary.write_bytes(b"same-wheel")
            rebuilt.write_bytes(b"same-wheel")

            matching = _wheel_reproducibility_check([primary], [rebuilt])
            self.assertTrue(matching["ok"])
            self.assertEqual(matching["primary_sha256"], matching["rebuilt_sha256"])

            rebuilt.write_bytes(b"different-wheel")
            changed = _wheel_reproducibility_check([primary], [rebuilt])
            self.assertFalse(changed["ok"])

    def test_run_records_raw_log_hash_size_and_actual_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            result = _run(
                "proof",
                [os.sys.executable, "-c", "print('verified-output')"],
                cwd=root,
                logs=logs,
            )
            content = (logs / "proof.log").read_bytes()
            self.assertTrue(result["ok"])
            self.assertEqual(result["log_size_bytes"], len(content))
            self.assertEqual(
                result["log_sha256"],
                __import__("hashlib").sha256(content).hexdigest(),
            )
            self.assertEqual(
                result["actual_result"], "Command completed successfully"
            )

    def test_expected_failure_requires_rejection_and_expected_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            rejected = _expect_failure(
                "rejected",
                [
                    os.sys.executable,
                    "-c",
                    "import sys; print('scope is not allowed'); sys.exit(3)",
                ],
                cwd=root,
                logs=logs,
                contains="not allowed",
            )
            wrong_reason = _expect_failure(
                "wrong-reason",
                [os.sys.executable, "-c", "import sys; print('other'); sys.exit(3)"],
                cwd=root,
                logs=logs,
                contains="not allowed",
            )
            self.assertTrue(rejected["ok"])
            self.assertFalse(wrong_reason["ok"])

    def test_claim_collision_records_exactly_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            marker = root / "claim.lock"
            script = (
                "import os,sys; "
                "fd=os.open(sys.argv[1], os.O_CREAT|os.O_EXCL|os.O_WRONLY); "
                "os.close(fd)"
            )
            collision = _run_claim_collision(
                commands=[
                    (
                        "actor-a",
                        [os.sys.executable, "-c", script, str(marker)],
                    ),
                    (
                        "actor-b",
                        [os.sys.executable, "-c", script, str(marker)],
                    ),
                ],
                cwd=root,
                logs=logs,
                env=dict(os.environ),
            )
            self.assertTrue(collision["ok"])
            attempts = collision["attempts"]
            self.assertEqual(
                sum(row["ok"] is True for row in attempts),  # type: ignore[index]
                1,
            )
            self.assertEqual(
                sum(row["exit_code"] != 0 for row in attempts),  # type: ignore[index]
                1,
            )

    def test_smoke_workspace_is_outside_framework_root_and_disposable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            framework_root = Path(directory) / "framework"
            framework_root.mkdir()
            workspace, smoke = _temporary_release_workspace(framework_root)
            try:
                self.assertNotEqual(smoke, framework_root)
                self.assertNotIn(framework_root, smoke.parents)
                marker = smoke / "marker.txt"
                marker.write_text("temporary", encoding="utf-8")
                self.assertTrue(marker.is_file())
            finally:
                workspace.cleanup()
            self.assertFalse(smoke.exists())

    def test_run_policy_validation_reads_both_output_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            log_path = root / "route.log"
            route = {"mode": "deep", "intent": "build", "mechanism": "next-task-new"}
            manifest_path.write_text(
                '{"route":{"mode":"deep","intent":"build","mechanism":"next-task-new"},'
                '"managed_lifecycle":true}',
                encoding="utf-8",
            )
            log_path.write_text(
                '{"manifest_path":' + json.dumps(str(manifest_path)) + ',"route":' + json.dumps(route) + '}',
                encoding="utf-8",
            )
            check: dict[str, object] = {"ok": True, "log_path": str(log_path)}

            _validate_run_policy(
                check,
                mode="deep",
                intent="build",
                mechanism="next-task-new",
                managed_lifecycle=True,
            )

            self.assertTrue(check["ok"])
            self.assertEqual(check["verified_policy"]["mode"], "deep")  # type: ignore[index]

            _validate_run_policy(
                check,
                mode="standard",
                intent="build",
                mechanism="direct-standard",
                managed_lifecycle=True,
            )
            self.assertFalse(check["ok"])
            self.assertIn("manifest policy mismatch", str(check["policy_validation_error"]))


if __name__ == "__main__":
    unittest.main()
