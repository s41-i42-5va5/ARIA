from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aria.errors import ConfigurationError
from aria.identity import (
    enroll_identity,
    identity_status,
    load_identity,
    sign_with_identity,
    verify_enrollment_request,
)
from aria.signing import verify_bytes


class DeviceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / "runtime"
        self.request = Path(self.temporary.name) / "enrollment.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_enrollment_protects_private_key_and_proves_public_request(self) -> None:
        result = enroll_identity(
            self.runtime,
            actor_id="alice",
            device_id="alice-laptop",
            display_name="Alice",
            email="alice@example.test",
            request_path=self.request,
        )
        identity_path = Path(str(result["identity_path"]))
        stored_text = identity_path.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE KEY", stored_text)
        self.assertNotIn("protected_private_key", json.dumps(result))

        request = verify_enrollment_request(
            json.loads(self.request.read_text(encoding="utf-8"))
        )
        self.assertEqual(request["key_id"], result["key_id"])
        identity = load_identity(
            self.runtime, actor_id="alice", device_id="alice-laptop"
        )
        signature = sign_with_identity(identity, b"backlog-event")
        self.assertEqual(
            verify_bytes(
                b"backlog-event",
                public_key_b64=signature["public_key"],
                signature_b64=signature["signature"],
                key_id=signature["key_id"],
            ),
            result["key_id"],
        )

        status = identity_status(self.runtime)
        self.assertEqual(status["count"], 1)
        self.assertNotIn("protected_private_key", status["identities"][0])

    def test_enrollment_refuses_overwrite_and_unverified_email_is_explicit(self) -> None:
        first = enroll_identity(
            self.runtime,
            actor_id="alice",
            device_id="workstation",
            email="alice@example.test",
        )
        self.assertFalse(first["enrollment_request"]["email_verified"])
        with self.assertRaisesRegex(ConfigurationError, "Refusing to overwrite"):
            enroll_identity(
                self.runtime,
                actor_id="alice",
                device_id="workstation",
            )


if __name__ == "__main__":
    unittest.main()
