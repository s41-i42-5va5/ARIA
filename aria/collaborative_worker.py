from __future__ import annotations

from aria.activity import load_activity
from aria.collaboration import load_control_contract
from aria.collaborative_activity_runtime import publish_verified_github_activity
from aria.collaborative_backlog import load_collaborative_backlog
from aria.collaborative_request_queue import process_github_request_queue
from aria.collaborative_state import load_collaborative_state
from aria.collaborative_state_runtime import (
    recover_pending_state_closure,
    sync_open_pull_request_review,
    sync_accepted_pull_request,
)
from aria.control_worktree_sync import (
    ControlWriter,
    recover_pending_control_sync,
    refresh_control_worktree,
)
from aria.errors import AriaError, ConfigurationError
from aria.github_integration import GitHubIntegrationVerifier
from aria.github_request_queue import GitHubRequestQueue
from aria.project import ProjectConfig


def run_collaborative_coordinator_once(
    project: ProjectConfig,
    *,
    coordinator_adapter: object,
    request_queue: GitHubRequestQueue,
    control_writer: ControlWriter,
    integration_verifier: GitHubIntegrationVerifier,
    coordinator_integration_id: int,
    max_requests: int = 20,
    max_pull_requests: int = 20,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if type(max_pull_requests) is not int or not 1 <= max_pull_requests <= 100:
        raise ConfigurationError("coordinator pull request limit must be between 1 and 100")
    recovered_state = recover_pending_state_closure(
        project,
        coordinator_adapter=coordinator_adapter,
        verifier=integration_verifier,
        control_writer=control_writer,
        coordinator_integration_id=coordinator_integration_id,
        git_environment=git_environment,
    )
    recovered_sync = (
        None
        if recovered_state is not None
        else recover_pending_control_sync(
            project, writer=control_writer, git_environment=git_environment
        )
    )
    refreshed = (
        {
            "ok": True,
            "updated": False,
            "control_commit": (
                recovered_state["control_commit"]
                if recovered_state is not None
                else recovered_sync["control_commit"]
            ),
            "recovered_sync": True,
        }
        if recovered_state is not None or recovered_sync is not None
        else refresh_control_worktree(
            project, writer=control_writer, git_environment=git_environment
        )
    )
    requests = process_github_request_queue(
        project,
        coordinator_adapter=coordinator_adapter,
        queue=request_queue,
        control_writer=control_writer,
        coordinator_integration_id=coordinator_integration_id,
        max_requests=max_requests,
        git_environment=git_environment,
    )
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    activity_results: list[dict[str, object]] = []
    list_open = getattr(integration_verifier, "list_bound_open_pull_requests", None)
    open_candidates = (
        list_open(
            repository_id=contract.repository_id,
            integration_branch=contract.integration_branch,
            maximum=max_pull_requests,
        )
        if callable(list_open)
        else ()
    )
    for candidate in open_candidates:
        backlog_snapshot = load_collaborative_backlog(project.docs_root / "BACKLOG.yaml")
        item = next(
            (row for row in backlog_snapshot.get("items", []) if row["id"] == candidate.backlog_item_id),
            None,
        )
        try:
            if item is None or item.get("status") not in {"in_progress", "blocked", "in_review"}:
                raise ConfigurationError("open pull request has no active backlog task")
            assignee = item.get("assignee")
            lease = item.get("lease")
            if not isinstance(assignee, dict) or not isinstance(lease, dict):
                raise ConfigurationError("open pull request task lease is invalid")
            candidate = integration_verifier.verify_open(
                repository_id=contract.repository_id,
                integration_branch=contract.integration_branch,
                pull_request_number=candidate.number,
                backlog_item_id=candidate.backlog_item_id,
                expected_actor_id=str(assignee["user_id"]),
                expected_branch=str(lease["branch"]),
                scope_paths=list(lease["scope_paths"]),
            )
            sync_open_pull_request_review(
                project,
                coordinator_adapter=coordinator_adapter,
                candidate=candidate,
                control_writer=control_writer,
                coordinator_integration_id=coordinator_integration_id,
                expected_backlog_revision=int(backlog_snapshot["revision"]),
                git_environment=git_environment,
            )
        except AriaError as error:
            activity_results.append(
                {
                    "ok": False,
                    "pull_request_number": candidate.number,
                    "item_id": candidate.backlog_item_id,
                    "stage": "pre_merge_check",
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
            continue
        snapshot = load_activity(project.docs_root / "ACTIVITY.yaml")
        existing = next(
            (
                entry
                for entry in snapshot["active_work"]
                if entry["task_id"] == candidate.backlog_item_id
            ),
            None,
        )
        if (
            existing is not None
            and existing["pr_number"] == candidate.number
            and existing["stage"] in {"in_review", "waiting_for_ci"}
        ):
            activity_results.append(
                {
                    "ok": True,
                    "pull_request_number": candidate.number,
                    "item_id": candidate.backlog_item_id,
                    "stage": existing["stage"],
                    "applied": False,
                    "reason": "already_published",
                }
            )
            continue
        try:
            activity_results.append(
                publish_verified_github_activity(
                    project,
                    coordinator_adapter=coordinator_adapter,
                    candidate=candidate,
                    stage="in_review",
                    expected_revision=int(snapshot["revision"]),
                    control_writer=control_writer,
                    git_environment=git_environment,
                )
            )
        except AriaError as error:
            activity_results.append(
                {
                    "ok": False,
                    "pull_request_number": candidate.number,
                    "item_id": candidate.backlog_item_id,
                    "stage": "in_review",
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
    candidates = integration_verifier.list_bound_merged_pull_requests(
        integration_branch=contract.integration_branch,
        repository_id=contract.repository_id,
        maximum=1000,
    )
    state = load_collaborative_state(project.docs_root / "STATE.yaml", contract)
    accepted_prs = {event["pull_request_number"] for event in state["events"]}
    selected = [candidate for candidate in candidates if candidate.number not in accepted_prs]
    processed: list[dict[str, object]] = []
    for candidate in selected[:max_pull_requests]:
        waiting_failure: dict[str, object] | None = None
        if candidate.pull_request_author is not None and candidate.branch is not None:
            activity = load_activity(project.docs_root / "ACTIVITY.yaml")
            existing = next(
                (
                    entry
                    for entry in activity["active_work"]
                    if entry["task_id"] == candidate.backlog_item_id
                ),
                None,
            )
            if not (
                existing is not None
                and existing["pr_number"] == candidate.number
                and existing["stage"] == "waiting_for_ci"
            ):
                try:
                    activity_results.append(
                        publish_verified_github_activity(
                            project,
                            coordinator_adapter=coordinator_adapter,
                            candidate=candidate,
                            stage="waiting_for_ci",
                            expected_revision=int(activity["revision"]),
                            control_writer=control_writer,
                            git_environment=git_environment,
                        )
                    )
                except AriaError as error:
                    waiting_failure = {
                        "ok": False,
                        "pull_request_number": candidate.number,
                        "item_id": candidate.backlog_item_id,
                        "stage": "waiting_for_ci",
                        "error": type(error).__name__,
                        "message": str(error),
                    }
                    activity_results.append(waiting_failure)
        if waiting_failure is not None:
            processed.append(waiting_failure)
            break
        backlog = load_collaborative_backlog(project.docs_root / "BACKLOG.yaml")
        state = load_collaborative_state(project.docs_root / "STATE.yaml", contract)
        try:
            result = sync_accepted_pull_request(
                project,
                coordinator_adapter=coordinator_adapter,
                verifier=integration_verifier,
                control_writer=control_writer,
                coordinator_integration_id=coordinator_integration_id,
                item_id=candidate.backlog_item_id,
                pull_request_number=candidate.number,
                expected_backlog_revision=int(backlog["revision"]),
                expected_state_revision=int(state["revision"]),
                git_environment=git_environment,
            )
        except AriaError as error:
            processed.append(
                {
                    "ok": False,
                    "pull_request_number": candidate.number,
                    "item_id": candidate.backlog_item_id,
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
            break
        processed.append(
            {
                "ok": True,
                "pull_request_number": candidate.number,
                "item_id": candidate.backlog_item_id,
                "merge_commit": result["merge_commit"],
                "control_commit": result["control_commit"],
            }
        )
    return {
        "ok": (
            bool(requests["ok"])
            and all(row["ok"] for row in activity_results)
            and all(row["ok"] for row in processed)
        ),
        "project": project.project_id,
        "refresh": refreshed,
        "requests": requests,
        "activity": {
            "open_discovered": len(open_candidates),
            "processed": activity_results,
        },
        "pull_requests": {
            "discovered": len(candidates),
            "pending": len(selected),
            "processed": processed,
        },
    }
