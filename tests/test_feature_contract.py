from __future__ import annotations

import copy
import unittest

from aria.errors import WorkflowError
from aria.feature_contract import (
    canonical_sha,
    feature_contract_policy,
    validate_convergence,
    validate_feature_contract,
)


class FeatureContractTests(unittest.TestCase):
    def contract(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "ready",
            "outcome": "Duplicate delivery creates one durable operation",
            "ambiguities_resolved": True,
            "requirements": [
                {"id": "R-001", "statement": "Delivery is idempotent"},
                {"id": "R-002", "statement": "Parallel delivery is race safe"},
            ],
            "acceptance": [
                {
                    "id": "AC-001",
                    "requirement_ids": ["R-001"],
                    "oracle": "One operation exists after duplicate delivery",
                },
                {
                    "id": "AC-002",
                    "requirement_ids": ["R-002"],
                    "oracle": "All callers observe the same operation id",
                },
            ],
            "clarifications": [
                {
                    "question": "How long is the key retained?",
                    "resolution": "Twenty-four hours",
                }
            ],
            "plan": {
                "summary": "Add a durable idempotency boundary",
                "steps": [
                    {
                        "id": "P-001",
                        "title": "Implement storage contract",
                        "requirement_ids": ["R-001", "R-002"],
                    }
                ],
            },
            "tasks": [
                {
                    "id": "T-001",
                    "title": "Implement idempotency store",
                    "requirement_ids": ["R-001"],
                    "plan_step_ids": ["P-001"],
                    "depends_on": [],
                },
                {
                    "id": "T-002",
                    "title": "Add concurrent delivery control",
                    "requirement_ids": ["R-002"],
                    "plan_step_ids": ["P-001"],
                    "depends_on": ["T-001"],
                },
            ],
        }

    def test_policy_keeps_quick_light_and_requires_standard_deep_contracts(self) -> None:
        quick = feature_contract_policy({"intent": "build", "mode": "quick"})
        standard = feature_contract_policy({"intent": "build", "mode": "standard"})
        design = feature_contract_policy({"intent": "design", "mode": "deep"})
        review = feature_contract_policy({"intent": "review", "mode": "deep"})
        self.assertFalse(quick["required"])
        self.assertTrue(standard["required"])
        self.assertTrue(standard["convergence"]["required"])
        self.assertTrue(design["required"])
        self.assertFalse(design["convergence"]["required"])
        self.assertFalse(review["required"])

    def test_contract_requires_acceptance_plan_and_acyclic_task_coverage(self) -> None:
        contract = self.contract()
        validated = validate_feature_contract(
            contract, task_id="task-1", run_id="run-1"
        )
        self.assertEqual(validated["outcome"], contract["outcome"])

        broken = self.contract()
        broken["tasks"][0]["depends_on"] = ["T-002"]
        with self.assertRaisesRegex(WorkflowError, "dependency cycle"):
            validate_feature_contract(broken, task_id="task-1", run_id="run-1")

        missing = self.contract()
        missing["acceptance"] = missing["acceptance"][:1]
        with self.assertRaisesRegex(WorkflowError, "without acceptance"):
            validate_feature_contract(missing, task_id="task-1", run_id="run-1")

        placeholder = self.contract()
        placeholder["outcome"] = "Outcome: <measurable product outcome>"
        with self.assertRaisesRegex(WorkflowError, "replace the template placeholder"):
            validate_feature_contract(placeholder, task_id="task-1", run_id="run-1")

        orphan = self.contract()
        orphan["plan"]["steps"].append(
            {
                "id": "P-002",
                "title": "Document rollout",
                "requirement_ids": ["R-001"],
            }
        )
        with self.assertRaisesRegex(WorkflowError, "without executable tasks"):
            validate_feature_contract(orphan, task_id="task-1", run_id="run-1")

    def test_convergence_binds_every_requirement_to_tasks_paths_and_tests(self) -> None:
        contract = validate_feature_contract(
            self.contract(), task_id="task-1", run_id="run-1"
        )
        contract_sha = canonical_sha(contract)
        convergence = {
            "schema_version": 1,
            "run_id": "run-1",
            "feature_contract_sha256": contract_sha,
            "verdict": "converged",
            "tasks": [
                {"id": "T-001", "status": "completed"},
                {"id": "T-002", "status": "completed"},
            ],
            "requirements": [
                {
                    "id": "R-001",
                    "status": "proven",
                    "task_ids": ["T-001"],
                    "implementation_paths": ["backend/service.py"],
                    "acceptance_results": [
                        {
                            "id": "AC-001",
                            "status": "proven",
                            "oracle": "One operation exists after duplicate delivery",
                            "evidence_refs": [
                                {
                                    "verification_index": 0,
                                    "classes": ["focused"],
                                    "proof_excerpts": ["PROOF focused: idempotency"],
                                }
                            ],
                        }
                    ],
                },
                {
                    "id": "R-002",
                    "status": "proven",
                    "task_ids": ["T-002"],
                    "implementation_paths": ["backend/service.py"],
                    "acceptance_results": [
                        {
                            "id": "AC-002",
                            "status": "proven",
                            "oracle": "All callers observe the same operation id",
                            "evidence_refs": [
                                {
                                    "verification_index": 0,
                                    "classes": ["concurrency"],
                                    "proof_excerpts": [
                                        "PROOF concurrency: one operation"
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
        validated = validate_convergence(
            convergence,
            run_id="run-1",
            feature_contract=contract,
            feature_contract_sha256=contract_sha,
            verification=[
                {
                    "classes": ["focused", "concurrency"],
                    "class_evidence": {
                        "focused": {
                            "proof_excerpts": ["PROOF focused: idempotency"]
                        },
                        "concurrency": {
                            "proof_excerpts": [
                                "PROOF concurrency: one operation"
                            ]
                        },
                    },
                }
            ],
            changed_files=["backend/service.py"],
            no_change_reason=None,
        )
        self.assertEqual(validated["verdict"], "converged")

        with self.assertRaisesRegex(WorkflowError, "not linked to execution receipt"):
            validate_convergence(
                convergence,
                run_id="run-1",
                feature_contract=contract,
                feature_contract_sha256=contract_sha,
                verification=[
                    {
                        "execution_id": "trusted-execution-1",
                        "requirement_ids": ["R-001", "R-002"],
                        "acceptance_ids": ["AC-001"],
                        "classes": ["focused", "concurrency"],
                        "class_evidence": {
                            "focused": {
                                "proof_excerpts": ["PROOF focused: idempotency"]
                            },
                            "concurrency": {
                                "proof_excerpts": [
                                    "PROOF concurrency: one operation"
                                ]
                            },
                        },
                    }
                ],
                changed_files=["backend/service.py"],
                no_change_reason=None,
            )

        broken = dict(convergence)
        broken["requirements"] = convergence["requirements"][:1]
        with self.assertRaisesRegex(WorkflowError, "coverage mismatch"):
            validate_convergence(
                broken,
                run_id="run-1",
                feature_contract=contract,
                feature_contract_sha256=contract_sha,
                verification=[
                    {
                        "classes": ["focused", "concurrency"],
                        "class_evidence": {
                            "focused": {
                                "proof_excerpts": ["PROOF focused: idempotency"]
                            },
                            "concurrency": {
                                "proof_excerpts": [
                                    "PROOF concurrency: one operation"
                                ]
                            },
                        },
                    }
                ],
                changed_files=["backend/service.py"],
                no_change_reason=None,
            )

        wrong_plan = self.contract()
        wrong_plan["tasks"][0]["plan_step_ids"] = ["P-002"]
        wrong_plan["plan"]["steps"].append(
            {
                "id": "P-002",
                "title": "Unrelated parallel safety step",
                "requirement_ids": ["R-002"],
            }
        )
        with self.assertRaisesRegex(WorkflowError, "covered by its linked plan steps"):
            validate_feature_contract(wrong_plan, task_id="task-1", run_id="run-1")

        extra_link = self.contract()
        extra_link["plan"]["steps"] = [
            {
                "id": "P-001",
                "title": "Idempotent storage",
                "requirement_ids": ["R-001"],
            },
            {
                "id": "P-002",
                "title": "Parallel delivery",
                "requirement_ids": ["R-002"],
            },
        ]
        extra_link["tasks"][0]["plan_step_ids"] = ["P-001", "P-002"]
        extra_link["tasks"][1]["plan_step_ids"] = ["P-002"]
        with self.assertRaisesRegex(WorkflowError, "unrelated plan step link"):
            validate_feature_contract(extra_link, task_id="task-1", run_id="run-1")

        missing_acceptance = dict(convergence)
        missing_acceptance["requirements"] = [
            dict(convergence["requirements"][0]),
            dict(convergence["requirements"][1]),
        ]
        missing_acceptance["requirements"][0]["acceptance_results"] = []
        with self.assertRaisesRegex(WorkflowError, "requires acceptance_results"):
            validate_convergence(
                missing_acceptance,
                run_id="run-1",
                feature_contract=contract,
                feature_contract_sha256=contract_sha,
                verification=[
                    {
                        "classes": ["focused", "concurrency"],
                        "class_evidence": {
                            "focused": {"proof_excerpts": ["PROOF focused: idempotency"]},
                            "concurrency": {
                                "proof_excerpts": ["PROOF concurrency: one operation"]
                            },
                        },
                    }
                ],
                changed_files=["backend/service.py"],
                no_change_reason=None,
            )

        repeated_proof = copy.deepcopy(convergence)
        second_acceptance = repeated_proof["requirements"][1]["acceptance_results"][0]
        second_acceptance["evidence_refs"] = [
            {
                "verification_index": 0,
                "classes": ["focused"],
                "proof_excerpts": ["PROOF focused: idempotency"],
            }
        ]
        with self.assertRaisesRegex(WorkflowError, "distinct oracle-specific"):
            validate_convergence(
                repeated_proof,
                run_id="run-1",
                feature_contract=contract,
                feature_contract_sha256=contract_sha,
                verification=[
                    {
                        "classes": ["focused", "concurrency"],
                        "class_evidence": {
                            "focused": {"proof_excerpts": ["PROOF focused: idempotency"]},
                            "concurrency": {
                                "proof_excerpts": ["PROOF concurrency: one operation"]
                            },
                        },
                    }
                ],
                changed_files=["backend/service.py"],
                no_change_reason=None,
            )

        fake_unique = copy.deepcopy(repeated_proof)
        fake_unique["requirements"][0]["acceptance_results"][0]["evidence_refs"][0][
            "proof_excerpts"
        ].append("FAKE UNIQUE A")
        fake_unique["requirements"][1]["acceptance_results"][0]["evidence_refs"][0][
            "proof_excerpts"
        ].append("FAKE UNIQUE B")
        with self.assertRaisesRegex(WorkflowError, "unverified proof excerpts"):
            validate_convergence(
                fake_unique,
                run_id="run-1",
                feature_contract=contract,
                feature_contract_sha256=contract_sha,
                verification=[
                    {
                        "classes": ["focused", "concurrency"],
                        "class_evidence": {
                            "focused": {"proof_excerpts": ["PROOF focused: idempotency"]},
                            "concurrency": {
                                "proof_excerpts": ["PROOF concurrency: one operation"]
                            },
                        },
                    }
                ],
                changed_files=["backend/service.py"],
                no_change_reason=None,
            )


if __name__ == "__main__":
    unittest.main()
