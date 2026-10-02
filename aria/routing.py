from __future__ import annotations

from pathlib import PurePosixPath

from aria.errors import WorkflowError


INTENTS = {"auto", "design", "build", "review"}
MODES = {"auto", "quick", "standard", "deep"}
HIGH_RISK_FLAGS = {
    "schema",
    "security",
    "auth",
    "tenancy",
    "concurrency",
    "migration",
    "public-api",
    "multi-layer",
    "infrastructure",
    "data-loss",
    "safety",
}
HIGH_RISK_TERMS = {
    "schema",
    "схем",
    "security",
    "безопас",
    "auth",
    "авториза",
    "tenant",
    "tenancy",
    "concurr",
    "конкурент",
    "migration",
    "миграц",
    "public api",
    "публичн",
    "breaking",
    "multi-layer",
    "инфраструктур",
    "data loss",
    "потеря данных",
    "safety",
    "watchdog",
    "аварийн",
}
RESEARCH_TERMS = {
    "research",
    "исслед",
    "reference",
    "референс",
    "сравн",
    "standard",
    "стандарт",
    "актуальн",
    "внешн",
    "конкурент",
    "рынок",
    "protocol",
    "протокол",
    "документац",
}
ADR_TERMS = {
    "architecture",
    "архитект",
    "schema",
    "схем",
    "migration",
    "миграц",
    "security",
    "безопас",
    "public api",
    "публичн",
    "storage",
    "хранени",
    "dependency",
    "зависимост",
    "protocol",
    "протокол",
    "integration",
    "интеграц",
}
MEDIUM_SCOPE_TERMS = {
    "backend",
    "frontend",
    "api и ui",
    "api and ui",
    "cross-layer",
    "end-to-end",
    "интеграц",
    "согласованно",
    "несколько компонентов",
    "workflow",
    "pipeline",
    "воркер",
    "worker",
}
DESIGN_TERMS = {
    "design",
    "architecture",
    "architect",
    "проектир",
    "архитектур",
    "спроектир",
    "предложи решение",
}
REVIEW_TERMS = {
    "review",
    "audit",
    "ревью",
    "аудит",
    "проверь код",
    "проверь решение",
}


def _contains_any(text: str, terms: set[str]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _detect_intent(task: str) -> str:
    if _contains_any(task, REVIEW_TERMS):
        return "review"
    if _contains_any(task, DESIGN_TERMS):
        return "design"
    return "build"


def decide_run_route(
    *,
    task: str,
    intent: str,
    mode: str,
    changed_paths: list[str],
    risk_flags: list[str],
    spec_exists: bool,
    target_type: str | None = None,
) -> dict[str, object]:
    if intent not in INTENTS:
        raise WorkflowError(f"Unsupported intent: {intent!r}")
    if mode not in MODES:
        raise WorkflowError(f"Unsupported mode: {mode!r}")
    selected_intent = _detect_intent(task) if intent == "auto" else intent
    normalized_flags = {item.strip().lower() for item in risk_flags if item.strip()}
    detected = sorted(
        (normalized_flags & HIGH_RISK_FLAGS)
        | {term for term in HIGH_RISK_TERMS if term in task.lower()}
    )
    roots = {
        PurePosixPath(path.replace("\\", "/")).parts[0].lower()
        for path in changed_paths
        if PurePosixPath(path.replace("\\", "/")).parts
    }
    high_risk = bool(detected) or len(roots) >= 3 or target_type == "repository"
    reasons: list[str] = []
    if mode != "auto":
        selected_mode = mode
        reasons.append("explicit-mode")
    elif high_risk:
        selected_mode = "deep"
        reasons.extend([f"high-risk:{item}" for item in detected])
        if len(roots) >= 3:
            reasons.append("three-or-more-code-roots")
        if target_type == "repository":
            reasons.append("repository-review")
    elif (
        spec_exists
        or len(roots) == 2
        or len(task) > 180
        or _contains_any(task, MEDIUM_SCOPE_TERMS)
    ):
        selected_mode = "standard"
        reasons.append(
            "relevant-spec" if spec_exists else "medium-scope-or-description"
        )
    else:
        selected_mode = "quick"
        reasons.append("small-low-risk-scope")

    if selected_mode == "deep" and selected_intent == "design":
        mechanism = "spec"
    elif selected_mode == "deep" and selected_intent == "build":
        mechanism = "next-task-new"
    elif selected_intent == "review":
        mechanism = "scoped-review"
    else:
        mechanism = f"direct-{selected_mode}"
    warnings: list[str] = []
    if mode in {"quick", "standard"} and high_risk:
        warnings.append(
            "Explicit mode is below the automatic deep recommendation for this risk"
        )
    return {
        "intent": selected_intent,
        "mode": selected_mode,
        "mechanism": mechanism,
        "automatic": intent == "auto" or mode == "auto",
        "reasons": reasons,
        "risk_signals": detected,
        "warnings": warnings,
    }


def design_trace_assessment(task: str, route: dict[str, object]) -> dict[str, object]:
    research_reasons = sorted(term for term in RESEARCH_TERMS if term in task.lower())
    adr_reasons = sorted(term for term in ADR_TERMS if term in task.lower())
    deep_design = route.get("intent") == "design" and route.get("mode") == "deep"
    return {
        "required_for_deep_design": deep_design,
        "research": {
            "recommended": bool(research_reasons),
            "signals": research_reasons,
            "rule": (
                "Search when explicitly requested or when current external facts, "
                "standards, APIs, safety evidence or real alternatives affect the decision"
            ),
        },
        "adr": {
            "recommended": bool(adr_reasons),
            "signals": adr_reasons,
            "rule": (
                "Create an ADR only for a durable architectural choice with real "
                "alternatives or broad/expensive-to-reverse impact"
            ),
        },
    }
