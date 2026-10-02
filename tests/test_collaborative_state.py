from __future__ import annotations

import copy
import unittest

from aria.collaboration import build_control_contract
from aria.collaborative_backlog import ProviderIdentity
from aria.collaborative_state import (
    apply_state_acceptance,
    collaborative_state_template,
    dump_collaborative_state,
    validate_collaborative_state,
)
from aria.errors import ConfigurationError, WorkflowError
from aria.github_integration import (
    GitHubIntegrationAcceptance,
    RequiredGitHubCheck,
)
from aria.provider import ProviderActor


class CollaborativeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = build_control_contract(
            project_id="demo",
            provider="github",
            repository_id="123456789",
        )
        self.actor = ProviderActor("200", "yura", "Yura")
        self.coordinator = ProviderIdentity(
            "github-app", ProviderActor("9001", "aria-coordinator", "ARIA Coordinator")
        )
        self.acceptance = GitHubIntegrationAcceptance(
            repository_id="123456789",
            pull_request_number=17,
            integration_branch="dev",
            merge_commit="a" * 40,
            merged_at="2026-08-26T12:00:00Z",
            pull_request_author=self.actor,
            required_checks=(RequiredGitHubCheck("ARIA integration", 9001),),
            backlog_item_id="BLG-000001",
            source_branch="work/yura",
            changed_paths=("src/export/writer.py",),
        )
        self.item = {
            "id": "BLG-000001",
            "title": "Add shared backlog",
            "status": "done",
            "assignee": ProviderIdentity("github", self.actor).as_mapping(),
            "evidence_refs": sorted(
                [
                    "github-pr:17",
                    f"git-commit:{'a' * 40}",
                    "github-check:ARIA-integration",
                ]
            ),
        }

    def apply(self):
        return apply_state_acceptance(
            collaborative_state_template(self.contract),
            contract=self.contract,
            backlog_item=self.item,
            backlog_revision=2,
            acceptance=self.acceptance,
            coordinator=self.coordinator,
            event_id="state-pr-17-aaaaaaaa",
            expected_revision=0,
        )

    def test_acceptance_creates_exact_checkpoint_and_round_trip(self) -> None:
        result = self.apply()
        self.assertTrue(result["applied"])
        state = result["state"]
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["accepted_head"], "a" * 40)
        self.assertEqual(state["backlog_revision"], 2)
        self.assertEqual(state["components"][0]["id"], "BLG-000001")
        self.assertEqual(state["events"][0]["requested_by"]["user_id"], "200")
        content = dump_collaborative_state(state, self.contract)
        self.assertEqual(validate_collaborative_state(__import__("yaml").safe_load(content), self.contract), state)

    def test_duplicate_is_idempotent_but_changed_content_is_rejected(self) -> None:
        state = self.apply()["state"]
        duplicate = apply_state_acceptance(
            state,
            contract=self.contract,
            backlog_item=self.item,
            backlog_revision=2,
            acceptance=self.acceptance,
            coordinator=self.coordinator,
            event_id="state-pr-17-aaaaaaaa",
            expected_revision=1,
        )
        self.assertFalse(duplicate["applied"])
        changed = copy.deepcopy(self.item)
        changed["evidence_refs"].append("github-check:changed")
        with self.assertRaisesRegex(WorkflowError, "reused with different content"):
            apply_state_acceptance(
                state,
                contract=self.contract,
                backlog_item=changed,
                backlog_revision=2,
                acceptance=self.acceptance,
                coordinator=self.coordinator,
                event_id="state-pr-17-aaaaaaaa",
                expected_revision=1,
            )

    def test_unassigned_or_failed_backlog_item_is_not_published(self) -> None:
        item = copy.deepcopy(self.item)
        item["status"] = "in_progress"
        with self.assertRaisesRegex(WorkflowError, "not eligible"):
            apply_state_acceptance(
                collaborative_state_template(self.contract),
                contract=self.contract,
                backlog_item=item,
                backlog_revision=1,
                acceptance=self.acceptance,
                coordinator=self.coordinator,
                event_id="state-pr-17-aaaaaaaa",
                expected_revision=0,
            )

    def test_event_or_component_tamper_breaks_validation(self) -> None:
        state = self.apply()["state"]
        tampered = copy.deepcopy(state)
        tampered["components"][0]["title"] = "Changed"
        with self.assertRaisesRegex(ConfigurationError, "latest checkpoint linkage"):
            validate_collaborative_state(tampered, self.contract)
        tampered = copy.deepcopy(state)
        tampered["events"][0]["merge_commit"] = "b" * 40
        with self.assertRaisesRegex(ConfigurationError, "hash is invalid"):
            validate_collaborative_state(tampered, self.contract)


if __name__ == "__main__":
    unittest.main()
