from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from aria.access import (
    access_status,
    access_template,
    authorize_access,
    bootstrap_access,
    grant_access,
    load_access_policy,
    revoke_access,
    verify_access_audit,
)
from aria.errors import WorkflowError
from aria.identity import enroll_identity


class AccessPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.docs = root / "docs"
        self.runtime = root / "runtime"
        self.docs.mkdir()
        self.registry = self.runtime / "projects.toml"
        self.registry.parent.mkdir()
        (self.docs / "ARIA_TEAM.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "actors": [
                        {
                            "id": "owner",
                            "display_name": "Owner",
                            "type": "human",
                            "roles": ["maintainer", "release-manager"],
                        },
                        {
                            "id": "bob",
                            "display_name": "Bob",
                            "type": "human",
                            "roles": ["contributor"],
                        },
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (self.docs / "TRUST.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "keys": [],
                    "policies": {},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (self.docs / "ACCESS.yaml").write_bytes(access_template("demo"))
        self.project = SimpleNamespace(
            project_id="demo",
            docs_root=self.docs,
            runtime_root=self.runtime / "projects" / "demo",
            registry_path=self.registry,
            files=SimpleNamespace(
                team="ARIA_TEAM.yaml",
                trust="TRUST.yaml",
                access="ACCESS.yaml",
            ),
        )
        self.owner_request = root / "owner.json"
        self.bob_request = root / "bob.json"
        enroll_identity(
            self.runtime,
            actor_id="owner",
            device_id="owner-pc",
            request_path=self.owner_request,
        )
        enroll_identity(
            self.runtime,
            actor_id="bob",
            device_id="bob-pc",
            request_path=self.bob_request,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_bootstrap_grant_version_scope_revoke_and_audit(self) -> None:
        bootstrapped = bootstrap_access(
            self.project, actor_id="owner", device_id="owner-pc"
        )
        self.assertEqual(bootstrapped["revision"], 1)
        granted = grant_access(
            self.project,
            request_path=self.bob_request,
            permissions=["project.read", "backlog.read", "backlog.write"],
            versions=["1.*"],
            branches=["feature/*"],
            expected_revision=1,
            admin_actor_id="owner",
            admin_device_id="owner-pc",
        )
        self.assertEqual(granted["revision"], 2)
        allowed = authorize_access(
            self.project,
            permission="backlog.write",
            actor_id="bob",
            device_id="bob-pc",
            version="1.5",
            branch="feature/backlog",
        )
        self.assertEqual(allowed["actor_id"], "bob")
        with self.assertRaisesRegex(WorkflowError, "not allowed"):
            authorize_access(
                self.project,
                permission="backlog.write",
                actor_id="bob",
                device_id="bob-pc",
                version="2.0",
                branch="feature/backlog",
            )
        revoked = revoke_access(
            self.project,
            actor_id="bob",
            device_id="bob-pc",
            expected_revision=2,
            admin_actor_id="owner",
            admin_device_id="owner-pc",
        )
        self.assertEqual(revoked["revision"], 3)
        with self.assertRaisesRegex(WorkflowError, "active project device"):
            authorize_access(
                self.project,
                permission="backlog.read",
                actor_id="bob",
                device_id="bob-pc",
                version="1.5",
                branch="feature/backlog",
            )
        self.assertEqual(verify_access_audit(self.project)["events"], 3)
        self.assertEqual(access_status(self.project)["revision"], 3)

    def test_tamper_and_stale_revision_fail_closed(self) -> None:
        bootstrap_access(self.project, actor_id="owner", device_id="owner-pc")
        with self.assertRaisesRegex(WorkflowError, "Stale access revision"):
            grant_access(
                self.project,
                request_path=self.bob_request,
                permissions=["backlog.read"],
                versions=["*"],
                branches=["*"],
                expected_revision=0,
                admin_actor_id="owner",
                admin_device_id="owner-pc",
            )
        path = self.docs / "ACCESS.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["grants"][0]["versions"] = ["2.*"]
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "signature is invalid"):
            load_access_policy(self.project)

    def test_enrollment_request_tamper_is_rejected(self) -> None:
        bootstrap_access(self.project, actor_id="owner", device_id="owner-pc")
        request = json.loads(self.bob_request.read_text(encoding="utf-8"))
        request["actor_id"] = "owner"
        self.bob_request.write_text(json.dumps(request), encoding="utf-8")
        with self.assertRaisesRegex(WorkflowError, "signature is invalid"):
            grant_access(
                self.project,
                request_path=self.bob_request,
                permissions=["backlog.read"],
                versions=["*"],
                branches=["*"],
                expected_revision=1,
                admin_actor_id="owner",
                admin_device_id="owner-pc",
            )

    def test_grant_rolls_back_policy_when_audit_write_crashes(self) -> None:
        bootstrap_access(self.project, actor_id="owner", device_id="owner-pc")
        access_before = (self.docs / "ACCESS.yaml").read_bytes()
        history_before = (self.docs / "ACCESS_HISTORY.jsonl").read_bytes()
        with patch("aria.access._append_event", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                grant_access(
                    self.project,
                    request_path=self.bob_request,
                    permissions=["backlog.read"],
                    versions=["*"],
                    branches=["*"],
                    expected_revision=1,
                    admin_actor_id="owner",
                    admin_device_id="owner-pc",
                )
        self.assertEqual((self.docs / "ACCESS.yaml").read_bytes(), access_before)
        self.assertEqual(
            (self.docs / "ACCESS_HISTORY.jsonl").read_bytes(), history_before
        )
        self.assertEqual(verify_access_audit(self.project)["events"], 1)

    def test_one_actor_can_use_two_independently_enrolled_devices(self) -> None:
        bootstrap_access(self.project, actor_id="owner", device_id="owner-pc")
        grant_access(
            self.project,
            request_path=self.bob_request,
            permissions=["project.read", "backlog.read"],
            versions=["1.*"],
            branches=["feature/*"],
            expected_revision=1,
            admin_actor_id="owner",
            admin_device_id="owner-pc",
        )
        second_request = self.bob_request.with_name("bob-laptop.json")
        enroll_identity(
            self.runtime,
            actor_id="bob",
            device_id="bob-laptop",
            request_path=second_request,
        )
        grant_access(
            self.project,
            request_path=second_request,
            permissions=["project.read", "backlog.read"],
            versions=["1.*"],
            branches=["feature/*"],
            expected_revision=2,
            admin_actor_id="owner",
            admin_device_id="owner-pc",
        )

        for device_id in ("bob-pc", "bob-laptop"):
            allowed = authorize_access(
                self.project,
                permission="backlog.read",
                actor_id="bob",
                device_id=device_id,
                version="1.5",
                branch="feature/backlog",
            )
            self.assertEqual(allowed["device_id"], device_id)


if __name__ == "__main__":
    unittest.main()
