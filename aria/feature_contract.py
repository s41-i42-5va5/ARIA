from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from aria.errors import WorkflowError


ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{1,63}")
TEMPLATE_PLACEHOLDER_RE = re.compile(r"<[^<>\r\n]+>")


def canonical_sha(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def feature_contract_policy(
    route: Mapping[str, object],
    *,
    task_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    intent = str(route.get("intent"))
    mode = str(route.get("mode"))
    required = intent in {"design", "build"} and mode in {"standard", "deep"}
    contract_template = (
        {
            "schema_version": 1,
            "run_id": run_id or "<run_id>",
            "task_id": task_id or "<task_id>",
            "status": "ready",
            "outcome": "<measurable product outcome>",
            "ambiguities_resolved": True,
            "requirements": [{"id": "R-001", "statement": "<material behavior>"}],
            "acceptance": [
                {
                    "id": "AC-001",
                    "requirement_ids": ["R-001"],
                    "oracle": "<observable acceptance oracle>",
                }
            ],
            "clarifications": [
                {"question": "<material ambiguity>", "resolution": "<decision>"}
            ],
            "plan": {
                "summary": "<implementation approach>",
                "steps": [
                    {
                        "id": "P-001",
                        "title": "<plan step>",
                        "requirement_ids": ["R-001"],
                    }
                ],
            },
            "tasks": [
                {
                    "id": "T-001",
                    "title": "<executable task>",
                    "requirement_ids": ["R-001"],
                    "plan_step_ids": ["P-001"],
                    "depends_on": [],
                }
            ],
        }
        if required
        else None
    )
    convergence_template = (
        {
            "schema_version": 1,
            "run_id": run_id or "<run_id>",
            "feature_contract_sha256": "<raw artifact SHA-256>",
            "verdict": "converged",
            "tasks": [{"id": "T-001", "status": "completed"}],
            "requirements": [
                {
                    "id": "R-001",
                    "status": "proven",
                    "task_ids": ["T-001"],
                    "implementation_paths": ["<actual changed path>"],
                    "acceptance_results": [
                        {
                            "id": "AC-001",
                            "status": "proven",
                            "oracle": "<exact Feature Contract oracle>",
                            "evidence_refs": [
                                {
                                    "verification_index": 0,
                                    "classes": ["focused"],
                                    "proof_excerpts": [
                                        "<exact oracle-specific class_evidence proof excerpt>"
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        if required and intent == "build"
        else None
    )
    return {
        "schema_version": 1,
        "required": required,
        "artifact": "outputs/feature-contract.json" if required else None,
        "applies_to": "standard/deep design and build" if required else None,
        "required_sections": (
            [
                "outcome",
                "requirements",
                "acceptance",
                "clarifications",
                "plan",
                "tasks",
            ]
            if required
            else []
        ),
        "template": contract_template,
        "convergence": {
            "required": required and intent == "build",
            "artifact": (
                "outputs/convergence.json"
                if required and intent == "build"
                else None
            ),
            "rule": (
                "Every requirement must be linked to completed tasks, implementation "
                "paths and executed verification evidence before completed closure."
                if required and intent == "build"
                else None
            ),
            "template": convergence_template,
        },
    }


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WorkflowError(f"{label} must be a mapping with string keys")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} must be a non-empty string")
    text = value.strip()
    if TEMPLATE_PLACEHOLDER_RE.search(text) is not None:
        raise WorkflowError(f"{label} must replace the template placeholder")
    return text


def _identifier(value: object, label: str) -> str:
    identifier = _text(value, label)
    if ID_RE.fullmatch(identifier) is None:
        raise WorkflowError(f"{label} has an invalid identifier: {identifier!r}")
    return identifier


def _string_ids(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        suffix = "a list" if allow_empty else "a non-empty list"
        raise WorkflowError(f"{label} must be {suffix}")
    rows = [_identifier(item, f"{label} item") for item in value]
    if len(rows) != len(set(rows)):
        raise WorkflowError(f"{label} contains duplicate ids")
    return rows


def _assert_known(values: list[str], known: set[str], label: str) -> None:
    unknown = sorted(set(values) - known)
    if unknown:
        raise WorkflowError(f"{label} references unknown ids: {unknown}")


def _assert_acyclic(dependencies: dict[str, list[str]], label: str) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise WorkflowError(f"{label} contains a dependency cycle at {node}")
        if node in visited:
            return
        visiting.add(node)
        for dependency in dependencies.get(node, []):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in dependencies:
        visit(node)


def validate_feature_contract(
    value: object,
    *,
    task_id: str,
    run_id: str,
) -> dict[str, object]:
    contract = _mapping(value, "Feature Contract")
    if contract.get("schema_version") != 1:
        raise WorkflowError("Feature Contract schema_version must be 1")
    if contract.get("task_id") != task_id:
        raise WorkflowError("Feature Contract task_id does not match the run")
    if contract.get("run_id") != run_id:
        raise WorkflowError("Feature Contract run_id does not match the run")
    if contract.get("status") != "ready":
        raise WorkflowError("Feature Contract status must be ready")
    _text(contract.get("outcome"), "Feature Contract outcome")
    if contract.get("ambiguities_resolved") is not True:
        raise WorkflowError("Feature Contract must confirm ambiguities_resolved")

    raw_requirements = contract.get("requirements")
    if not isinstance(raw_requirements, list) or not raw_requirements:
        raise WorkflowError("Feature Contract requirements must be a non-empty list")
    requirement_ids: list[str] = []
    for index, raw in enumerate(raw_requirements):
        row = _mapping(raw, f"Feature Contract requirement {index}")
        requirement_ids.append(
            _identifier(row.get("id"), f"Feature Contract requirement {index} id")
        )
        _text(row.get("statement"), f"Feature Contract requirement {index} statement")
    if len(requirement_ids) != len(set(requirement_ids)):
        raise WorkflowError("Feature Contract contains duplicate requirement ids")
    known_requirements = set(requirement_ids)

    raw_acceptance = contract.get("acceptance")
    if not isinstance(raw_acceptance, list) or not raw_acceptance:
        raise WorkflowError("Feature Contract acceptance must be a non-empty list")
    acceptance_ids: list[str] = []
    accepted_requirements: set[str] = set()
    for index, raw in enumerate(raw_acceptance):
        row = _mapping(raw, f"Feature Contract acceptance {index}")
        acceptance_ids.append(
            _identifier(row.get("id"), f"Feature Contract acceptance {index} id")
        )
        linked = _string_ids(
            row.get("requirement_ids"),
            f"Feature Contract acceptance {index} requirement_ids",
        )
        _assert_known(
            linked,
            known_requirements,
            f"Feature Contract acceptance {index}",
        )
        accepted_requirements.update(linked)
        _text(row.get("oracle"), f"Feature Contract acceptance {index} oracle")
    if len(acceptance_ids) != len(set(acceptance_ids)):
        raise WorkflowError("Feature Contract contains duplicate acceptance ids")
    missing_acceptance = sorted(known_requirements - accepted_requirements)
    if missing_acceptance:
        raise WorkflowError(
            "Feature Contract requirements without acceptance oracles: "
            f"{missing_acceptance}"
        )

    clarifications = contract.get("clarifications")
    if not isinstance(clarifications, list):
        raise WorkflowError("Feature Contract clarifications must be a list")
    for index, raw in enumerate(clarifications):
        row = _mapping(raw, f"Feature Contract clarification {index}")
        _text(row.get("question"), f"Feature Contract clarification {index} question")
        _text(row.get("resolution"), f"Feature Contract clarification {index} resolution")

    plan = _mapping(contract.get("plan"), "Feature Contract plan")
    _text(plan.get("summary"), "Feature Contract plan summary")
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError("Feature Contract plan steps must be a non-empty list")
    step_ids: list[str] = []
    step_requirements: dict[str, set[str]] = {}
    planned_requirements: set[str] = set()
    for index, raw in enumerate(raw_steps):
        row = _mapping(raw, f"Feature Contract plan step {index}")
        step_id = _identifier(
            row.get("id"), f"Feature Contract plan step {index} id"
        )
        step_ids.append(step_id)
        _text(row.get("title"), f"Feature Contract plan step {index} title")
        linked = _string_ids(
            row.get("requirement_ids"),
            f"Feature Contract plan step {index} requirement_ids",
        )
        _assert_known(linked, known_requirements, f"Feature Contract plan step {index}")
        step_requirements[step_id] = set(linked)
        planned_requirements.update(linked)
    if len(step_ids) != len(set(step_ids)):
        raise WorkflowError("Feature Contract contains duplicate plan step ids")
    missing_plan = sorted(known_requirements - planned_requirements)
    if missing_plan:
        raise WorkflowError(
            f"Feature Contract requirements absent from the plan: {missing_plan}"
        )

    raw_tasks = contract.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise WorkflowError("Feature Contract tasks must be a non-empty list")
    task_ids: list[str] = []
    task_requirements: dict[str, set[str]] = {}
    dependencies: dict[str, list[str]] = {}
    implemented_requirements: set[str] = set()
    implemented_steps: set[str] = set()
    step_task_requirements: dict[str, set[str]] = {
        step_id: set() for step_id in step_ids
    }
    for index, raw in enumerate(raw_tasks):
        row = _mapping(raw, f"Feature Contract task {index}")
        item_id = _identifier(row.get("id"), f"Feature Contract task {index} id")
        task_ids.append(item_id)
        _text(row.get("title"), f"Feature Contract task {index} title")
        linked = _string_ids(
            row.get("requirement_ids"),
            f"Feature Contract task {index} requirement_ids",
        )
        _assert_known(linked, known_requirements, f"Feature Contract task {index}")
        task_requirements[item_id] = set(linked)
        implemented_requirements.update(linked)
        step_links = _string_ids(
            row.get("plan_step_ids"),
            f"Feature Contract task {index} plan_step_ids",
        )
        _assert_known(step_links, set(step_ids), f"Feature Contract task {index}")
        implemented_steps.update(step_links)
        step_link_requirements = set().union(
            *(step_requirements[step_id] for step_id in step_links)
        )
        if not set(linked).issubset(step_link_requirements):
            raise WorkflowError(
                f"Feature Contract task {item_id} requirements must be covered by "
                "its linked plan steps"
            )
        for step_id in step_links:
            if not set(linked).intersection(step_requirements[step_id]):
                raise WorkflowError(
                    f"Feature Contract task {item_id} has an unrelated plan step "
                    f"link: {step_id}"
                )
            step_task_requirements[step_id].update(
                set(linked).intersection(step_requirements[step_id])
            )
        dependencies[item_id] = _string_ids(
            row.get("depends_on", []),
            f"Feature Contract task {index} depends_on",
            allow_empty=True,
        )
    if len(task_ids) != len(set(task_ids)):
        raise WorkflowError("Feature Contract contains duplicate task ids")
    known_tasks = set(task_ids)
    for item_id, linked in dependencies.items():
        _assert_known(linked, known_tasks, f"Feature Contract task {item_id}")
        if item_id in linked:
            raise WorkflowError(f"Feature Contract task {item_id} depends on itself")
    _assert_acyclic(dependencies, "Feature Contract task graph")
    missing_tasks = sorted(known_requirements - implemented_requirements)
    if missing_tasks:
        raise WorkflowError(
            f"Feature Contract requirements absent from tasks: {missing_tasks}"
        )
    orphan_steps = sorted(set(step_ids) - implemented_steps)
    if orphan_steps:
        raise WorkflowError(
            f"Feature Contract plan steps without executable tasks: {orphan_steps}"
        )
    underimplemented_steps = {
        step_id: sorted(step_requirements[step_id] - covered)
        for step_id, covered in step_task_requirements.items()
        if covered != step_requirements[step_id]
    }
    if underimplemented_steps:
        raise WorkflowError(
            "Feature Contract plan step requirements absent from linked tasks: "
            f"{underimplemented_steps}"
        )

    return contract


def validate_convergence(
    value: object,
    *,
    run_id: str,
    feature_contract: Mapping[str, object],
    feature_contract_sha256: str,
    verification: list[dict[str, object]],
    changed_files: list[str],
    no_change_reason: str | None,
) -> dict[str, object]:
    convergence = _mapping(value, "Convergence")
    if convergence.get("schema_version") != 1:
        raise WorkflowError("Convergence schema_version must be 1")
    if convergence.get("run_id") != run_id:
        raise WorkflowError("Convergence run_id does not match the run")
    if convergence.get("feature_contract_sha256") != feature_contract_sha256:
        raise WorkflowError("Convergence feature contract SHA mismatch")
    if convergence.get("verdict") != "converged":
        raise WorkflowError("Completed closure requires convergence verdict converged")

    contract_tasks = {
        str(row["id"]): {str(item) for item in row["requirement_ids"]}
        for row in feature_contract.get("tasks", [])
        if isinstance(row, dict)
    }
    raw_task_results = convergence.get("tasks")
    if not isinstance(raw_task_results, list):
        raise WorkflowError("Convergence tasks must be a list")
    task_results: dict[str, str] = {}
    for index, raw in enumerate(raw_task_results):
        row = _mapping(raw, f"Convergence task {index}")
        task_id = _identifier(row.get("id"), f"Convergence task {index} id")
        status = _text(row.get("status"), f"Convergence task {index} status")
        if status != "completed":
            raise WorkflowError(f"Convergence task {task_id} is not completed")
        if task_id in task_results:
            raise WorkflowError(f"Convergence contains duplicate task {task_id}")
        task_results[task_id] = status
    if set(task_results) != set(contract_tasks):
        raise WorkflowError(
            "Convergence task coverage mismatch; "
            f"missing={sorted(set(contract_tasks) - set(task_results))}, "
            f"unknown={sorted(set(task_results) - set(contract_tasks))}"
        )

    contract_requirements = {
        str(row["id"])
        for row in feature_contract.get("requirements", [])
        if isinstance(row, dict)
    }
    contract_acceptance: dict[str, dict[str, str]] = {
        str(requirement_id): {}
        for requirement_id in contract_requirements
    }
    for raw_acceptance in feature_contract.get("acceptance", []):
        if not isinstance(raw_acceptance, dict):
            continue
        acceptance_id = str(raw_acceptance.get("id"))
        oracle = str(raw_acceptance.get("oracle"))
        for requirement_id in raw_acceptance.get("requirement_ids", []):
            if str(requirement_id) in contract_acceptance:
                contract_acceptance[str(requirement_id)][acceptance_id] = oracle
    raw_requirements = convergence.get("requirements")
    if not isinstance(raw_requirements, list):
        raise WorkflowError("Convergence requirements must be a list")
    requirement_results: set[str] = set()
    acceptance_proof_sets: dict[str, set[str]] = {}
    known_changed = set(changed_files)
    for index, raw in enumerate(raw_requirements):
        row = _mapping(raw, f"Convergence requirement {index}")
        requirement_id = _identifier(
            row.get("id"), f"Convergence requirement {index} id"
        )
        if requirement_id in requirement_results:
            raise WorkflowError(
                f"Convergence contains duplicate requirement {requirement_id}"
            )
        requirement_results.add(requirement_id)
        if row.get("status") != "proven":
            raise WorkflowError(
                f"Convergence requirement {requirement_id} is not proven"
            )
        linked_tasks = _string_ids(
            row.get("task_ids"),
            f"Convergence requirement {requirement_id} task_ids",
        )
        _assert_known(
            linked_tasks,
            set(contract_tasks),
            f"Convergence requirement {requirement_id}",
        )
        if not any(
            requirement_id in contract_tasks[task_id] for task_id in linked_tasks
        ):
            raise WorkflowError(
                f"Convergence requirement {requirement_id} is not linked to a task that implements it"
            )
        implementation_paths = row.get("implementation_paths")
        if not isinstance(implementation_paths, list) or not all(
            isinstance(path, str) and path for path in implementation_paths
        ):
            raise WorkflowError(
                f"Convergence requirement {requirement_id} implementation_paths must be a string list"
            )
        unknown_paths = sorted(set(implementation_paths) - known_changed)
        if unknown_paths:
            raise WorkflowError(
                f"Convergence requirement {requirement_id} references unchanged paths: {unknown_paths}"
            )
        if not implementation_paths and not no_change_reason:
            raise WorkflowError(
                f"Convergence requirement {requirement_id} has no implementation path"
            )
        raw_acceptance_results = row.get("acceptance_results")
        if not isinstance(raw_acceptance_results, list) or not raw_acceptance_results:
            raise WorkflowError(
                f"Convergence requirement {requirement_id} requires acceptance_results"
            )
        acceptance_results: set[str] = set()
        expected_acceptance = contract_acceptance.get(requirement_id, {})
        for acceptance_index, raw_acceptance_result in enumerate(
            raw_acceptance_results
        ):
            acceptance_result = _mapping(
                raw_acceptance_result,
                f"Convergence requirement {requirement_id} acceptance {acceptance_index}",
            )
            acceptance_id = _identifier(
                acceptance_result.get("id"),
                f"Convergence requirement {requirement_id} acceptance id",
            )
            if acceptance_id in acceptance_results:
                raise WorkflowError(
                    f"Convergence requirement {requirement_id} contains duplicate "
                    f"acceptance {acceptance_id}"
                )
            acceptance_results.add(acceptance_id)
            if acceptance_id not in expected_acceptance:
                raise WorkflowError(
                    f"Convergence requirement {requirement_id} references unrelated "
                    f"acceptance {acceptance_id}"
                )
            if acceptance_result.get("status") != "proven":
                raise WorkflowError(
                    f"Convergence acceptance {acceptance_id} is not proven"
                )
            if acceptance_result.get("oracle") != expected_acceptance[acceptance_id]:
                raise WorkflowError(
                    f"Convergence acceptance {acceptance_id} oracle does not match "
                    "the Feature Contract"
                )
            evidence_refs = acceptance_result.get("evidence_refs")
            if not isinstance(evidence_refs, list) or not evidence_refs:
                raise WorkflowError(
                    f"Convergence acceptance {acceptance_id} requires verification evidence"
                )
            acceptance_proofs: set[str] = set()
            for evidence_index, raw_evidence in enumerate(evidence_refs):
                evidence = _mapping(
                    raw_evidence,
                    f"Convergence acceptance {acceptance_id} evidence {evidence_index}",
                )
                verification_index = evidence.get("verification_index")
                if (
                    isinstance(verification_index, bool)
                    or not isinstance(verification_index, int)
                    or verification_index < 0
                    or verification_index >= len(verification)
                ):
                    raise WorkflowError(
                        f"Convergence acceptance {acceptance_id} has invalid verification_index"
                    )
                classes = _string_ids(
                    evidence.get("classes"),
                    f"Convergence acceptance {acceptance_id} evidence classes",
                )
                executed = verification[verification_index].get("classes", [])
                executed_set = {
                    str(item) for item in executed if isinstance(item, str)
                }
                execution_id = verification[verification_index].get("execution_id")
                if execution_id is not None:
                    linked_requirements = {
                        str(value)
                        for value in verification[verification_index].get(
                            "requirement_ids", []
                        )
                        if isinstance(value, str)
                    }
                    linked_acceptance = {
                        str(value)
                        for value in verification[verification_index].get(
                            "acceptance_ids", []
                        )
                        if isinstance(value, str)
                    }
                    if (
                        requirement_id not in linked_requirements
                        or acceptance_id not in linked_acceptance
                    ):
                        raise WorkflowError(
                            f"Convergence acceptance {acceptance_id} is not linked to "
                            f"execution receipt {execution_id}"
                        )
                _assert_known(
                    classes,
                    executed_set,
                    f"Convergence acceptance {acceptance_id} evidence",
                )
                raw_proofs = evidence.get("proof_excerpts")
                if not isinstance(raw_proofs, list) or not raw_proofs or not all(
                    isinstance(proof, str) and proof.strip() for proof in raw_proofs
                ):
                    raise WorkflowError(
                        f"Convergence acceptance {acceptance_id} evidence proof_excerpts "
                        "must be a non-empty string list"
                    )
                declared_proofs = {str(proof).strip() for proof in raw_proofs}
                class_evidence = verification[verification_index].get(
                    "class_evidence", {}
                )
                if not isinstance(class_evidence, dict):
                    raise WorkflowError(
                        f"Convergence acceptance {acceptance_id} verification has no "
                        "class_evidence"
                    )
                verified_declared: set[str] = set()
                for test_class in classes:
                    class_row = class_evidence.get(test_class)
                    accepted_proofs = (
                        {
                            str(proof)
                            for proof in class_row.get("proof_excerpts", [])
                            if isinstance(proof, str)
                        }
                        if isinstance(class_row, dict)
                        else set()
                    )
                    if not declared_proofs.intersection(accepted_proofs):
                        raise WorkflowError(
                            f"Convergence acceptance {acceptance_id} has no verified "
                            f"proof excerpt for class {test_class}"
                        )
                    verified_declared.update(declared_proofs.intersection(accepted_proofs))
                unverified_proofs = declared_proofs - verified_declared
                if unverified_proofs:
                    raise WorkflowError(
                        f"Convergence acceptance {acceptance_id} declares unverified "
                        f"proof excerpts: {sorted(unverified_proofs)}"
                    )
                acceptance_proofs.update(verified_declared)
            acceptance_proof_sets[acceptance_id] = acceptance_proofs
        if acceptance_results != set(expected_acceptance):
            raise WorkflowError(
                f"Convergence acceptance coverage mismatch for requirement "
                f"{requirement_id}; missing={sorted(set(expected_acceptance) - acceptance_results)}, "
                f"unknown={sorted(acceptance_results - set(expected_acceptance))}"
            )

    if requirement_results != contract_requirements:
        raise WorkflowError(
            "Convergence requirement coverage mismatch; "
            f"missing={sorted(contract_requirements - requirement_results)}, "
            f"unknown={sorted(requirement_results - contract_requirements)}"
        )
    proof_owners: dict[str, set[str]] = {}
    for acceptance_id, proofs in acceptance_proof_sets.items():
        for proof in proofs:
            proof_owners.setdefault(proof, set()).add(acceptance_id)
    acceptance_with_unique_proof = {
        acceptance_id
        for proof, owners in proof_owners.items()
        if len(owners) == 1
        for acceptance_id in owners
    }
    missing_unique = sorted(set(acceptance_proof_sets) - acceptance_with_unique_proof)
    if missing_unique:
        raise WorkflowError(
            "Each acceptance criterion requires at least one distinct oracle-specific "
            f"proof excerpt; missing={missing_unique}"
        )
    return convergence
