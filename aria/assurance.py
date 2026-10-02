from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatch
from pathlib import Path

import yaml

from aria.errors import ConfigurationError, WorkflowError
from aria.project import ProjectConfig, canonical_sha, git_resolve_commit, safe_relative_path


TEST_CLASSES = {
    "focused",
    "integration",
    "e2e",
    "adversarial",
    "concurrency",
    "load",
    "stress",
    "soak",
    "recovery",
    "chaos",
    "migration",
    "rollback",
    "security-isolation",
    "full-regression",
}
REVIEW_DIMENSIONS = {
    "requirements",
    "correctness",
    "architecture",
    "security",
    "reliability",
    "performance",
    "concurrency",
    "data-integrity",
    "observability",
    "maintainability",
    "tests",
    "documentation",
}


def _contains(text: str, *terms: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _canonical_commands(stack_text: str) -> list[str]:
    commands: list[str] = []
    in_fence = False
    for raw in stack_text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence or not line or line.startswith("#"):
            continue
        if line.startswith(("python ", "py ", "npm ", "pnpm ", "yarn ", "docker ", ".\\gradlew", "./gradlew", "pytest ")):
            commands.append(line)
    return list(dict.fromkeys(commands))


def build_assurance_plan(
    *,
    task: str,
    route: dict[str, object],
    changed_paths: list[str],
    risk_flags: list[str],
    stack_text: str,
    target_type: str | None,
) -> dict[str, object]:
    intent = str(route.get("intent"))
    mode = str(route.get("mode"))
    text = " ".join([task, *changed_paths, *risk_flags, *map(str, route.get("risk_signals", []))])
    repository = intent == "review" and target_type == "repository"
    cross_layer = _contains(
        text,
        "cross-layer",
        "multi-layer",
        "end-to-end",
        "e2e",
        "api и ui",
        "воркер",
        "integration",
        "интеграц",
    ) or len({path.split("/", 1)[0] for path in changed_paths}) >= 2
    signals = {
        "security": _contains(text, "security", "безопас", "auth", "авториза", "tenant"),
        "concurrency": _contains(text, "concurr", "parallel", "race", "очеред", "worker", "параллел"),
        "migration": _contains(text, "migration", "миграц", "schema", "схем"),
        "recovery": _contains(text, "recovery", "rollback", "failover", "восстанов", "data-loss", "потеря данных"),
        "release": _contains(text, "release", "релиз", "production", "боев", "repository", "репозитор"),
        "hardware": _contains(text, "hardware", "device", "android", "usb", "gnss", "nmea", "watchdog", "устройств"),
    }
    execute: list[str] = []
    assess: list[str] = []
    reasons: list[str] = []
    if intent == "build":
        execute.append("focused")
        if mode in {"standard", "deep"}:
            execute.append("integration")
        if mode == "deep" or cross_layer or signals["hardware"]:
            execute.extend(["e2e", "adversarial"])
            reasons.append("deep/cross-layer work requires a real linked scenario")
    elif intent == "review" and target_type != "spec":
        execute.append("focused")
        if mode in {"standard", "deep"}:
            execute.append("integration")
        if mode == "deep" or cross_layer:
            execute.extend(["e2e", "adversarial"])
    if signals["security"]:
        execute.append("security-isolation")
        reasons.append("security or isolation boundary detected")
    if signals["concurrency"]:
        execute.extend(["concurrency", "load", "stress"])
        reasons.append("shared state/concurrency requires load and race evidence")
    if signals["migration"]:
        execute.extend(["migration", "rollback"])
        reasons.append("schema or migration requires forward and rollback evidence")
    if signals["recovery"]:
        execute.extend(["recovery", "chaos"])
        reasons.append("recovery/durability behavior detected")
    if signals["release"] and intent != "design":
        execute.extend(["full-regression", "soak"])
        reasons.append("release/production scope requires regression and duration evidence")
    if repository:
        execute.extend(
            [
                "e2e",
                "adversarial",
                "concurrency",
                "load",
                "stress",
                "recovery",
                "full-regression",
            ]
        )
        assess.extend(["soak", "chaos", "migration", "rollback", "security-isolation"])
        reasons.append("repository assurance campaign exercises linked extreme behavior")
    execute = list(dict.fromkeys(execute))
    assess = [item for item in dict.fromkeys(assess) if item not in execute]
    if intent == "review":
        dimensions = [
            "correctness",
            "architecture",
            "security",
            "reliability",
            "performance",
            "concurrency",
            "data-integrity",
            "maintainability",
            "tests",
            "documentation",
        ]
        if target_type == "spec":
            dimensions.insert(0, "requirements")
        if repository:
            dimensions.insert(-2, "observability")
    else:
        dimensions = ["correctness", "tests"]
    level = "assurance-campaign" if repository else (
        "full" if mode == "deep" else "e2e" if "e2e" in execute else "focused"
    )
    return {
        "schema_version": 1,
        "level": level,
        "risk_signals": signals,
        "required_execution_classes": execute,
        "required_assessment_classes": assess,
        "review_dimensions": dimensions,
        "reasons": reasons or ["minimum evidence for selected route"],
        "canonical_command_hints": _canonical_commands(stack_text),
        "scenario_contract": {
            "required_for": ["e2e", "adversarial", "concurrency", "load", "stress", "soak", "recovery", "chaos"],
            "fields": [
                "initial_state",
                "actions",
                "expected_result",
                "forbidden_result",
                "side_effects",
                "correlation",
                "parallelism_or_load",
                "actual_result",
            ],
            "rule": "A complex check must exercise a linked workflow, not rename a unit test.",
        },
        "read_back_required": True,
    }


def apply_system_map_impact(
    plan: dict[str, object],
    *,
    system_map: dict[str, object],
    changed_paths: list[str],
) -> dict[str, object]:
    content = system_map.get("content")
    if not isinstance(content, dict) or not changed_paths:
        return plan
    impacted: list[str] = []
    shared: list[str] = []
    mapped_paths: set[str] = set()
    component_rows: dict[str, dict[str, object]] = {}

    def matches(pattern: str, path: str) -> bool:
        clean = pattern.replace("\\", "/")
        if clean.endswith("/**"):
            return path.startswith(clean[:-3].rstrip("/") + "/") or path == clean[:-3].rstrip("/")
        return fnmatch(path, clean)

    for component in content.get("components", []):
        if not isinstance(component, dict):
            continue
        component_id = str(component.get("id"))
        component_rows[component_id] = component
        patterns = component.get("paths", [])
        component_matches = {
            path
            for path in changed_paths
            if isinstance(patterns, list)
            and any(
                isinstance(pattern, str) and matches(pattern, path)
                for pattern in patterns
            )
        }
        if component_matches:
            impacted.append(component_id)
            mapped_paths.update(component_matches)
    for primitive in content.get("shared_primitives", []):
        if not isinstance(primitive, dict):
            continue
        patterns = primitive.get("paths", [])
        primitive_matches = {
            path
            for path in changed_paths
            if isinstance(patterns, list)
            and any(
                isinstance(pattern, str) and matches(pattern, path)
                for pattern in patterns
            )
        }
        if primitive_matches:
            shared.append(str(primitive.get("id")))
            mapped_paths.update(primitive_matches)
    unmapped = sorted(set(changed_paths) - mapped_paths)
    impacted_ids = set(impacted)
    dependents: set[str] = set()
    while True:
        discovered = {
            component_id
            for component_id, component in component_rows.items()
            if component_id not in impacted_ids | dependents
            and isinstance(component.get("depends_on"), list)
            and bool(
                {str(item) for item in component["depends_on"]}
                & (impacted_ids | dependents)
            )
        }
        if not discovered:
            break
        dependents.update(discovered)
    risk_rows: list[str] = []
    test_seams: list[str] = []
    for component_id in impacted:
        component = component_rows.get(component_id, {})
        risks = component.get("risks", [])
        seams = component.get("test_seams", [])
        if isinstance(risks, list):
            risk_rows.extend(str(value) for value in risks if isinstance(value, str))
        if isinstance(seams, list):
            test_seams.extend(str(value) for value in seams if isinstance(value, str))
    critical_flows: list[str] = []
    affected_component_ids = impacted_ids | dependents
    for flow in content.get("critical_flows", []):
        if not isinstance(flow, dict) or not isinstance(flow.get("steps"), list):
            continue
        if affected_component_ids & {str(step) for step in flow["steps"]}:
            critical_flows.append(str(flow.get("id")))
            failure_modes = flow.get("failure_modes", [])
            assurance = flow.get("assurance", [])
            if isinstance(failure_modes, list):
                risk_rows.extend(
                    str(value) for value in failure_modes if isinstance(value, str)
                )
            if isinstance(assurance, list):
                test_seams.extend(
                    str(value) for value in assurance if isinstance(value, str)
                )
    updated = dict(plan)
    updated["impacted_components"] = list(dict.fromkeys(impacted))
    updated["impacted_dependents"] = sorted(dependents)
    updated["impacted_critical_flows"] = list(dict.fromkeys(critical_flows))
    updated["impacted_risks"] = list(dict.fromkeys(risk_rows))
    updated["recommended_test_seams"] = list(dict.fromkeys(test_seams))
    updated["impacted_shared_primitives"] = list(dict.fromkeys(shared))
    updated["unmapped_changed_paths"] = unmapped
    updated["system_map_fresh"] = system_map.get("fresh") is True
    execute = list(updated.get("required_execution_classes", []))
    reasons = list(updated.get("reasons", []))

    def require(classes: tuple[str, ...], reason: str) -> None:
        for test_class in classes:
            if test_class not in execute:
                execute.append(test_class)
        reasons.append(reason)

    risk_text = " ".join(risk_rows).lower()
    if _contains(
        risk_text,
        "security",
        "auth",
        "tenant",
        "privilege",
        "secret",
        "isolation",
        "ssrf",
        "access",
        "escape",
        "безопас",
        "межтенант",
        "утеч",
        "авториз",
        "доступ",
        "изоляц",
        "привилег",
        "секрет",
    ):
        require(
            ("security-isolation",),
            "mapped component risk requires negative security/isolation controls",
        )
    if _contains(
        risk_text,
        "concurr",
        "race",
        "parallel",
        "duplicate",
        "idempot",
        "backpressure",
        "lost work",
        "deadlock",
        "counter",
        "overspend",
        "гонк",
        "параллел",
        "конкурент",
        "дубликат",
        "идемпот",
        "взаимоблок",
        "потеря работ",
        "счётчик",
        "счетчик",
        "очеред",
    ):
        require(
            ("concurrency", "load", "stress"),
            "mapped component risk requires race and pressure evidence",
        )
    if _contains(
        risk_text,
        "recovery",
        "restart",
        "durab",
        "data loss",
        "failover",
        "partial",
        "rollout failure",
        "восстанов",
        "перезапуск",
        "сохранност",
        "потеря дан",
        "отказоуст",
        "частич",
        "сбой",
    ):
        require(
            ("recovery", "chaos"),
            "mapped component risk requires failure and recovery evidence",
        )
    if _contains(
        risk_text,
        "migration",
        "schema",
        "rollback",
        "миграц",
        "схем",
        "откат",
    ):
        require(
            ("migration", "rollback"),
            "mapped component risk requires forward/rollback evidence",
        )
    if _contains(
        risk_text,
        "performance",
        "overload",
        "resource exhaustion",
        "denial of service",
        "latency",
        "производитель",
        "перегруз",
        "исчерпан",
        "отказ в обслуж",
        "задержк",
        "нагруз",
    ):
        require(
            ("load", "stress"),
            "mapped component risk requires measurable load/stress evidence",
        )
    if _contains(
        risk_text,
        "safety",
        "unsafe",
        "watchdog",
        "collision",
        "функциональн безопас",
        "физическ безопас",
        "опасн",
        "аварийн",
        "сторожев",
        "столкнов",
    ):
        require(
            ("e2e", "adversarial", "recovery"),
            "mapped safety risk requires linked failure-boundary evidence",
        )
    if dependents:
        require(
            ("integration",),
            "reverse dependents of the changed component require integration retest",
        )
    if critical_flows:
        require(
            ("e2e", "adversarial"),
            "changed component participates in a critical linked flow",
        )
    updated["required_execution_classes"] = execute
    updated["reasons"] = list(dict.fromkeys(reasons))
    if critical_flows or dependents or any(
        test_class in execute
        for test_class in (
            "security-isolation",
            "concurrency",
            "load",
            "stress",
            "recovery",
            "chaos",
            "migration",
            "rollback",
        )
    ):
        if updated.get("level") == "focused":
            updated["level"] = "e2e"
    if shared:
        for test_class in ("integration", "e2e", "adversarial", "full-regression"):
            if test_class not in execute:
                execute.append(test_class)
        updated["required_execution_classes"] = execute
        reasons.append(
            "shared primitive changed; dependent components and neighboring flows must be retested"
        )
        updated["reasons"] = list(dict.fromkeys(reasons))
        updated["level"] = "full"
    if unmapped:
        for test_class in ("integration", "e2e", "adversarial", "full-regression"):
            if test_class not in execute:
                execute.append(test_class)
        updated["required_execution_classes"] = execute
        if unmapped:
            reasons.append(
                "changed paths are not mapped; linked impact is uncertain and requires fail-safe regression evidence"
            )
        updated["reasons"] = list(dict.fromkeys(reasons))
        updated["level"] = "full"
    if system_map.get("fresh") is not True:
        updated["impact_assessment_required"] = True
        reasons.append(
            "system map is stale; the LLM must recheck blast radius before treating mapped work as local"
        )
        updated["reasons"] = list(dict.fromkeys(reasons))
    return updated


def merge_assurance_plans(
    original: dict[str, object], actual: dict[str, object]
) -> dict[str, object]:
    """Never let closure-time facts weaken the immutable start recommendation."""
    merged = dict(original)
    for key in (
        "required_execution_classes",
        "required_assessment_classes",
        "review_dimensions",
        "reasons",
        "impacted_components",
        "impacted_dependents",
        "impacted_critical_flows",
        "impacted_risks",
        "recommended_test_seams",
        "impacted_shared_primitives",
        "unmapped_changed_paths",
    ):
        values: list[object] = []
        for source in (original, actual):
            raw = source.get(key, [])
            if isinstance(raw, list):
                values.extend(raw)
        merged[key] = list(dict.fromkeys(values))
    rank = {"focused": 0, "e2e": 1, "full": 2, "assurance-campaign": 3}
    levels = [str(original.get("level", "focused")), str(actual.get("level", "focused"))]
    merged["level"] = max(levels, key=lambda value: rank.get(value, 0))
    merged["risk_signals"] = {
        **(
            original.get("risk_signals", {})
            if isinstance(original.get("risk_signals"), dict)
            else {}
        ),
        **(
            actual.get("risk_signals", {})
            if isinstance(actual.get("risk_signals"), dict)
            else {}
        ),
    }
    merged["impact_assessment_required"] = bool(
        original.get("impact_assessment_required")
        or actual.get("impact_assessment_required")
    )
    merged["system_map_fresh"] = bool(
        original.get("system_map_fresh") and actual.get("system_map_fresh")
    )
    return merged


def inspect_system_map_file(
    project: ProjectConfig, path: Path, git: dict[str, object]
) -> dict[str, object]:
    if not path.is_file():
        return {
            "present": False,
            "valid": False,
            "fresh": False,
            "path": str(path),
            "errors": ["SYSTEM_MAP.yaml is missing"],
            "summary": None,
        }
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        return {
            "present": True,
            "valid": False,
            "fresh": False,
            "path": str(path),
            "errors": [str(error)],
            "summary": None,
        }
    errors: list[str] = []
    if not isinstance(payload, dict):
        errors.append("map must be a mapping")
        payload = {}
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if payload.get("project_id") != project.project_id:
        errors.append("project_id mismatch")
    components = payload.get("components")
    flows = payload.get("critical_flows")
    dimensions = payload.get("dimensions")
    if not isinstance(components, list) or not components:
        errors.append("components must be a non-empty list")
    if not isinstance(flows, list) or not flows:
        errors.append("critical_flows must be a non-empty list")
    if not isinstance(dimensions, dict) or not dimensions:
        errors.append("dimensions must be a non-empty mapping")
    elif any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) and value.strip() for value in values)
        for key, values in dimensions.items()
    ):
        errors.append("each dimension must contain a non-empty string list")
    generated = payload.get("generated_from")
    mapped_head = generated.get("git_head") if isinstance(generated, dict) else None
    head_exists = False
    if isinstance(mapped_head, str):
        try:
            head_exists = git_resolve_commit(project.code_root, mapped_head) == mapped_head
        except ConfigurationError:
            errors.append("generated_from.git_head is not in the registered repository")
    else:
        errors.append("generated_from.git_head is missing")
    changes = git.get("changes", {})
    working_sha = canonical_sha(changes.get("paths", {}) if isinstance(changes, dict) else {})
    mapped_working = generated.get("working_tree_sha256") if isinstance(generated, dict) else None
    fresh = head_exists and mapped_head == git.get("head") and mapped_working == working_sha
    ids = []
    if isinstance(components, list):
        for index, component in enumerate(components):
            if not isinstance(component, dict) or not isinstance(component.get("id"), str):
                errors.append(f"component {index} requires id")
                continue
            ids.append(component["id"])
            for field in ("name", "layer", "domain"):
                if not isinstance(component.get(field), str) or not str(
                    component[field]
                ).strip():
                    errors.append(f"component {component['id']} requires {field}")
            for field in ("responsibilities", "risks", "test_seams"):
                values = component.get(field)
                if not isinstance(values, list) or not values:
                    errors.append(f"component {component['id']} requires {field}")
            if not isinstance(component.get("depends_on"), list):
                errors.append(f"component {component['id']} requires depends_on")
            paths = component.get("paths")
            if not isinstance(paths, list) or not paths:
                errors.append(f"component {component['id']} requires paths")
            else:
                for value in paths:
                    if not isinstance(value, str):
                        errors.append(f"component {component['id']} has a non-string path")
                        continue
                    try:
                        safe_relative_path(value.rstrip("/**") or ".")
                    except ConfigurationError:
                        errors.append(f"component {component['id']} has unsafe path {value!r}")
    if len(ids) != len(set(ids)):
        errors.append("component ids must be unique")
    flow_ids: list[str] = []
    if isinstance(flows, list):
        for index, flow in enumerate(flows):
            if not isinstance(flow, dict) or not isinstance(flow.get("id"), str):
                errors.append(f"critical flow {index} requires id")
                continue
            flow_ids.append(flow["id"])
            for field in ("steps", "failure_modes", "assurance"):
                values = flow.get(field)
                if not isinstance(values, list) or not values:
                    errors.append(f"critical flow {flow['id']} requires {field}")
    if len(flow_ids) != len(set(flow_ids)):
        errors.append("critical flow ids must be unique")
    primitives = payload.get("shared_primitives", [])
    primitive_ids: list[str] = []
    if not isinstance(primitives, list):
        errors.append("shared_primitives must be a list")
    else:
        for index, primitive in enumerate(primitives):
            if not isinstance(primitive, dict) or not isinstance(
                primitive.get("id"), str
            ):
                errors.append(f"shared primitive {index} requires id")
                continue
            primitive_ids.append(primitive["id"])
            paths = primitive.get("paths")
            if not isinstance(paths, list) or not paths:
                errors.append(
                    f"shared primitive {primitive['id']} requires paths"
                )
            else:
                for value in paths:
                    if not isinstance(value, str):
                        errors.append(
                            f"shared primitive {primitive['id']} has a non-string path"
                        )
                        continue
                    try:
                        safe_relative_path(value.rstrip("/**") or ".")
                    except ConfigurationError:
                        errors.append(
                            f"shared primitive {primitive['id']} has unsafe path {value!r}"
                        )
            if not isinstance(primitive.get("consumers"), list) or not primitive.get(
                "consumers"
            ):
                errors.append(
                    f"shared primitive {primitive['id']} requires consumers"
                )
            if not isinstance(primitive.get("invalidation"), str) or not str(
                primitive.get("invalidation", "")
            ).strip():
                errors.append(
                    f"shared primitive {primitive['id']} requires invalidation"
                )
    if len(primitive_ids) != len(set(primitive_ids)):
        errors.append("shared primitive ids must be unique")
    summary = {
        "component_count": len(components) if isinstance(components, list) else 0,
        "critical_flow_count": len(flows) if isinstance(flows, list) else 0,
        "shared_primitive_count": len(payload.get("shared_primitives", []))
        if isinstance(payload.get("shared_primitives", []), list)
        else 0,
        "unknowns": payload.get("unknowns", []),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return {
        "present": True,
        "valid": not errors,
        "fresh": fresh and not errors,
        "path": str(path),
        "relative_path": project.files.system_map,
        "errors": errors,
        "mapped_git_head": mapped_head,
        "current_git_head": git.get("head"),
        "current_working_tree_sha256": working_sha,
        "summary": summary,
        "content": payload if not errors else None,
    }


def load_system_map(project: ProjectConfig, git: dict[str, object]) -> dict[str, object]:
    return inspect_system_map_file(
        project, project.document_path(project.files.system_map), git
    )


def validate_test_evidence(
    *,
    run_root: Path,
    plan: dict[str, object],
    evidence: object,
    execution_receipts: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    if not isinstance(evidence, list):
        raise WorkflowError("Verification evidence must be a list")
    required_execute = plan.get("required_execution_classes", [])
    required_assess = plan.get("required_assessment_classes", [])
    if not isinstance(required_execute, list) or not isinstance(required_assess, list):
        raise WorkflowError("Assurance plan is malformed")
    covered: set[str] = set()
    assessed: set[str] = set()
    used_outputs: set[str] = set()
    used_executions: set[str] = set()
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(evidence):
        if not isinstance(row, dict):
            raise WorkflowError(f"Verification {index} must be a mapping")
        classes = row.get("classes", [row.get("class", "focused")])
        if not isinstance(classes, list) or not classes or not all(
            isinstance(item, str) and item in TEST_CLASSES for item in classes
        ) or len(classes) != len(set(classes)):
            raise WorkflowError(f"Verification {index} has invalid classes")
        status = row.get("status", "passed")
        if status == "not_applicable":
            rationale = row.get("rationale")
            if not isinstance(rationale, str) or len(rationale.strip()) < 20:
                raise WorkflowError(
                    f"Verification {index} not_applicable requires a concrete rationale"
                )
            assessed.update(classes)
            normalized.append({"classes": classes, "status": status, "rationale": rationale.strip()})
            continue
        if status != "passed":
            raise WorkflowError(f"Verification {index} status must be passed or not_applicable")
        execution_id: str | None = None
        receipt: dict[str, object] | None = None
        if execution_receipts is not None:
            raw_execution_id = row.get("execution_id")
            if not isinstance(raw_execution_id, str) or not raw_execution_id:
                raise WorkflowError(
                    f"Verification {index} requires an aria verify execution_id"
                )
            if raw_execution_id in used_executions:
                raise WorkflowError(
                    f"Verification {index} reuses execution_id {raw_execution_id}"
                )
            receipt = execution_receipts.get(raw_execution_id)
            if receipt is None:
                raise WorkflowError(
                    f"Verification {index} references unknown execution_id {raw_execution_id}"
                )
            receipt_classes = receipt.get("classes")
            if not isinstance(receipt_classes, list) or set(receipt_classes) != set(classes):
                raise WorkflowError(
                    f"Verification {index} classes differ from the execution receipt"
                )
            execution_id = raw_execution_id
            used_executions.add(raw_execution_id)
        command = row.get("command")
        if not isinstance(command, str) or not command.strip():
            raise WorkflowError(f"Verification {index} requires a command")
        if row.get("exit_code") != 0:
            raise WorkflowError(f"Verification {index} has no successful exit_code")
        if receipt is not None and (
            command.strip() != receipt.get("command")
            or row.get("exit_code") != receipt.get("exit_code")
            or row.get("output_path") != receipt.get("output_path")
            or row.get("output_sha256") != receipt.get("output_sha256")
        ):
            raise WorkflowError(
                f"Verification {index} differs from its immutable execution receipt"
            )
        relative = row.get("output_path")
        if not isinstance(relative, str):
            raise WorkflowError(f"Verification {index} requires output_path")
        normalized_path = safe_relative_path(relative)
        if normalized_path in used_outputs:
            raise WorkflowError(
                f"Verification {index} reuses output_path from another test class"
            )
        used_outputs.add(normalized_path)
        output = run_root.joinpath(*Path(normalized_path).parts)
        resolved = output.resolve(strict=False)
        if not resolved.is_relative_to(run_root.resolve(strict=True)) or not output.is_file():
            raise WorkflowError(f"Verification {index} output is missing or outside the run")
        content = output.read_bytes()
        actual_sha = hashlib.sha256(content).hexdigest()
        if row.get("output_sha256") != actual_sha:
            raise WorkflowError(f"Verification {index} output SHA mismatch")
        actual_result = row.get("actual_result")
        excerpt = row.get("output_excerpt")
        if not isinstance(actual_result, str) or not actual_result.strip():
            raise WorkflowError(f"Verification {index} requires actual_result read-back")
        if not isinstance(excerpt, str) or not excerpt.strip():
            raise WorkflowError(f"Verification {index} requires output_excerpt")
        decoded = content.decode("utf-8", errors="replace")
        if excerpt not in decoded:
            raise WorkflowError(f"Verification {index} output_excerpt is not in raw output")
        scenario = row.get("scenario")
        complex_classes = {
            "e2e",
            "adversarial",
            "concurrency",
            "load",
            "stress",
            "soak",
            "recovery",
            "chaos",
        }.intersection(classes)
        if complex_classes:
            required_scenario_fields = (
                "initial_state",
                "actions",
                "expected_result",
                "forbidden_result",
                "side_effects",
                "correlation",
                "parallelism_or_load",
                "actual_result",
            )
            if not isinstance(scenario, dict):
                raise WorkflowError(
                    f"Verification {index} classes {sorted(complex_classes)} require a linked scenario"
                )
            missing_scenario = [
                field
                for field in required_scenario_fields
                if scenario.get(field) is None
                or scenario.get(field) == ""
                or scenario.get(field) == []
            ]
            if missing_scenario:
                raise WorkflowError(
                    f"Verification {index} scenario is missing {missing_scenario}"
                )
        class_evidence = row.get("class_evidence")
        if not isinstance(class_evidence, dict) or set(class_evidence) != set(classes):
            raise WorkflowError(
                f"Verification {index} requires one class_evidence entry per declared class"
            )
        seen_proofs: set[str] = set()
        normalized_class_evidence: dict[str, object] = {}
        for test_class in classes:
            class_row = class_evidence.get(test_class)
            if not isinstance(class_row, dict):
                raise WorkflowError(
                    f"Verification {index} class {test_class} evidence must be a mapping"
                )
            proofs = class_row.get("proof_excerpts")
            minimum = 2 if test_class in complex_classes else 1
            if (
                not isinstance(proofs, list)
                or len(proofs) < minimum
                or not all(isinstance(item, str) and item.strip() for item in proofs)
                or len(proofs) != len(set(proofs))
            ):
                raise WorkflowError(
                    f"Verification {index} class {test_class} requires {minimum} distinct proof excerpt(s)"
                )
            if any(proof not in decoded for proof in proofs):
                raise WorkflowError(
                    f"Verification {index} class {test_class} proof is absent from raw output"
                )
            if seen_proofs.intersection(proofs):
                raise WorkflowError(
                    f"Verification {index} reuses the same proof for different classes"
                )
            seen_proofs.update(proofs)
            normalized_row: dict[str, object] = {"proof_excerpts": proofs}
            if test_class in {"concurrency", "load", "stress", "soak"}:
                metrics_excerpt = class_row.get("metrics_excerpt")
                if not isinstance(metrics_excerpt, str) or metrics_excerpt not in decoded:
                    raise WorkflowError(
                        f"Verification {index} class {test_class} requires metrics_excerpt from raw output"
                    )
                try:
                    metrics = json.loads(metrics_excerpt)
                except json.JSONDecodeError as error:
                    raise WorkflowError(
                        f"Verification {index} class {test_class} metrics_excerpt must be JSON"
                    ) from error
                numeric = (int, float)
                valid_metrics = (
                    isinstance(metrics, dict)
                    and isinstance(metrics.get("concurrency"), numeric)
                    and not isinstance(metrics.get("concurrency"), bool)
                    and metrics["concurrency"] >= 2
                    and isinstance(metrics.get("operations"), numeric)
                    and not isinstance(metrics.get("operations"), bool)
                    and metrics["operations"] >= 1
                    and isinstance(metrics.get("duration_seconds"), numeric)
                    and not isinstance(metrics.get("duration_seconds"), bool)
                    and metrics["duration_seconds"] > 0
                    and isinstance(metrics.get("error_rate"), numeric)
                    and not isinstance(metrics.get("error_rate"), bool)
                    and metrics["error_rate"] >= 0
                )
                if not valid_metrics:
                    raise WorkflowError(
                        f"Verification {index} class {test_class} metrics are incomplete"
                    )
                normalized_row["metrics_excerpt"] = metrics_excerpt
            normalized_class_evidence[test_class] = normalized_row
        covered.update(classes)
        assessed.update(classes)
        normalized_row = {
                "classes": classes,
                "status": "passed",
                "command": command.strip(),
                "exit_code": 0,
                "output_path": normalized_path,
                "output_sha256": actual_sha,
                "output_excerpt": excerpt,
                "actual_result": actual_result.strip(),
                "scenario": scenario,
                "class_evidence": normalized_class_evidence,
            }
        if execution_id is not None and receipt is not None:
            normalized_row.update(
                {
                    "execution_id": execution_id,
                    "requirement_ids": list(receipt.get("requirement_ids", [])),
                    "acceptance_ids": list(receipt.get("acceptance_ids", [])),
                }
            )
        normalized.append(normalized_row)
    missing_execute = sorted(set(map(str, required_execute)) - covered)
    if missing_execute:
        raise WorkflowError(f"Missing required executed test classes: {missing_execute}")
    missing_assess = sorted(set(map(str, required_assess)) - assessed)
    if missing_assess:
        raise WorkflowError(f"Missing required applicability assessments: {missing_assess}")
    return normalized
