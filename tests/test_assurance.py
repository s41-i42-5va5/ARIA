from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from aria.assurance import (
    apply_system_map_impact,
    build_assurance_plan,
    validate_test_evidence,
)
from aria.errors import WorkflowError


class AssuranceTests(unittest.TestCase):
    def test_repository_review_is_a_real_assurance_campaign(self) -> None:
        plan = build_assurance_plan(
            task="Полный repository audit перед production release",
            route={
                "intent": "review",
                "mode": "deep",
                "risk_signals": ["concurrency", "security"],
            },
            changed_paths=[],
            risk_flags=["security", "concurrency"],
            stack_text="```powershell\npython -m pytest -q\n```\n",
            target_type="repository",
        )
        self.assertEqual(plan["level"], "assurance-campaign")
        self.assertTrue(
            {
                "e2e",
                "adversarial",
                "concurrency",
                "load",
                "stress",
                "recovery",
                "full-regression",
                "security-isolation",
            }.issubset(set(plan["required_execution_classes"]))
        )
        all_classes = set(plan["required_execution_classes"]) | set(
            plan["required_assessment_classes"]
        )
        self.assertTrue({"soak", "chaos", "migration", "rollback"}.issubset(all_classes))

    def test_shared_primitive_change_expands_blast_radius(self) -> None:
        base = build_assurance_plan(
            task="Измени контракт",
            route={"intent": "build", "mode": "quick", "risk_signals": []},
            changed_paths=["core/contracts.py"],
            risk_flags=[],
            stack_text="",
            target_type=None,
        )
        updated = apply_system_map_impact(
            base,
            system_map={
                "content": {
                    "components": [
                        {"id": "api", "paths": ["api/**"]},
                        {"id": "core", "paths": ["core/**"]},
                    ],
                    "shared_primitives": [
                        {"id": "contracts", "paths": ["core/contracts.py"]}
                    ],
                }
            },
            changed_paths=["core/contracts.py"],
        )
        self.assertEqual(updated["level"], "full")
        self.assertEqual(updated["impacted_components"], ["core"])
        self.assertEqual(updated["impacted_shared_primitives"], ["contracts"])
        self.assertTrue(
            {"integration", "e2e", "adversarial", "full-regression"}.issubset(
                set(updated["required_execution_classes"])
            )
        )

    def test_component_risks_dependents_and_critical_flows_drive_assurance(self) -> None:
        base = build_assurance_plan(
            task="Change internal coordinator",
            route={"intent": "build", "mode": "quick", "risk_signals": []},
            changed_paths=["core/coordinator.py"],
            risk_flags=[],
            stack_text="",
            target_type=None,
        )
        updated = apply_system_map_impact(
            base,
            system_map={
                "fresh": True,
                "content": {
                    "components": [
                        {
                            "id": "coordinator",
                            "paths": ["core/**"],
                            "depends_on": [],
                            "risks": [
                                "cross-tenant race",
                                "restart recovery",
                            ],
                            "test_seams": ["parallel tenant recovery scenario"],
                        },
                        {
                            "id": "api",
                            "paths": ["api/**"],
                            "depends_on": ["coordinator"],
                            "risks": [],
                            "test_seams": ["API integration"],
                        },
                    ],
                    "shared_primitives": [],
                    "critical_flows": [
                        {
                            "id": "tenant-job",
                            "steps": ["api", "coordinator"],
                            "failure_modes": ["duplicate effect"],
                            "assurance": ["linked E2E"],
                        }
                    ],
                },
            },
            changed_paths=["core/coordinator.py"],
        )
        self.assertEqual(updated["impacted_components"], ["coordinator"])
        self.assertEqual(updated["impacted_dependents"], ["api"])
        self.assertEqual(updated["impacted_critical_flows"], ["tenant-job"])
        self.assertIn("parallel tenant recovery scenario", updated["recommended_test_seams"])
        self.assertTrue(
            {
                "integration",
                "e2e",
                "adversarial",
                "security-isolation",
                "concurrency",
                "load",
                "stress",
                "recovery",
                "chaos",
            }.issubset(set(updated["required_execution_classes"]))
        )

    def test_russian_component_risks_drive_the_same_assurance_classes(self) -> None:
        base = build_assurance_plan(
            task="Изменить внутренний механизм",
            route={"intent": "build", "mode": "quick", "risk_signals": []},
            changed_paths=["core/engine.py"],
            risk_flags=[],
            stack_text="",
            target_type=None,
        )
        updated = apply_system_map_impact(
            base,
            system_map={
                "fresh": True,
                "content": {
                    "components": [
                        {
                            "id": "core",
                            "paths": ["core/**"],
                            "depends_on": [],
                            "risks": [
                                "межтенантная утечка данных и нарушение безопасности",
                                "гонка при параллельной обработке",
                                "восстановление после сбоя и потери данных",
                                "миграция схемы с откатом",
                                "перегрузка и опасное аварийное состояние",
                            ],
                            "test_seams": ["связный аварийный сценарий"],
                        },
                        {
                            "id": "other",
                            "paths": ["other/**"],
                            "depends_on": [],
                            "risks": [],
                            "test_seams": [],
                        },
                    ],
                    "shared_primitives": [],
                    "critical_flows": [
                        {
                            "id": "unrelated-flow",
                            "steps": ["other"],
                            "failure_modes": [],
                            "assurance": [],
                        }
                    ],
                },
            },
            changed_paths=["core/engine.py"],
        )
        self.assertEqual(updated["impacted_critical_flows"], [])
        self.assertTrue(
            {
                "security-isolation",
                "concurrency",
                "load",
                "stress",
                "recovery",
                "chaos",
                "migration",
                "rollback",
                "e2e",
                "adversarial",
            }.issubset(set(updated["required_execution_classes"]))
        )

    def test_verification_reads_real_output_and_cannot_relabel_a_missing_class(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            output = root / "outputs" / "test.log"
            output.parent.mkdir()
            metrics = (
                '{"concurrency":200,"operations":10000,'
                '"duration_seconds":20,"error_rate":0}'
            )
            output.write_text(
                "linked scenario: 200 users, restart recovered\n"
                "E2E request accepted\nE2E final state observed\n"
                "LOAD workers exercised\nLOAD latency read back\n"
                f"{metrics}\n"
                "RECOVERY fault injected\nRECOVERY durable state restored\n",
                encoding="utf-8",
            )
            row = {
                "classes": ["e2e", "load", "recovery"],
                "command": "project-test --linked --users 200 --restart",
                "exit_code": 0,
                "output_path": "outputs/test.log",
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "output_excerpt": "200 users, restart recovered",
                "actual_result": "The linked flow recovered after restart under 200-user load",
                "class_evidence": {
                    "e2e": {
                        "proof_excerpts": [
                            "E2E request accepted",
                            "E2E final state observed",
                        ]
                    },
                    "load": {
                        "proof_excerpts": [
                            "LOAD workers exercised",
                            "LOAD latency read back",
                        ],
                        "metrics_excerpt": metrics,
                    },
                    "recovery": {
                        "proof_excerpts": [
                            "RECOVERY fault injected",
                            "RECOVERY durable state restored",
                        ]
                    },
                },
                "scenario": {
                    "initial_state": "empty queue",
                    "actions": ["submit", "restart", "drain"],
                    "expected_result": "all accepted jobs finish once",
                    "forbidden_result": "lost or duplicate job",
                    "side_effects": "durable rows and metrics",
                    "correlation": "test run id",
                    "parallelism_or_load": "200 users",
                    "actual_result": "recovered",
                },
            }
            normalized = validate_test_evidence(
                run_root=root,
                plan={
                    "required_execution_classes": ["e2e", "load", "recovery"],
                    "required_assessment_classes": [],
                },
                evidence=[row],
            )
            self.assertEqual(normalized[0]["status"], "passed")
            with self.assertRaisesRegex(WorkflowError, "Missing required executed"):
                validate_test_evidence(
                    run_root=root,
                    plan={
                        "required_execution_classes": ["e2e", "load", "stress"],
                        "required_assessment_classes": [],
                    },
                    evidence=[row],
                )


if __name__ == "__main__":
    unittest.main()
