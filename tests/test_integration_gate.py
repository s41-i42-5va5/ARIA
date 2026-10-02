from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

import yaml

from aria.errors import WorkflowError
from aria.evidence_package import (
    build_signed_package,
    create_review_attestation,
)
from aria.integration_gate import run_integration_gate
from aria.project import canonical_sha
from aria.signing import generate_keypair


class IntegrationGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self._git("init")
        self._git("config", "user.email", "aria@example.invalid")
        self._git("config", "user.name", "ARIA Test")
        self.private = self.root / "private.pem"
        self.public = self.root / "public.pem"
        generate_keypair(self.private, self.public)
        self.ci_private = self.root / "ci-private.pem"
        self.ci_public = self.root / "ci-public.pem"
        generate_keypair(self.ci_private, self.ci_public)
        self.review_private = self.root / "review-private.pem"
        self.review_public = self.root / "review-public.pem"
        generate_keypair(self.review_private, self.review_public)
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "ARIA_TEAM.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "actors": [
                        {
                            "id": "alice",
                            "type": "human",
                            "roles": ["contributor", "reviewer"],
                        },
                        {
                            "id": "bob",
                            "type": "human",
                            "roles": ["reviewer"],
                        },
                        {
                            "id": "ci-release",
                            "type": "service",
                            "roles": ["ci", "release-manager"],
                        },
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        self.project = SimpleNamespace(
            project_id="sample",
            docs_root=docs,
            code_root=self.checkout,
            git_ignore_prefixes=(),
        )
        self.source_paths: list[Path] = []
        self.source_hashes: list[str] = []
        for index in (1, 2):
            tracked = self.checkout / f"source-{index}.txt"
            tracked.write_text(f"source {index}\n", encoding="utf-8")
            self._git("add", tracked.name)
            self._git("commit", "-m", f"source {index}")
            commit = self._git("rev-parse", "HEAD")
            path = self.root / f"source-{index}.aria-evidence"
            build_signed_package(
                files=[(f"evidence/source-{index}.json", b"{}\n")],
                output_path=path,
                private_key_path=self.private,
                subject={
                    "kind": "run",
                    "project_id": "sample",
                    "run_id": f"source-{index}",
                    "run_status": "completed",
                    "source_commit": commit,
                    "source_changed_count": 0,
                    "source_working_tree_sha256": canonical_sha({}),
                },
                trust_level="signed",
                actor_id="alice",
            )
            self.source_paths.append(path)
            self.source_hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        self.target = self._git("rev-parse", "HEAD")
        self.integration = self._integration_package(
            target=self.target,
            source_hashes=self.source_hashes,
        )
        self.review = self._review_package(
            reviewer_id="bob",
            private_key=self.review_private,
            output=self.root / "review.aria-evidence",
        )
        source_signature = self._signature(self.source_paths[0])
        integration_signature = self._signature(self.integration)
        review_signature = self._signature(self.review)
        self.policy = self.root / "TRUST.yaml"
        self.policy.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "keys": [
                        {
                            "id": source_signature["key_id"],
                            "public_key": source_signature["public_key"],
                            "actor_id": "alice",
                            "status": "trusted",
                        },
                        {
                            "id": integration_signature["key_id"],
                            "public_key": integration_signature["public_key"],
                            "actor_id": "ci-release",
                            "status": "trusted",
                        },
                        {
                            "id": review_signature["key_id"],
                            "public_key": review_signature["public_key"],
                            "actor_id": "bob",
                            "status": "trusted",
                        },
                    ],
                    "policies": {
                        "contributor": {
                            "minimum_trust_level": "signed",
                            "trusted_keys": [source_signature["key_id"]],
                            "allowed_actor_roles": ["contributor"],
                        },
                        "release": {
                            "minimum_trust_level": "ci-signed",
                            "trusted_keys": [integration_signature["key_id"]],
                            "allowed_actor_roles": ["ci", "release-manager"],
                            "required_approvals": 1,
                        },
                        "reviewer": {
                            "minimum_trust_level": "signed",
                            "trusted_keys": [review_signature["key_id"]],
                            "allowed_actor_roles": ["reviewer"],
                        },
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

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

    @staticmethod
    def _signature(path: Path) -> dict[str, object]:
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read("signature.json"))

    def _integration_package(
        self,
        *,
        target: str,
        source_hashes: list[str],
        purpose: str = "integration",
    ) -> Path:
        path = self.root / (
            f"integration-{len(list(self.root.glob('integration-*')))}.aria-evidence"
        )
        job_id = "job-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:32]
        job_bytes = json.dumps(
            {"schema_version": 1, "job_id": job_id},
            sort_keys=True,
        ).encode("utf-8")
        result = {
            "job_id": job_id,
            "nonce": "n" * 64,
            "project_id": "sample",
            "run_id": "integration-run",
            "source_commit": target,
            "feature_contract_sha256": "f" * 64,
            "execution_contract_sha256": "e" * 64,
            "purpose": purpose,
            "source_evidence_sha256": source_hashes,
            "required_execution_classes": [],
            "passed_execution_classes": [],
            "missing_execution_classes": [],
            "ok": True,
        }
        result_bytes = json.dumps(result, sort_keys=True).encode("utf-8")
        build_signed_package(
            files=[
                ("ci/ci-job.json", job_bytes),
                ("ci/ci-result.json", result_bytes),
            ],
            output_path=path,
            private_key_path=self.ci_private,
            subject={
                "kind": "ci-result",
                "job_id": job_id,
                "job_sha256": hashlib.sha256(job_bytes).hexdigest(),
                "nonce": result["nonce"],
                "project_id": "sample",
                "run_id": result["run_id"],
                "result_ok": True,
                "purpose": purpose,
                "source_commit": target,
                "feature_contract_sha256": result["feature_contract_sha256"],
                "execution_contract_sha256": result[
                    "execution_contract_sha256"
                ],
                "source_evidence_sha256": source_hashes,
                "required_execution_classes": [],
                "passed_execution_classes": [],
                "missing_execution_classes": [],
            },
            trust_level="ci-signed",
            actor_id="ci-release",
        )
        return path

    def _review_package(
        self,
        *,
        reviewer_id: str,
        private_key: Path,
        output: Path,
        integration: Path | None = None,
    ) -> Path:
        create_review_attestation(
            output_path=output,
            private_key_path=private_key,
            reviewer_id=reviewer_id,
            project_id="sample",
            target_commit=self.target,
            source_package_paths=self.source_paths,
            integration_package_path=integration or self.integration,
        )
        return output

    def _gate(
        self,
        integration: Path | None = None,
        review: Path | None = None,
        output: str = "verdict.json",
    ) -> dict[str, object]:
        return run_integration_gate(
            self.project,
            source_packages=self.source_paths,
            integration_package=integration or self.integration,
            target_commit=self.target,
            trust_policy_path=self.policy,
            source_policy="contributor",
            integration_policy="release",
            review_package=review or self.review,
            review_policy="reviewer",
            output_path=self.root / output,
        )

    def test_gate_requires_exact_sources_fresh_commit_and_signed_review(self) -> None:
        result = self._gate()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["source_packages"]), 2)
        self.assertTrue(result["independent_review"]["independent"])
        self.assertEqual(result["independent_review"]["reviewer_id"], "bob")

    def test_normal_pass_does_not_replace_integration_run(self) -> None:
        normal = self._integration_package(
            target=self.target,
            source_hashes=self.source_hashes,
            purpose="verification",
        )
        with self.assertRaisesRegex(
            WorkflowError, "normal run cannot replace an integration"
        ):
            self._gate(normal, output="normal.json")

    def test_stale_commit_and_incomplete_source_binding_fail(self) -> None:
        stale = self._integration_package(
            target="b" * 40, source_hashes=self.source_hashes
        )
        with self.assertRaisesRegex(WorkflowError, "stale"):
            self._gate(stale, output="stale.json")
        incomplete = self._integration_package(
            target=self.target, source_hashes=self.source_hashes[:1]
        )
        with self.assertRaisesRegex(WorkflowError, "exact source package set"):
            self._gate(incomplete, output="incomplete.json")

    def test_contributor_cannot_self_approve(self) -> None:
        self_review = self._review_package(
            reviewer_id="alice",
            private_key=self.private,
            output=self.root / "self-review.aria-evidence",
        )
        signature = self._signature(self_review)
        raw = yaml.safe_load(self.policy.read_text(encoding="utf-8"))
        raw["policies"]["reviewer"]["trusted_keys"].append(signature["key_id"])
        self.policy.write_text(
            yaml.safe_dump(raw, sort_keys=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(WorkflowError, "Reviewer cannot approve"):
            self._gate(review=self_review, output="self.json")

    def test_source_commit_must_be_ancestor_of_target(self) -> None:
        self._git("checkout", "--orphan", "side")
        for path in self.checkout.iterdir():
            if path.is_file():
                path.unlink()
        (self.checkout / "side.txt").write_text("side\n", encoding="utf-8")
        self._git("add", "side.txt")
        self._git("commit", "-m", "side")
        side_commit = self._git("rev-parse", "HEAD")
        self._git("checkout", "master")
        self._git("restore", ".")
        side_path = self.checkout / "side.txt"
        if side_path.exists():
            side_path.unlink()
        self.assertEqual(self._git("rev-parse", "HEAD"), self.target)
        self.assertEqual(self._git("status", "--porcelain=v1"), "")
        replacement = self.root / "side-source.aria-evidence"
        build_signed_package(
            files=[("evidence/side.json", b"{}\n")],
            output_path=replacement,
            private_key_path=self.private,
            subject={
                "kind": "run",
                "project_id": "sample",
                "run_id": "side",
                "run_status": "completed",
                "source_commit": side_commit,
                "source_changed_count": 0,
                "source_working_tree_sha256": canonical_sha({}),
            },
            trust_level="signed",
            actor_id="alice",
        )
        self.source_paths[0] = replacement
        with self.assertRaisesRegex(WorkflowError, "not contained in target"):
            self._gate(output="ancestry.json")

    def test_dirty_completed_run_cannot_enter_integration_gate(self) -> None:
        dirty = self.root / "dirty-source.aria-evidence"
        build_signed_package(
            files=[("evidence/dirty.json", b"{}\n")],
            output_path=dirty,
            private_key_path=self.private,
            subject={
                "kind": "run",
                "project_id": "sample",
                "run_id": "dirty-source",
                "run_status": "completed",
                "source_commit": self.target,
                "source_changed_count": 1,
                "source_working_tree_sha256": canonical_sha(
                    {"uncommitted.py": "a" * 64}
                ),
            },
            trust_level="signed",
            actor_id="alice",
        )
        self.source_paths[0] = dirty
        with self.assertRaisesRegex(WorkflowError, "clean committed Git tree"):
            self._gate(output="dirty.json")


if __name__ == "__main__":
    unittest.main()
