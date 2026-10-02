from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import yaml

from aria.ci import (
    attest_ci_result,
    authorize_ci_job,
    execute_ci_job,
    github_workflow,
    import_ci_result,
    prepare_ci_job,
)
from aria.errors import WorkflowError
from aria.io import atomic_write_json
from aria.signing import generate_keypair


class TrustedCiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self._git("init")
        self._git("config", "user.email", "aria@example.invalid")
        self._git("config", "user.name", "ARIA Test")
        (self.checkout / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "baseline")
        self.commit = self._git("rev-parse", "HEAD")
        self.run_id = "20260728T080000Z-1234abcd"
        self.now = datetime.now(UTC)
        self.job = self.root / "job.json"
        self.authorizer_private = self.root / "authorizer-private.pem"
        self.authorizer_public = self.root / "authorizer-public.pem"
        generate_keypair(self.authorizer_private, self.authorizer_public)
        self.private = self.root / "attester-private.pem"
        self.public = self.root / "attester-public.pem"
        generate_keypair(self.private, self.public)
        payload = {
            "schema_version": 1,
            "kind": "aria-ci-job",
            "job_id": "job-" + "1" * 32,
            "nonce": "2" * 64,
            "created_at": self.now.isoformat().replace("+00:00", "Z"),
            "expires_at": (self.now + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
            "project_id": "sample",
            "run_id": self.run_id,
            "source_commit": self.commit,
            "feature_contract_sha256": "f" * 64,
            "execution_contract_sha256": "e" * 64,
            "commands": [
                {
                    "id": "focused",
                    "adapter": "python",
                    "argv": ["python", "-c", "print('CI_OK')"],
                    "cwd": ".",
                    "classes": ["focused"],
                    "timeout_seconds": 30,
                }
            ],
        }
        self.job_payload = authorize_ci_job(
            payload,
            private_key_path=self.authorizer_private,
            actor_id="alice",
        )
        atomic_write_json(self.job, self.job_payload)
        self.package = self.root / "result.aria-evidence"
        self.unsigned = self.root / "unsigned-result.zip"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.checkout,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    def _job_policy(self) -> Path:
        authorization = self.job_payload["authorization"]
        payload = {
            "schema_version": 1,
            "keys": [
                {
                    "id": authorization["key_id"],
                    "public_key": authorization["public_key"],
                    "actor_id": "alice",
                    "status": "trusted",
                }
            ],
            "policies": {
                "job": {
                    "minimum_trust_level": "signed",
                    "trusted_keys": [authorization["key_id"]],
                }
            },
        }
        path = self.root / "JOB_TRUST.yaml"
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def _policy(self) -> Path:
        with zipfile.ZipFile(self.package) as archive:
            signature = json.loads(archive.read("signature.json"))
        authorization = self.job_payload["authorization"]
        payload = {
            "schema_version": 1,
            "keys": [
                {
                    "id": authorization["key_id"],
                    "public_key": authorization["public_key"],
                    "actor_id": "alice",
                    "status": "trusted",
                },
                {
                    "id": signature["key_id"],
                    "public_key": signature["public_key"],
                    "actor_id": "github-actions",
                    "status": "trusted",
                }
            ],
            "policies": {
                "job": {
                    "minimum_trust_level": "signed",
                    "trusted_keys": [authorization["key_id"]],
                },
                "ci": {
                    "minimum_trust_level": "ci-signed",
                    "trusted_keys": [signature["key_id"]],
                }
            },
        }
        path = self.root / "TRUST.yaml"
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        return path

    def _project(self) -> SimpleNamespace:
        runtime = self.root / "runtime"
        run_root = runtime / "runs" / self.run_id
        run_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            run_root / "manifest.json",
            {
                "project_id": "sample",
                "run_id": self.run_id,
                "context": {"git": {"head": self.commit}},
                "feature_contract_lock": {"sha256": "f" * 64},
                "execution_contract": {"sha256": "e" * 64},
            },
        )
        return SimpleNamespace(
            project_id="sample",
            runtime_root=runtime,
            code_root=self.checkout,
            git_ignore_prefixes=(),
        )

    def _prepare_project(self) -> SimpleNamespace:
        runtime = self.root / "prepare-runtime"
        run_root = runtime / "runs" / self.run_id
        execution = run_root / "execution"
        execution.mkdir(parents=True, exist_ok=True)
        contract = {"schema_version": 1, "commands": self.job_payload["commands"]}
        atomic_write_json(execution / "contract.json", contract)
        contract_sha = hashlib.sha256(
            (execution / "contract.json").read_bytes()
        ).hexdigest()
        atomic_write_json(
            run_root / "manifest.json",
            {
                "project_id": "sample",
                "run_id": self.run_id,
                "context": {"git": {"head": self.commit}},
                "feature_contract_lock": {"sha256": "f" * 64},
                "execution_contract": {
                    "path": "execution/contract.json",
                    "sha256": contract_sha,
                },
            },
        )
        return SimpleNamespace(
            project_id="sample",
            runtime_root=runtime,
            code_root=self.checkout,
            git_ignore_prefixes=(),
        )

    def test_execute_isolated_and_import_exact_job_binding(self) -> None:
        before_status = self._git("status", "--porcelain=v1", "--untracked-files=all")
        result = execute_ci_job(
            job_path=self.job,
            checkout=self.checkout,
            output_path=self.unsigned,
            job_trust_policy_path=self._job_policy(),
            job_policy_name="job",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["source_checkout_unchanged"])
        self.assertEqual(
            self._git("status", "--porcelain=v1", "--untracked-files=all"),
            before_status,
        )
        attested = attest_ci_result(
            job_path=self.job,
            unsigned_result_path=self.unsigned,
            output_path=self.package,
            private_key_path=self.private,
            actor_id="github-actions",
            job_trust_policy_path=self._job_policy(),
            job_policy_name="job",
        )
        self.assertTrue(attested["ok"])
        imported = import_ci_result(
            self._project(),
            run_id=self.run_id,
            job_path=self.job,
            package_path=self.package,
            trust_policy_path=self._policy(),
            policy_name="ci",
            job_trust_policy_path=self._policy(),
            job_policy_name="job",
        )
        self.assertTrue(imported["ok"])
        self.assertEqual(imported["actor_id"], "github-actions")
        with self.assertRaisesRegex(WorkflowError, "already been imported"):
            import_ci_result(
                self._project(),
                run_id=self.run_id,
                job_path=self.job,
                package_path=self.package,
                trust_policy_path=self._policy(),
                policy_name="ci",
                job_trust_policy_path=self._policy(),
                job_policy_name="job",
            )

    def test_attest_rejects_malformed_unsigned_result(self) -> None:
        execute_ci_job(
            job_path=self.job,
            checkout=self.checkout,
            output_path=self.unsigned,
            job_trust_policy_path=self._job_policy(),
            job_policy_name="job",
        )
        with zipfile.ZipFile(self.unsigned) as archive:
            entries = {
                info.filename: archive.read(info)
                for info in archive.infolist()
            }
        entries["ci/ci-result.json"] = b"{"
        with zipfile.ZipFile(
            self.unsigned, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        with self.assertRaisesRegex(WorkflowError, "result JSON is malformed"):
            attest_ci_result(
                job_path=self.job,
                unsigned_result_path=self.unsigned,
                output_path=self.package,
                private_key_path=self.private,
                actor_id="github-actions",
                job_trust_policy_path=self._job_policy(),
                job_policy_name="job",
            )

    def test_prepare_binds_clean_commit_contracts_and_integration_sources(self) -> None:
        source_one = self.root / "source-one.aria-evidence"
        source_two = self.root / "source-two.aria-evidence"
        source_one.write_bytes(b"one")
        source_two.write_bytes(b"two")
        output = self.root / "prepared-job.json"
        result = prepare_ci_job(
            self._prepare_project(),
            run_id=self.run_id,
            output_path=output,
            private_key_path=self.authorizer_private,
            actor_id="alice",
            integration_source_packages=[source_one, source_two],
        )
        job = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(result["ok"])
        self.assertEqual(job["source_commit"], self.commit)
        self.assertEqual(job["feature_contract_sha256"], "f" * 64)
        self.assertEqual(job["purpose"], "integration")
        self.assertEqual(len(job["source_evidence_sha256"]), 2)
        self.assertEqual(job["authorization"]["actor_id"], "alice")
        (self.checkout / "untracked.tmp").write_text("dirty", encoding="utf-8")
        try:
            with self.assertRaisesRegex(WorkflowError, "clean source checkout"):
                prepare_ci_job(
                    self._prepare_project(),
                    run_id=self.run_id,
                    output_path=self.root / "dirty-job.json",
                    private_key_path=self.authorizer_private,
                    actor_id="alice",
                )
        finally:
            (self.checkout / "untracked.tmp").unlink()

    def test_prepare_rejects_partial_required_assurance_coverage(self) -> None:
        project = self._prepare_project()
        manifest_path = (
            project.runtime_root / "runs" / self.run_id / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["assurance_plan"] = {
            "required_execution_classes": ["focused", "integration"]
        }
        atomic_write_json(manifest_path, manifest)
        with self.assertRaisesRegex(
            WorkflowError, "do not cover required execution classes"
        ):
            prepare_ci_job(
                project,
                run_id=self.run_id,
                output_path=self.root / "partial-job.json",
                private_key_path=self.authorizer_private,
                actor_id="alice",
                command_ids=["focused"],
            )

    def test_prepare_binds_clean_implementation_commit_after_run_baseline(self) -> None:
        project = self._prepare_project()
        (self.checkout / "implementation.txt").write_text(
            "implemented\n", encoding="utf-8"
        )
        self._git("add", "implementation.txt")
        self._git("commit", "-m", "implementation")
        implementation_head = self._git("rev-parse", "HEAD")
        output = self.root / "implementation-job.json"

        prepare_ci_job(
            project,
            run_id=self.run_id,
            output_path=output,
            private_key_path=self.authorizer_private,
            actor_id="alice",
        )

        job = json.loads(output.read_text(encoding="utf-8"))
        self.assertNotEqual(implementation_head, self.commit)
        self.assertEqual(job["source_commit"], implementation_head)

    def test_import_rejects_other_nonce_or_contract(self) -> None:
        execute_ci_job(
            job_path=self.job,
            checkout=self.checkout,
            output_path=self.unsigned,
            job_trust_policy_path=self._job_policy(),
            job_policy_name="job",
        )
        attest_ci_result(
            job_path=self.job,
            unsigned_result_path=self.unsigned,
            output_path=self.package,
            private_key_path=self.private,
            actor_id="github-actions",
            job_trust_policy_path=self._job_policy(),
            job_policy_name="job",
        )
        changed = {
            key: value
            for key, value in self.job_payload.items()
            if key != "authorization"
        }
        changed["nonce"] = "3" * 64
        changed = authorize_ci_job(
            changed,
            private_key_path=self.authorizer_private,
            actor_id="alice",
        )
        other_job = self.root / "other-job.json"
        atomic_write_json(other_job, changed)
        with self.assertRaisesRegex(
            WorkflowError, "does not match job, commit, contract or nonce"
        ):
            import_ci_result(
                self._project(),
                run_id=self.run_id,
                job_path=other_job,
                package_path=self.package,
                trust_policy_path=self._policy(),
                policy_name="ci",
                job_trust_policy_path=self._policy(),
                job_policy_name="job",
            )

    def test_command_that_writes_isolated_tree_fails_but_source_is_unchanged(self) -> None:
        changed = {
            key: value
            for key, value in self.job_payload.items()
            if key != "authorization"
        }
        changed["commands"] = [
            {
                "id": "mutating",
                "adapter": "python",
                "argv": [
                    "python",
                    "-c",
                    "from pathlib import Path; Path('generated.tmp').write_text('x'); print('changed')",
                ],
                "cwd": ".",
                "classes": ["adversarial"],
                "timeout_seconds": 30,
            }
        ]
        changed = authorize_ci_job(
            changed,
            private_key_path=self.authorizer_private,
            actor_id="alice",
        )
        atomic_write_json(self.job, changed)
        self.job_payload = changed
        result = execute_ci_job(
            job_path=self.job,
            checkout=self.checkout,
            output_path=self.unsigned,
            job_trust_policy_path=self._job_policy(),
            job_policy_name="job",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_command_ids"], ["mutating"])
        self.assertFalse((self.checkout / "generated.tmp").exists())
        self.assertEqual(
            self._git("status", "--porcelain=v1", "--untracked-files=all"), ""
        )

    def test_github_adapter_calls_generic_protocol_and_pins_major_actions(self) -> None:
        workflow = github_workflow(project_id="sample")
        parsed = yaml.safe_load(workflow)
        self.assertEqual(set(parsed["jobs"]), {"execute", "attest"})
        self.assertIn("aria ci execute", workflow)
        self.assertIn("aria ci attest", workflow)
        self.assertIn("needs: execute", workflow)
        self.assertIn("environment: aria-signing", workflow)
        self.assertIn("ARIA_WHEELHOUSE_SHA256", workflow)
        self.assertIn("pip install --no-index", workflow)
        self.assertNotIn("python -m pip install .", workflow)
        execute_job, attest_job = workflow.split("\n  attest:", maxsplit=1)
        self.assertNotIn("ARIA_SIGNING_KEY_PEM", execute_job)
        self.assertIn("ARIA_SIGNING_KEY_PEM", attest_job)
        self.assertIn(
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            workflow,
        )
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            workflow,
        )
        self.assertIn("permissions:\n  contents: read", workflow)


if __name__ == "__main__":
    unittest.main()
