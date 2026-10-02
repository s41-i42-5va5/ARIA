from __future__ import annotations


FUNCTIONAL_COVERAGE_HEADINGS = (
    "## Functional surface",
    "## Inputs, outputs, and states",
    "## Bindings and interactions",
    "## Failure, boundary, and composition behavior",
    "## Coverage verdict and test obligations",
)


def functional_coverage_contract(target_type: str | None) -> dict[str, object]:
    required = target_type in {"component", "repository"}
    return {
        "schema_version": 1,
        "required": required,
        "artifact": "outputs/functional-coverage.md" if required else None,
        "headings": list(FUNCTIONAL_COVERAGE_HEADINGS) if required else [],
        "method": (
            "LLM analysis grounded in specification when available and otherwise in code; "
            "distinguish specified, observed, inferred and unknown behavior"
            if required
            else None
        ),
        "completeness_gate": (
            "An independent functional_coverage_reviewer challenges omissions before closure"
            if required
            else None
        ),
        "anti_formalism": (
            "Free Markdown content; no domain schema or exhaustive Cartesian combinations"
            if required
            else None
        ),
    }


def required_role_contract(
    route: dict[str, object], target_type: str | None = None
) -> dict[str, object]:
    roles: list[str] = []
    mechanism = route.get("mechanism")
    intent = route.get("intent")
    mode = route.get("mode")
    if mechanism == "spec":
        roles.append("architecture_attacker")
    elif mechanism == "next-task-new":
        roles.extend(["c1_reviewer", "adversarial_test_designer", "c2_reviewer"])
    elif intent == "review" and mode == "deep":
        if target_type in {"component", "repository"}:
            roles.append("functional_coverage_reviewer")
        roles.extend(
            ["architecture_reviewer", "adversarial_reviewer", "test_reviewer"]
        )
    elif intent == "review":
        if target_type in {"component", "repository"}:
            roles.append("functional_coverage_reviewer")
        roles.append("independent_reviewer")
    elif mode == "standard" and intent in {"build", "design"}:
        roles.append("independent_reviewer")
    risk_signals = route.get("risk_signals", [])
    if isinstance(risk_signals, list) and any(
        signal in {"security", "безопас", "auth", "авториза", "tenancy", "tenant"}
        for signal in risk_signals
    ):
        roles.append("security_reviewer")
    return {
        "schema_version": 1,
        "required_roles": list(dict.fromkeys(roles)),
        "artifact_location": "outputs/roles/<role>.json",
        "independence_rule": (
            "Each required role must be performed by an agent other than the orchestrator; "
            "role agents cannot modify code, project documents or Git"
        ),
    }


def closure_contract(
    intent: str,
    mechanism: str | None = None,
    target_type: str | None = None,
    feature_contract: dict[str, object] | None = None,
) -> dict[str, object]:
    common = ["real read-back", "review", "explicit closure"]
    if intent == "build":
        required = ["implemented change", "test with actual output", *common]
    elif intent == "review":
        required = ["immutable scope", "verified findings", *common]
    else:
        required = ["usable design answer", *common]
    review_fields = [
        "scope_sha256",
        "findings",
        "coverage_path",
        "coverage_sha256",
        "verification[]",
    ]
    if target_type in {"component", "repository"}:
        review_fields.extend(
            [
                "functional_coverage_path",
                "functional_coverage_sha256",
                "functional_coverage_scope_sha256",
            ]
        )
    result_fields: dict[str, object] = {
        "common": ["status", "summary", "read_back", "review", "closure"],
        "intent": {
            "build": [
                "changed_files",
                "tests[].classes",
                "tests[].command",
                "tests[].exit_code",
                "tests[].output_path",
                "tests[].output_sha256",
                "tests[].output_excerpt",
                "tests[].actual_result",
                "tests[].class_evidence",
            ],
            "design": ["deliverable"],
            "review": review_fields,
        }[intent],
        "deep_spec_extra": [
            "spec_candidate_path",
            "spec_target",
            "spec_sha256",
            "research_assessment.required",
            "research_assessment.reason",
            "research_assessment.references[]",
            "adr_assessment.required",
            "adr_assessment.reason",
            "adr_assessment.candidates[]",
        ]
        if intent == "design" and mechanism == "spec"
        else [],
        "optional_behavior_memory": ["lessons[]", "lesson_resolutions[]"],
        "independent_roles": [
            "role_evidence[].artifact_path",
            "role artifact target_kind",
            "role artifact target_sha256",
        ],
        "optional_system_map_refresh": [
            "system_map_candidate_path",
            "system_map_sha256",
        ]
        if intent == "review"
        else [],
        "deep_spec_gate": {
            "proposal_status": "proposed",
            "approval_command": "_approve-spec",
            "direct_completed_forbidden": True,
        }
        if intent == "design" and mechanism == "spec"
        else None,
    }
    if isinstance(feature_contract, dict) and feature_contract.get("required") is True:
        result_fields["feature_contract"] = [
            "feature_contract.path",
            "feature_contract.sha256",
        ]
        convergence = feature_contract.get("convergence")
        if isinstance(convergence, dict) and convergence.get("required") is True:
            result_fields["convergence"] = [
                "convergence.path",
                "convergence.sha256",
            ]
            result_fields["convergence_artifact_fields"] = [
                "requirements[].implementation_paths",
                "requirements[].task_ids",
                "requirements[].acceptance_results[].id",
                "requirements[].acceptance_results[].oracle",
                "requirements[].acceptance_results[].evidence_refs",
            ]
    return {
        "required": required,
        "result_fields": result_fields,
        "internal_close_command": "_close-run",
        "no_manual_acknowledgments": True,
    }
