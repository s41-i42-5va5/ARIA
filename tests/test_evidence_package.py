from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from aria.errors import ConfigurationError, WorkflowError
from aria.evidence_package import (
    build_signed_package,
    export_run_package,
    inspect_evidence,
    inspect_package,
    verify_evidence,
    verify_package,
)
from aria.signing import generate_keypair


class EvidencePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = self.root / "signing-private.pem"
        self.public = self.root / "signing-public.pem"
        self.identity = generate_keypair(self.private, self.public)
        self.package = self.root / "evidence.aria-evidence"
        self.result = build_signed_package(
            files=[
                ("evidence/manifest.json", b'{"run_id":"run-1"}\n'),
                ("evidence/execution/output.log", b"114 tests OK\n"),
            ],
            output_path=self.package,
            private_key_path=self.private,
            subject={
                "kind": "run",
                "project_id": "sample",
                "run_id": "run-1",
                "source_commit": "a" * 40,
            },
            trust_level="ci-signed",
            actor_id="ci-release",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _signature(self) -> dict[str, object]:
        with zipfile.ZipFile(self.package) as archive:
            return json.loads(archive.read("signature.json"))

    def _policy(
        self,
        *,
        status: str = "trusted",
        expires_at: str | None = None,
        trusted_key: bool = True,
        actor_id: str = "ci-release",
    ) -> Path:
        signature = self._signature()
        key_id = str(signature["key_id"])
        key: dict[str, object] = {
            "id": key_id,
            "public_key": signature["public_key"],
            "actor_id": actor_id,
            "status": status,
        }
        if expires_at is not None:
            key["expires_at"] = expires_at
        payload = {
            "schema_version": 1,
            "keys": [key],
            "policies": {
                "release": {
                    "minimum_trust_level": "ci-signed",
                    "trusted_keys": [key_id] if trusted_key else [],
                }
            },
        }
        path = self.root / f"trust-{status}-{trusted_key}.yaml"
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )
        return path

    def _mutate_entry(self, name: str, replacement: bytes | None = None) -> Path:
        tampered = self.root / f"tampered-{name.replace('/', '-')}.zip"
        with zipfile.ZipFile(self.package, "r") as source:
            rows = [(info.filename, source.read(info)) for info in source.infolist()]
        with zipfile.ZipFile(tampered, "w") as target:
            for entry, content in rows:
                if entry == name:
                    content = replacement if replacement is not None else content + b"x"
                target.writestr(entry, content)
        return tampered

    def test_offline_signature_and_policy_verification(self) -> None:
        result = verify_package(
            self.package,
            trust_policy_path=self._policy(),
            policy_name="release",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["signature_valid"])
        self.assertTrue(result["trust"]["trusted"])
        self.assertEqual(result["trust"]["trust_level"], "ci-signed")
        self.assertEqual(result["actor_id"], "ci-release")
        self.assertEqual(result["files"], 2)

    def test_export_uses_real_run_manifest_schema(self) -> None:
        run_id = "20260728T080000Z-1234abcd"
        runtime = self.root / "runtime"
        run_root = runtime / "runs" / run_id
        bundle = run_root / "execution" / "bundle.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "ok": True,
                    "git": {
                        "head": "b" * 40,
                        "working_tree_sha256": "c" * 64,
                        "changed_count": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
        bundle_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
        (run_root / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "project": "sample",
                    "status": "completed",
                    "context": {"git": {"head": "a" * 40}},
                    "feature_contract_lock": {"sha256": "f" * 64},
                    "execution_contract": {"sha256": "e" * 64},
                    "execution_bundle": {
                        "path": "execution/bundle.json",
                        "sha256": bundle_sha,
                    },
                }
            ),
            encoding="utf-8",
        )
        output = self.root / "exported.aria-evidence"
        export_run_package(
            project=SimpleNamespace(runtime_root=runtime),
            run_id=run_id,
            output_path=output,
            private_key_path=self.private,
            actor_id="ci-release",
        )
        subject = verify_package(output)["subject"]
        self.assertEqual(subject["project_id"], "sample")
        self.assertEqual(subject["feature_contract_sha256"], "f" * 64)
        self.assertEqual(subject["source_commit"], "b" * 40)

    def test_inspection_does_not_claim_signature_verification(self) -> None:
        result = inspect_package(self.package)
        self.assertTrue(result["ok"])
        self.assertFalse(result["signature_checked"])
        self.assertEqual(result["subject"]["run_id"], "run-1")

    def test_manifest_signature_and_evidence_tampering_fail_closed(self) -> None:
        with zipfile.ZipFile(self.package) as archive:
            manifest = archive.read("package.json").replace(b"run-1", b"run-2")
        cases = (
            ("package.json", "digest mismatch", manifest),
            ("signature.json", "not valid UTF-8 JSON"),
            ("evidence/manifest.json", "integrity failed"),
            ("evidence/execution/output.log", "integrity failed"),
        )
        for case in cases:
            name, message, *replacement = case
            with self.subTest(name=name), self.assertRaisesRegex(
                WorkflowError, message
            ):
                verify_package(
                    self._mutate_entry(
                        name, replacement[0] if replacement else None
                    )
                )

    def test_unknown_revoked_and_expired_keys_fail_policy(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "not allowed by policy"):
            verify_package(
                self.package,
                trust_policy_path=self._policy(trusted_key=False),
                policy_name="release",
            )

    def test_actor_substitution_and_future_signed_time_fail_policy(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "trusted key owner"):
            verify_package(
                self.package,
                trust_policy_path=self._policy(actor_id="another-actor"),
                policy_name="release",
            )
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat().replace(
            "+00:00", "Z"
        )
        future_package = self.root / "future.aria-evidence"
        with patch("aria.evidence_package._stamp", return_value=future):
            build_signed_package(
                files=[("evidence/result.json", b"{}\n")],
                output_path=future_package,
                private_key_path=self.private,
                subject={"kind": "run", "run_id": "future"},
                trust_level="ci-signed",
                actor_id="ci-release",
            )
        with self.assertRaisesRegex(WorkflowError, "timestamp is in the future"):
            verify_package(
                future_package,
                trust_policy_path=self._policy(),
                policy_name="release",
            )
        with self.assertRaisesRegex(WorkflowError, "revoked"):
            verify_package(
                self.package,
                trust_policy_path=self._policy(status="revoked"),
                policy_name="release",
            )
        expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(WorkflowError, "expired"):
            verify_package(
                self.package,
                trust_policy_path=self._policy(expires_at=expired),
                policy_name="release",
            )

    def test_key_identity_mismatch_and_overwrite_are_rejected(self) -> None:
        signature = self._signature()
        public_raw = base64.b64decode(str(signature["public_key"]))
        payload = {
            "schema_version": 1,
            "keys": [
                {
                    "id": "ed25519:" + "0" * 32,
                    "public_key": base64.b64encode(public_raw).decode("ascii"),
                    "actor_id": "ci-release",
                    "status": "trusted",
                }
            ],
            "policies": {
                "default": {
                    "minimum_trust_level": "signed",
                    "trusted_keys": [],
                }
            },
        }
        policy = self.root / "bad-policy.yaml"
        policy.write_text(yaml.safe_dump(payload), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "identity mismatch"):
            verify_package(self.package, trust_policy_path=policy)
        with self.assertRaisesRegex(ConfigurationError, "Refusing to overwrite"):
            generate_keypair(self.private, self.root / "another-public.pem")

    def test_policy_rejects_unknown_fields_and_missing_assurance(self) -> None:
        policy = self._policy()
        payload = yaml.safe_load(policy.read_text(encoding="utf-8"))
        payload["policies"]["release"]["unexpected"] = True
        policy.write_text(yaml.safe_dump(payload), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "unknown fields"):
            verify_package(
                self.package,
                trust_policy_path=policy,
                policy_name="release",
            )
        payload["policies"]["release"].pop("unexpected")
        payload["policies"]["release"]["required_assurance_classes"] = [
            "integration"
        ]
        policy.write_text(yaml.safe_dump(payload), encoding="utf-8")
        with self.assertRaisesRegex(
            WorkflowError, "lacks required assurance classes"
        ):
            verify_package(
                self.package,
                trust_policy_path=policy,
                policy_name="release",
            )

    def test_contextual_policy_fails_closed_without_roles_and_approvals(self) -> None:
        policy = self._policy()
        payload = yaml.safe_load(policy.read_text(encoding="utf-8"))
        payload["policies"]["release"]["allowed_actor_roles"] = [
            "release-manager"
        ]
        payload["policies"]["release"]["required_approvals"] = 2
        policy.write_text(yaml.safe_dump(payload), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "requires actor role context"):
            verify_package(
                self.package,
                trust_policy_path=policy,
                policy_name="release",
            )
        with self.assertRaisesRegex(WorkflowError, "requires approval context"):
            verify_package(
                self.package,
                trust_policy_path=policy,
                policy_name="release",
                actor_roles=["release-manager"],
            )
        verified = verify_package(
            self.package,
            trust_policy_path=policy,
            policy_name="release",
            actor_roles=["release-manager"],
            approval_count=2,
        )
        self.assertTrue(verified["trust"]["trusted"])

    def test_v1_bundle_remains_readable_as_local_but_not_ci_signed(self) -> None:
        legacy = self.root / "bundle-v1.json"
        legacy.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "legacy-run",
                    "git": {"head": "a" * 40},
                    "executions": [],
                    "ok": True,
                }
            ),
            encoding="utf-8",
        )
        inspected = inspect_evidence(legacy)
        self.assertTrue(inspected["legacy"])
        self.assertEqual(inspected["trust_level"], "local")
        verified = verify_evidence(legacy)
        self.assertFalse(verified["signature_valid"])
        self.assertEqual(verified["trust"]["trust_level"], "local")
        with self.assertRaisesRegex(WorkflowError, "local-only"):
            verify_evidence(
                legacy,
                trust_policy_path=self._policy(),
                policy_name="release",
            )

    def test_compressed_package_limit_is_enforced_before_zip_parsing(self) -> None:
        oversized = self.root / "oversized.aria-evidence"
        oversized.write_bytes(b"x" * 9)
        with (
            patch("aria.evidence_package.MAX_PACKAGE_BYTES", 8),
            self.assertRaisesRegex(WorkflowError, "exceeds size limit"),
        ):
            verify_package(oversized)


if __name__ == "__main__":
    unittest.main()
