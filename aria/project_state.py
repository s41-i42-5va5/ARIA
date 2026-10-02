from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath
from typing import Iterable

import yaml

from aria.errors import WorkflowError


STATE_PROFILES = {"frontier", "roadmap"}
TERMINAL_TASK_STATUSES = {"done", "dropped"}
ELIGIBLE_TASK_STATUSES = {"ready", "not_started", "backlog"}
ACTIVE_TASK_STATUSES = {"in_progress", "blocked"}
KNOWN_TASK_STATUSES = (
    TERMINAL_TASK_STATUSES
    | ELIGIBLE_TASK_STATUSES
    | {
        "in_progress",
        "blocked",
        "deferred",
    }
)
KNOWN_STAGE_STATUSES = {"planned", "in_progress", "blocked", "done"}


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise WorkflowError(f"{label} must be a mapping with string keys")
    return value


def parse_project_state(
    text: str,
    *,
    project_id: str,
    expected_profile: str,
) -> dict[str, object]:
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise WorkflowError(f"STATE.yaml is invalid YAML: {error}") from error
    state = _mapping(payload, "STATE.yaml")
    validation = validate_state_model(
        state,
        project_id=project_id,
        expected_profile=expected_profile,
    )
    if validation["errors"]:
        raise WorkflowError(
            "STATE.yaml model is invalid: " + "; ".join(validation["errors"])
        )
    return state


def state_profile(state: dict[str, object]) -> str:
    return "frontier" if state.get("schema_version") == 1 else str(state.get("profile"))


def state_current(state: dict[str, object]) -> dict[str, object]:
    key = "frontier" if state.get("schema_version") == 1 else "current"
    value = state.get(key, {})
    return value if isinstance(value, dict) else {}


def iter_stage_tasks(
    state: dict[str, object],
) -> Iterable[tuple[int, str, dict[str, object], dict[str, object]]]:
    stages = state.get("stages", [])
    if not isinstance(stages, list):
        return
    for stage_index, stage_value in enumerate(stages):
        if not isinstance(stage_value, dict):
            continue
        stage_id = stage_value.get("id")
        tasks = stage_value.get("tasks", [])
        if not isinstance(stage_id, str) or not isinstance(tasks, list):
            continue
        for task_value in tasks:
            if isinstance(task_value, dict):
                yield stage_index, stage_id, stage_value, task_value


def task_index(state: dict[str, object]) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for stage_index, stage_id, _stage, task in iter_stage_tasks(state):
        task_id = task.get("id")
        if isinstance(task_id, str) and task_id:
            rows[task_id] = {
                "task": task,
                "stage_id": stage_id,
                "stage_index": stage_index,
            }
    return rows


def validate_state_model(
    state: dict[str, object],
    *,
    project_id: str,
    expected_profile: str,
) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    schema = state.get("schema_version")
    if state.get("project_id") != project_id:
        errors.append("project_id does not match PROJECT.yaml")
    if schema == 1:
        if expected_profile != "frontier":
            errors.append("schema v1 can only use the frontier profile")
        if not isinstance(state.get("frontier"), dict):
            errors.append("schema v1 frontier must be a mapping")
        return {
            "ok": not errors,
            "schema_version": schema,
            "profile": "frontier",
            "stage_count": 0,
            "task_count": 0,
            "errors": errors,
            "warnings": warnings,
        }
    if schema != 2:
        errors.append(f"unsupported schema_version: {schema!r}")
        return {
            "ok": False,
            "schema_version": schema,
            "profile": None,
            "stage_count": 0,
            "task_count": 0,
            "errors": errors,
            "warnings": warnings,
        }

    profile = state.get("profile")
    if profile not in STATE_PROFILES:
        errors.append(f"profile must be one of {sorted(STATE_PROFILES)}")
    if profile != expected_profile:
        errors.append(
            f"profile does not match PROJECT.yaml: {profile!r}!={expected_profile!r}"
        )
    for key in ("focus", "current", "issues"):
        if not isinstance(state.get(key), dict):
            errors.append(f"{key} must be a mapping")
    stages = state.get("stages")
    if not isinstance(stages, list):
        errors.append("stages must be a list")
        stages = []

    ids: set[str] = set()
    stage_ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    task_statuses: dict[str, str] = {}
    task_count = 0
    for stage_index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            errors.append(f"stages[{stage_index}] must be a mapping")
            continue
        stage_id = stage.get("id")
        if not isinstance(stage_id, str) or not stage_id:
            errors.append(f"stages[{stage_index}].id is missing")
            continue
        if stage_id in stage_ids:
            errors.append(f"duplicate stage id: {stage_id}")
        stage_ids.add(stage_id)
        stage_status = stage.get("status")
        if stage_status not in KNOWN_STAGE_STATUSES:
            errors.append(f"stage {stage_id} has unsupported status: {stage_status!r}")
        tasks = stage.get("tasks", [])
        if not isinstance(tasks, list):
            errors.append(f"stage {stage_id} tasks must be a list")
            continue
        for task_position, task in enumerate(tasks):
            task_count += 1
            if not isinstance(task, dict):
                errors.append(
                    f"stage {stage_id} task {task_position} must be a mapping"
                )
                continue
            task_id = task.get("id")
            if not isinstance(task_id, str) or not task_id:
                errors.append(f"stage {stage_id} task {task_position} has no id")
                continue
            if task_id in ids:
                errors.append(f"duplicate task id: {task_id}")
            ids.add(task_id)
            status = task.get("status")
            if status not in KNOWN_TASK_STATUSES:
                errors.append(f"task {task_id} has unsupported status: {status!r}")
            else:
                task_statuses[task_id] = str(status)
            raw_dependencies = task.get("depends_on", [])
            if not isinstance(raw_dependencies, list) or not all(
                isinstance(item, str) and item for item in raw_dependencies
            ):
                errors.append(f"task {task_id} depends_on must be a string list")
                raw_dependencies = []
            dependencies[task_id] = list(dict.fromkeys(raw_dependencies))
            priority = task.get("priority")
            if not isinstance(priority, int) or priority < 0:
                errors.append(f"task {task_id} priority must be a non-negative integer")
            spec = task.get("spec")
            if spec is not None and (not isinstance(spec, str) or not spec.strip()):
                errors.append(f"task {task_id} spec must be a path or null")
            adrs = task.get("adr", [])
            if not isinstance(adrs, list) or not all(
                isinstance(item, str) and item.strip() for item in adrs
            ):
                errors.append(f"task {task_id} adr must be a string list")
            references = task.get("references", [])
            if not isinstance(references, list) or not all(
                isinstance(item, str) and item.strip() for item in references
            ):
                errors.append(f"task {task_id} references must be a string list")
            trace = task.get("trace")
            if trace is not None:
                if not isinstance(trace, dict):
                    errors.append(f"task {task_id} trace must be a mapping")
                else:
                    _validate_task_trace(task_id, trace, errors)
            safety_impact = task.get("safety_impact")
            if safety_impact is not None and safety_impact not in {
                "none",
                "low",
                "medium",
                "high",
                "critical",
            }:
                errors.append(
                    f"task {task_id} safety_impact has unsupported value: "
                    f"{safety_impact!r}"
                )
        if stage_status == "done" and any(
            isinstance(task, dict) and task.get("status") not in TERMINAL_TASK_STATUSES
            for task in tasks
        ):
            errors.append(f"done stage {stage_id} still contains open tasks")

    for task_id, task_dependencies in dependencies.items():
        for dependency in task_dependencies:
            if dependency not in ids:
                errors.append(f"task {task_id} depends on unknown task {dependency}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            errors.append(f"dependency cycle contains task {task_id}")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies.get(task_id, []):
            if dependency in dependencies:
                visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in dependencies:
        visit(task_id)

    focus = state.get("focus") if isinstance(state.get("focus"), dict) else {}
    focus_stage = focus.get("stage_id")
    if focus_stage is not None and focus_stage not in stage_ids:
        errors.append(f"focus refers to unknown stage {focus_stage}")
    if focus_stage is not None:
        focus_rows = [
            stage
            for stage in stages
            if isinstance(stage, dict) and stage.get("id") == focus_stage
        ]
        if focus_rows and focus_rows[0].get("status") != "in_progress":
            errors.append("focus stage must have status in_progress")
    ordered_tasks = focus.get("ordered_tasks", [])
    if not isinstance(ordered_tasks, list) or not all(
        isinstance(item, str) and item for item in ordered_tasks
    ):
        errors.append("focus.ordered_tasks must be a string list")
        ordered_tasks = []
    for task_id in ordered_tasks:
        if task_id not in ids:
            errors.append(f"focus refers to unknown task {task_id}")
    focus_spec = focus.get("spec")
    if focus_spec is not None and (not isinstance(focus_spec, str) or not focus_spec):
        errors.append("focus.spec must be a path or null")

    current = state.get("current") if isinstance(state.get("current"), dict) else {}
    current_id = current.get("task_id")
    if current_id is None and current.get("status") is not None:
        errors.append("current status must be null when task_id is null")
    if current_id is not None:
        if current_id not in ids and profile == "roadmap":
            errors.append(f"current refers to unknown roadmap task {current_id}")
        current_status = current.get("status")
        if current_status not in ACTIVE_TASK_STATUSES:
            errors.append("current task status must be in_progress or blocked")
        roadmap_status = task_statuses.get(str(current_id))
        if roadmap_status is not None and roadmap_status != current_status:
            errors.append(
                f"current status disagrees with task {current_id}: "
                f"{current_status!r}!={roadmap_status!r}"
            )
    if profile == "frontier" and stages:
        warnings.append(
            "frontier profile has roadmap stages; they are not auto-selected"
        )
    if profile == "roadmap" and not stages:
        errors.append("roadmap profile requires at least one stage")

    return {
        "ok": not errors,
        "schema_version": schema,
        "profile": profile,
        "stage_count": len(stage_ids),
        "task_count": task_count,
        "errors": list(dict.fromkeys(errors)),
        "warnings": list(dict.fromkeys(warnings)),
    }


def _task_title(task: dict[str, object]) -> str:
    title = task.get("title")
    return (
        str(title).strip()
        if isinstance(title, str) and title.strip()
        else str(task["id"])
    )


def _validate_task_trace(
    task_id: str, trace: dict[str, object], errors: list[str]
) -> None:
    spec = trace.get("spec")
    if spec is not None:
        if not isinstance(spec, dict):
            errors.append(f"task {task_id} trace.spec must be a mapping or null")
        else:
            _validate_trace_artifact(task_id, "spec", spec, errors)
            revision = spec.get("revision")
            if not isinstance(revision, int) or revision < 1:
                errors.append(
                    f"task {task_id} trace.spec revision must be a positive integer"
                )
    for key in ("adrs", "research"):
        artifacts = trace.get(key, [])
        if not isinstance(artifacts, list):
            errors.append(f"task {task_id} trace.{key} must be a list")
            continue
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, dict):
                errors.append(f"task {task_id} trace.{key}[{index}] must be a mapping")
                continue
            _validate_trace_artifact(task_id, f"{key}[{index}]", artifact, errors)
    reference_rows = trace.get("references", [])
    if not isinstance(reference_rows, list):
        errors.append(f"task {task_id} trace.references must be a list")
    else:
        for index, reference in enumerate(reference_rows):
            if not isinstance(reference, dict):
                errors.append(
                    f"task {task_id} trace.references[{index}] must be a mapping"
                )
                continue
            for key in ("id", "url", "title", "accessed_at"):
                if (
                    not isinstance(reference.get(key), str)
                    or not str(reference[key]).strip()
                ):
                    errors.append(
                        f"task {task_id} trace.references[{index}].{key} is required"
                    )
    implementation = trace.get("implementation")
    if implementation is not None:
        if not isinstance(implementation, dict):
            errors.append(f"task {task_id} trace.implementation must be a mapping")
        else:
            for key in ("run_id", "base_commit", "head_commit"):
                if (
                    not isinstance(implementation.get(key), str)
                    or not str(implementation[key]).strip()
                ):
                    errors.append(
                        f"task {task_id} trace.implementation.{key} is required"
                    )
            for key in ("commits", "changed_files"):
                values = implementation.get(key)
                if not isinstance(values, list) or not all(
                    isinstance(item, str) and item.strip() for item in values
                ):
                    errors.append(
                        f"task {task_id} trace.implementation.{key} must be a string list"
                    )
    legacy_implementation = trace.get("legacy_implementation")
    if legacy_implementation is not None:
        if not isinstance(legacy_implementation, dict):
            errors.append(
                f"task {task_id} trace.legacy_implementation must be a mapping"
            )
        else:
            if legacy_implementation.get("completeness") not in {
                "pointer_only",
                "listed_commits",
            }:
                errors.append(
                    f"task {task_id} trace.legacy_implementation.completeness is invalid"
                )
            source = legacy_implementation.get("source")
            if not isinstance(source, str) or not source.strip():
                errors.append(
                    f"task {task_id} trace.legacy_implementation.source is required"
                )
            commits = legacy_implementation.get("commits")
            if (
                not isinstance(commits, list)
                or not commits
                or not all(isinstance(item, str) and item.strip() for item in commits)
            ):
                errors.append(
                    f"task {task_id} trace.legacy_implementation.commits must be a non-empty string list"
                )
    history = trace.get("history")
    if history is not None:
        if not isinstance(history, dict):
            errors.append(f"task {task_id} trace.history must be a mapping")
        elif not isinstance(history.get("sequence"), int) or not isinstance(
            history.get("event_sha256"), str
        ):
            errors.append(
                f"task {task_id} trace.history requires sequence and event_sha256"
            )


def _validate_trace_artifact(
    task_id: str,
    label: str,
    artifact: dict[str, object],
    errors: list[str],
) -> None:
    for key in ("id", "path", "sha256"):
        if not isinstance(artifact.get(key), str) or not str(artifact[key]).strip():
            errors.append(f"task {task_id} trace.{label}.{key} is required")


def _selection(
    task: dict[str, object],
    *,
    stage_id: str,
    source: str,
    skipped: list[dict[str, str]],
) -> dict[str, object]:
    spec = task.get("spec")
    return {
        "task_id": task["id"],
        "task": _task_title(task),
        "stage_id": stage_id,
        "status": task.get("status"),
        "priority": task.get("priority"),
        "depends_on": list(task.get("depends_on", [])),
        "spec": spec if isinstance(spec, str) else None,
        "adr": list(task.get("adr", [])),
        "references": list(task.get("references", [])),
        "trace": task.get("trace"),
        "safety_impact": task.get("safety_impact"),
        "source": source,
        "skipped": skipped,
    }


def _eligible(
    task: dict[str, object],
    *,
    index: dict[str, dict[str, object]],
) -> tuple[bool, str | None]:
    status = task.get("status")
    if status in TERMINAL_TASK_STATUSES:
        return False, str(status)
    if status == "deferred":
        return False, "deferred"
    if status == "blocked":
        return False, "blocked"
    if status == "in_progress":
        return True, None
    if status not in ELIGIBLE_TASK_STATUSES:
        return False, f"status={status}"
    waiting = [
        dependency
        for dependency in task.get("depends_on", [])
        if index.get(str(dependency), {}).get("task", {}).get("status") != "done"
    ]
    if waiting:
        return False, "waiting:" + ",".join(map(str, waiting))
    return True, None


def select_next_task(state: dict[str, object]) -> dict[str, object] | None:
    if state_profile(state) != "roadmap":
        return None
    index = task_index(state)
    current = state_current(state)
    current_id = current.get("task_id")
    if isinstance(current_id, str) and current.get("status") == "in_progress":
        row = index[current_id]
        return _selection(
            row["task"],
            stage_id=str(row["stage_id"]),
            source="current",
            skipped=[],
        )
    if isinstance(current_id, str) and current.get("status") == "blocked":
        return {
            "blocked": True,
            "task_id": current_id,
            "task": current.get("task_summary") or current_id,
            "stage_id": current.get("stage_id"),
            "reason": current.get("next_action") or "current task is blocked",
            "source": "current",
            "skipped": [],
        }

    skipped: list[dict[str, str]] = []
    focus = state.get("focus") if isinstance(state.get("focus"), dict) else {}
    for task_id in focus.get("ordered_tasks", []):
        row = index.get(str(task_id))
        if row is None:
            continue
        eligible, reason = _eligible(row["task"], index=index)
        if eligible:
            return _selection(
                row["task"],
                stage_id=str(row["stage_id"]),
                source="focus",
                skipped=skipped,
            )
        skipped.append({"task_id": str(task_id), "reason": str(reason)})

    focus_stage = focus.get("stage_id")
    stages = state.get("stages", [])
    candidate_stages = [
        stage
        for stage in stages
        if isinstance(stage, dict)
        and (
            stage.get("id") == focus_stage
            if focus_stage is not None
            else stage.get("status") == "in_progress"
        )
    ]
    for stage in candidate_stages:
        candidates: list[tuple[int, int, dict[str, object]]] = []
        for position, task in enumerate(stage.get("tasks", [])):
            if not isinstance(task, dict):
                continue
            eligible, reason = _eligible(task, index=index)
            if eligible:
                candidates.append((int(task.get("priority", 0)), position, task))
            elif task.get("status") not in TERMINAL_TASK_STATUSES:
                skipped.append({"task_id": str(task.get("id")), "reason": str(reason)})
        if candidates:
            _priority, _position, task = min(
                candidates, key=lambda row: (row[0], row[1])
            )
            return _selection(
                task,
                stage_id=str(stage["id"]),
                source="stage-priority",
                skipped=skipped,
            )
        if focus_stage is not None:
            break
    return None


def find_task(state: dict[str, object], task_id: str) -> dict[str, object] | None:
    row = task_index(state).get(task_id)
    if row is None:
        return None
    return _selection(
        row["task"],
        stage_id=str(row["stage_id"]),
        source="explicit",
        skipped=[],
    )


def state_projection(
    state: dict[str, object],
    *,
    selected: dict[str, object] | None = None,
) -> dict[str, object]:
    if state.get("schema_version") == 1:
        return state
    stages = state.get("stages", [])
    stage_summaries: list[dict[str, object]] = []
    selected_stage = selected.get("stage_id") if isinstance(selected, dict) else None
    focus = state.get("focus") if isinstance(state.get("focus"), dict) else {}
    current = state_current(state)
    active_stage = focus.get("stage_id") or current.get("stage_id") or selected_stage
    active_stage_detail: dict[str, object] | None = None
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        tasks = [task for task in stage.get("tasks", []) if isinstance(task, dict)]
        counts = dict(Counter(str(task.get("status")) for task in tasks))
        summary = {
            "id": stage.get("id"),
            "title": stage.get("title"),
            "status": stage.get("status"),
            "task_counts": counts,
        }
        stage_summaries.append(summary)
        if stage.get("id") == active_stage:
            active_stage_detail = {
                **summary,
                "exit_criteria": stage.get("exit_criteria", []),
            }
    selected_task: dict[str, object] | None = None
    if isinstance(selected, dict):
        selected_task = {
            key: selected.get(key)
            for key in (
                "task_id",
                "task",
                "stage_id",
                "status",
                "priority",
                "depends_on",
                "spec",
                "adr",
                "references",
                "trace",
                "safety_impact",
                "source",
            )
        }
        if selected.get("blocked"):
            selected_task["blocked"] = True
            selected_task["reason"] = selected.get("reason")
    return {
        "schema_version": 2,
        "project_id": state.get("project_id"),
        "profile": state.get("profile"),
        "focus": focus,
        "current": current,
        "selected_task": selected_task,
        "active_stage": active_stage_detail,
        "stages": stage_summaries,
        "issues": state.get("issues", {}),
        "last_verified": state.get("last_verified"),
        "last_completed": state.get("last_completed"),
        "history_checkpoint": state.get("history_checkpoint"),
    }


def projection_yaml(
    state: dict[str, object],
    *,
    selected: dict[str, object] | None = None,
) -> str:
    return yaml.safe_dump(
        state_projection(state, selected=selected),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def roadmap_overview(state: dict[str, object]) -> dict[str, object] | None:
    if state_profile(state) != "roadmap":
        return None
    projection = state_projection(state, selected=select_next_task(state))
    return {
        "focus": projection["focus"],
        "current": projection["current"],
        "next_task": projection["selected_task"],
        "active_stage": projection["active_stage"],
        "stages": projection["stages"],
    }


def normalize_spec_path(path: str, specs_root: str) -> str:
    normalized = path.replace("\\", "/").strip()
    candidate = PurePosixPath(normalized)
    root = PurePosixPath(specs_root)
    if candidate.suffix.lower() != ".md" or not candidate.is_relative_to(root):
        raise WorkflowError(f"Task spec must be a Markdown file under {root}: {path}")
    return candidate.as_posix()
