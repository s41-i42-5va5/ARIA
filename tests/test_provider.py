from __future__ import annotations

import unittest

from aria.errors import ConfigurationError
from aria.provider import (
    ProviderActor,
    ProviderBranchProtection,
    ProviderInspection,
    ProviderMembership,
    validate_provider_inspection,
)


class ProviderContractTests(unittest.TestCase):
    def test_immutable_identity_is_distinct_from_username_snapshot(self) -> None:
        before = ProviderActor(
            user_id="provider-user-42",
            username_snapshot="yura",
            display_name_snapshot="Yura",
        )
        after = ProviderActor(
            user_id="provider-user-42",
            username_snapshot="yura-renamed",
            display_name_snapshot="Yura",
        )
        self.assertEqual(before.user_id, after.user_id)
        self.assertNotEqual(before.username_snapshot, after.username_snapshot)

    def test_membership_and_protection_fail_closed(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "requires a role"):
            ProviderMembership(active=True, roles=())
        with self.assertRaisesRegex(ConfigurationError, "unique and sorted"):
            ProviderMembership(active=True, roles=("owner", "developer"))

        weak = ProviderBranchProtection(
            protected=True,
            direct_user_push=True,
            canonical_writer="aria-coordinator",
        )
        self.assertFalse(weak.coordinator_only)
        strong = ProviderBranchProtection(
            protected=True,
            direct_user_push=False,
            canonical_writer="aria-coordinator",
        )
        self.assertTrue(strong.coordinator_only)

    def test_readback_must_match_immutable_repository_identity(self) -> None:
        inspection = ProviderInspection(
            provider="github",
            repository_id="repo-42",
            actor=ProviderActor(
                user_id="user-7",
                username_snapshot="aram",
            ),
            membership=ProviderMembership(active=True, roles=("owner",)),
            protection=ProviderBranchProtection(
                protected=True,
                direct_user_push=False,
                canonical_writer="aria-coordinator",
            ),
        )
        validate_provider_inspection(
            inspection,
            expected_provider="github",
            expected_repository_id="repo-42",
        )
        with self.assertRaisesRegex(ConfigurationError, "repository_id"):
            validate_provider_inspection(
                inspection,
                expected_provider="github",
                expected_repository_id="other-repository",
            )


if __name__ == "__main__":
    unittest.main()
