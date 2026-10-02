from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime

import yaml

from aria.activity import parse_activity_event
from aria.collaboration import load_control_contract
from aria.activity_outbox import (
    cache_activity_identity,
    cached_activity_authorization,
    queue_request_applied,
    queue_request_matches_projection,
    queue_offline_activity_request,
)
from aria.collaborative_activity_runtime import submit_authenticated_activity
from aria.collaborative_backlog import (
    ACTION_PERMISSIONS,
    load_collaborative_backlog,
    parse_backlog_request,
)
from aria.collaborative_backlog_runtime import submit_authenticated_backlog_action
from aria.collaborative_runtime import (
    AuthenticatedCollaborativeAdapter,
    authorize_collaborative_actor,
)
from aria.control_worktree_sync import ControlWriter
from aria.errors import (
    AriaError,
    ConfigurationError,
    ProviderAdapterError,
    WorkflowError,
)
from aria.github_request_queue import (
    GitHubRequestIssue,
    GitHubRequestQueue,
    load_queue_request,
    validate_queue_request,
)
from aria.io import atomic_write_bytes, json_bytes
from aria.project import ProjectConfig
from aria.provider import (
    ProviderActor,
    ProviderInspection,
    ProviderTeamMember,
)
def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _operation_id(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _retryable_processing_error(project: ProjectConfig, error: AriaError) -> bool:
    if isinstance(error, ProviderAdapterError):
        return True
    if any(
        path.exists()
        for path in (
            project.runtime_root / "control-sync" / "pending.json",
            project.runtime_root / "control-sync" / "transaction.json",
            project.runtime_root / "state-closure" / "pending.json",
        )
    ):
        return True
    message = str(error).lower()
    return isinstance(error, WorkflowError) and any(
        token in message
        for token in (
            "git operation failed",
            "remote head",
            "network",
            "temporar",
            "timeout",
            "timed out",
            "recovery is pending",
            "audit is unavailable",
        )
    )


def _rejection_ledger_path(project: ProjectConfig):
    return project.runtime_root / "request-queue" / "terminal-rejections.json"


def _load_rejection_ledger(project: ProjectConfig) -> dict[str, object]:
    path = _rejection_ledger_path(project)
    if not path.exists():
        return {"schema_version": 1, "project_id": project.project_id, "issues": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("queue rejection ledger is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("project_id") != project.project_id
        or not isinstance(value.get("issues"), dict)
        or len(value["issues"]) > 10000
    ):
        raise ConfigurationError("queue rejection ledger is invalid")
    return value


def _was_terminally_rejected(
    project: ProjectConfig, issue: GitHubRequestIssue
) -> bool:
    row = _load_rejection_ledger(project)["issues"].get(str(issue.number))
    return (
        isinstance(row, dict)
        and row.get("issue_id") == issue.issue_id
        and row.get("body_sha256") == issue.body_sha256
    )


def _record_terminal_rejection(
    project: ProjectConfig, issue: GitHubRequestIssue, *, reason: str
) -> None:
    ledger = _load_rejection_ledger(project)
    issues = dict(ledger["issues"])
    issues[str(issue.number)] = {
        "issue_id": issue.issue_id,
        "body_sha256": issue.body_sha256,
        "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
    }
    if len(issues) > 10000:
        raise WorkflowError("queue rejection ledger reached its safety limit")
    atomic_write_bytes(
        _rejection_ledger_path(project),
        json_bytes({**ledger, "issues": issues}),
    )


def _reject_terminal(
    project: ProjectConfig,
    queue: GitHubRequestQueue,
    issue: GitHubRequestIssue,
    *,
    reason: str,
) -> None:
    """Persist rejection intent before the externally visible Issue close.

    If the process stops after either write, the ledger makes the next run
    finish the rejection instead of applying the request.
    """
    _record_terminal_rejection(project, issue, reason=reason)
    try:
        request = load_queue_request(issue.body)
        structured = issue.title == _request_title(request)
    except ConfigurationError:
        request = None
        structured = False
    if structured and request is not None:
        queue.comment(
            issue.number,
            "ARIA rejected "
            f"request_id={request['request_id']} "
            f"body_sha256={issue.body_sha256} "
            "without_apply=true "
            f"reason_sha256={hashlib.sha256(reason.encode('utf-8')).hexdigest()}",
        )
        queue.close(issue.number, state_reason="not_planned")
    else:
        queue.reject(issue.number, reason=reason)


def enqueue_backlog_action(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    queue: GitHubRequestQueue,
    expected_revision: int,
    action: str,
    item_id: str | None,
    payload: dict[str, object],
    request_id: str | None = None,
    submitted_at: str | None = None,
) -> dict[str, object]:
    online = True
    try:
        authorization = authorize_collaborative_actor(
            project, adapter=adapter, require_protection=False
        )
        cache_activity_identity(project, actor=authorization.actor)
    except ProviderAdapterError:
        authorization = cached_activity_authorization(project)
        online = False
    if action == "claim" and payload == {}:
        payload = {
            "branch": f"work/{authorization.actor.actor.username_snapshot}"
        }
    permission = ACTION_PERMISSIONS.get(action)
    if permission is None:
        raise ConfigurationError("backlog action is invalid")
    if permission not in authorization.permissions:
        raise WorkflowError(f"backlog action is not permitted: {action}")
    operation = {"action": action, "item_id": item_id, "payload": payload}
    operation_id = request_id or (
        f"backlog-r{expected_revision + 1}-"
        f"{_operation_id({'actor': authorization.actor.key, 'operation': operation, 'revision': expected_revision})}"
    )
    timestamp = submitted_at or _utc_now()
    parse_backlog_request(
        {
            "schema_version": 1,
            "request_id": operation_id,
            "correlation_id": f"correlation-{operation_id}",
            "project_id": project.project_id,
            **operation,
            "requested_at": timestamp,
        }
    )
    request = validate_queue_request(
        {
            "schema_version": 1,
            "request_id": operation_id,
            "project_id": project.project_id,
            "kind": "backlog",
            "expected_revision": expected_revision,
            "operation": operation,
            "submitted_at": timestamp,
        }
    )
    try:
        if not online:
            raise ProviderAdapterError("provider is offline")
        issue = queue.submit(request, expected_actor=authorization.actor.actor)
    except ProviderAdapterError:
        local = queue_offline_activity_request(
            project, request=request, actor=authorization.actor, queued_at=timestamp
        )
        return {
            "ok": True,
            "delivery": "local-runtime-outbox",
            "sync_status": "pending_sync",
            "project": project.project_id,
            "request_id": operation_id,
            **local,
        }
    return {
        "ok": True,
        "delivery": "github-issue-queue",
        "sync_status": "pending_sync",
        "project": project.project_id,
        "request_id": operation_id,
        "issue_number": issue.number,
        "issue_id": issue.issue_id,
        "body_sha256": issue.body_sha256,
    }


def enqueue_activity(
    project: ProjectConfig,
    *,
    adapter: AuthenticatedCollaborativeAdapter,
    queue: GitHubRequestQueue,
    expected_revision: int,
    task_id: str,
    stage: str,
    branch: str,
    note: str | None = None,
    event_id: str | None = None,
    submitted_at: str | None = None,
) -> dict[str, object]:
    online = True
    try:
        authorization = authorize_collaborative_actor(
            project, adapter=adapter, require_protection=False
        )
        cache_activity_identity(project, actor=authorization.actor)
    except ProviderAdapterError:
        authorization = cached_activity_authorization(project)
        online = False
    if "activity.write" not in authorization.permissions:
        raise WorkflowError("activity write is not permitted")
    operation = {
        "task_id": task_id,
        "stage": stage,
        "branch": branch,
        "note": note,
    }
    operation_id = event_id or (
        f"activity-r{expected_revision + 1}-"
        f"{_operation_id({'actor': authorization.actor.key, 'operation': operation, 'revision': expected_revision})}"
    )
    timestamp = submitted_at or _utc_now()
    parse_activity_event(
        {
            "schema_version": 1,
            "event_id": operation_id,
            "project_id": project.project_id,
            "task_id": task_id,
            "actor": {
                "provider": authorization.actor.provider,
                "user_id": authorization.actor.actor.user_id,
                "username_snapshot": authorization.actor.actor.username_snapshot,
            },
            "source": "local_aria",
            "stage": stage,
            "branch": branch,
            "pr_number": None,
            "note": note,
            "observed_at": timestamp,
        }
    )
    backlog = load_collaborative_backlog(project.docs_root / "BACKLOG.yaml")
    item = next((row for row in backlog["items"] if row["id"] == task_id), None)
    assignee = item["assignee"] if item is not None else None
    if (
        item is None
        or item["status"] == "done"
        or assignee is None
        or assignee["provider"] != authorization.actor.provider
        or assignee["user_id"] != authorization.actor.actor.user_id
    ):
        raise WorkflowError("only the active backlog assignee may queue activity")
    lease = item.get("lease")
    if (
        item["status"] not in {"in_progress", "blocked"}
        or not isinstance(lease, dict)
        or lease.get("branch") != branch
    ):
        raise WorkflowError("queued activity must use the active task lease branch")
    request = validate_queue_request(
        {
            "schema_version": 1,
            "request_id": operation_id,
            "project_id": project.project_id,
            "kind": "activity",
            "expected_revision": expected_revision,
            "operation": operation,
            "submitted_at": timestamp,
        }
    )
    try:
        if not online:
            raise ProviderAdapterError("provider is offline")
        issue = queue.submit(request, expected_actor=authorization.actor.actor)
    except ProviderAdapterError:
        local = queue_offline_activity_request(
            project, request=request, actor=authorization.actor, queued_at=timestamp
        )
        return {
            "ok": True,
            "delivery": "local-runtime-outbox",
            "sync_status": "pending_sync",
            "project": project.project_id,
            "event_id": operation_id,
            **local,
        }
    return {
        "ok": True,
        "delivery": "github-issue-queue",
        "sync_status": "pending_sync",
        "project": project.project_id,
        "event_id": operation_id,
        "issue_number": issue.number,
        "issue_id": issue.issue_id,
        "body_sha256": issue.body_sha256,
    }


class _IssueActorAdapter:
    provider_id = "github"

    def __init__(self, inspection: ProviderInspection) -> None:
        self._inspection = inspection

    def inspect_collaboration(self, *, repository_id: str, control_branch: str):
        return self._inspection


def _issue_inspection(
    issue: GitHubRequestIssue,
    *,
    members: tuple[ProviderTeamMember, ...],
    coordinator: ProviderInspection,
) -> ProviderInspection:
    member = next(
        (
            row
            for row in members
            if row.provider == "github" and row.actor.user_id == issue.author.user_id
        ),
        None,
    )
    if member is None:
        raise WorkflowError("queue request author is not a live repository collaborator")
    return ProviderInspection(
        provider="github",
        repository_id=coordinator.repository_id,
        actor=member.actor,
        membership=member.membership,
        protection=coordinator.protection,
    )


def _request_title(request: dict[str, object]) -> str:
    return f"ARIA request {request['project_id']}: {request['request_id']}"


def _acceptance_message(
    request: dict[str, object], issue: GitHubRequestIssue, control_commit: str
) -> str:
    return (
        "ARIA accepted "
        f"request_id={request['request_id']} "
        f"body_sha256={issue.body_sha256} "
        f"control_commit={control_commit}"
    )


def _control_commit_has_request(
    project: ProjectConfig,
    *,
    control_commit: str,
    request: dict[str, object],
    expected_actor: ProviderActor,
    allow_legacy_activity_receipt: bool = False,
) -> bool:
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    remote_ref = f"refs/remotes/{contract.remote}/{contract.control_branch}"
    try:
        ancestor = subprocess.run(
            [
                "git", "-C", str(project.docs_root), "merge-base", "--is-ancestor",
                control_commit, remote_ref,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        document_name = "BACKLOG.yaml" if request["kind"] == "backlog" else "ACTIVITY.yaml"
        content = subprocess.run(
            [
                "git", "-C", str(project.docs_root), "show",
                f"{control_commit}:{document_name}",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise WorkflowError("queue receipt control proof is unavailable") from error
    if ancestor.returncode != 0 or content.returncode != 0:
        raise WorkflowError("queue receipt commit is not in the verified control history")
    try:
        value = yaml.safe_load(content.stdout)
    except yaml.YAMLError as error:
        raise ConfigurationError("queue receipt control document is invalid") from error
    return queue_request_matches_projection(
        project,
        request,
        expected_actor=expected_actor,
        projection_value=value,
        allow_legacy_activity_receipt=allow_legacy_activity_receipt,
    )


def process_github_request_queue(
    project: ProjectConfig,
    *,
    coordinator_adapter: object,
    queue: GitHubRequestQueue,
    control_writer: ControlWriter,
    coordinator_integration_id: int,
    max_requests: int = 20,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if type(max_requests) is not int or not 1 <= max_requests <= 100:
        raise ConfigurationError("queue max_requests must be between 1 and 100")
    if not all(
        hasattr(coordinator_adapter, method)
        for method in ("inspect_collaboration", "list_collaborators")
    ):
        raise ConfigurationError("coordinator queue adapter is invalid")
    authorization = authorize_collaborative_actor(
        project, adapter=coordinator_adapter, require_protection=True
    )
    coordinator_inspection = coordinator_adapter.inspect_collaboration(
        repository_id=authorization.contract.repository_id,
        control_branch=authorization.contract.control_branch,
    )
    if "admin" not in coordinator_inspection.membership.roles:
        raise WorkflowError("queue processing requires repository admin membership")
    issues = queue.list_all(project_id=project.project_id)
    processed: list[dict[str, object]] = []
    for listed in issues:
        issue = queue.read(listed.number)
        if _was_terminally_rejected(project, issue):
            if issue.state == "closed" and issue.state_reason == "not_planned":
                continue
            if len(processed) >= max_requests:
                break
            reason = "recovering durable terminal queue rejection"
            _reject_terminal(project, queue, issue, reason=reason)
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": False,
                    "rejected": True,
                    "recovered": True,
                    "sync_status": "rejected",
                    "error": "WorkflowError",
                    "message": reason,
                }
            )
            continue
        if issue.state_reason == "reopened":
            if len(processed) >= max_requests:
                break
            reason = "terminal queue request was reopened"
            _reject_terminal(project, queue, issue, reason=reason)
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": False,
                    "rejected": True,
                    "sync_status": "rejected",
                    "error": "WorkflowError",
                    "message": reason,
                }
            )
            continue
        try:
            queue.verify_immutable(issue)
            request = load_queue_request(issue.body)
            if issue.title != _request_title(request):
                raise WorkflowError("queue issue identity changed before processing")
        except (ConfigurationError, WorkflowError) as error:
            if len(processed) >= max_requests:
                break
            if _retryable_processing_error(project, error):
                processed.append(
                    {
                        "issue_number": issue.number,
                        "ok": False,
                        "retryable": True,
                        "sync_status": "pending_sync",
                        "error": type(error).__name__,
                        "message": str(error),
                    }
                )
                continue
            _reject_terminal(project, queue, issue, reason=str(error))
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": False,
                    "rejected": True,
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
            continue
        acceptance_receipt = queue.acceptance_receipt(
            issue,
            request,
            coordinator_integration_id=coordinator_integration_id,
        )
        if acceptance_receipt is not None:
            try:
                control_proven = _control_commit_has_request(
                    project,
                    control_commit=acceptance_receipt,
                    request=request,
                    expected_actor=issue.author,
                    allow_legacy_activity_receipt=True,
                )
            except AriaError as error:
                processed.append(
                    {
                        "issue_number": issue.number,
                        "ok": False,
                        "retryable": True,
                        "sync_status": "pending_sync",
                        "error": type(error).__name__,
                        "message": str(error),
                    }
                )
                continue
            if not control_proven:
                processed.append(
                    {
                        "issue_number": issue.number,
                        "ok": False,
                        "retryable": True,
                        "sync_status": "pending_sync",
                        "error": "WorkflowError",
                        "message": "queue acceptance receipt has no complete request proof",
                    }
                )
            continue
        acceptance_intent = (
            queue.acceptance_intent(
                issue,
                request,
                coordinator_integration_id=coordinator_integration_id,
            )
            if issue.state == "open"
            else None
        )
        if acceptance_intent is not None:
            try:
                control_proven = _control_commit_has_request(
                    project,
                    control_commit=acceptance_intent,
                    request=request,
                    expected_actor=issue.author,
                    allow_legacy_activity_receipt=True,
                )
            except AriaError as error:
                processed.append(
                    {
                        "issue_number": issue.number,
                        "ok": False,
                        "retryable": True,
                        "sync_status": "pending_sync",
                        "error": type(error).__name__,
                        "message": str(error),
                    }
                )
                continue
            if not control_proven:
                processed.append(
                    {
                        "issue_number": issue.number,
                        "ok": False,
                        "retryable": True,
                        "sync_status": "pending_sync",
                        "error": "WorkflowError",
                        "message": "queue acceptance intent has no complete request proof",
                    }
                )
                continue
            if len(processed) >= max_requests:
                break
            queue.close(issue.number)
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": True,
                    "request_id": request["request_id"],
                    "kind": request["kind"],
                    "control_commit": acceptance_intent,
                    "applied": False,
                    "sync_status": "accepted",
                    "recovered": True,
                }
            )
            continue
        rejection_receipt = queue.rejection_receipt(
            issue,
            request,
            coordinator_integration_id=coordinator_integration_id,
        )
        if rejection_receipt is not None:
            _record_terminal_rejection(
                project,
                issue,
                reason=f"App rejection receipt {rejection_receipt}",
            )
            continue
        if queue_request_applied(project, request, expected_actor=issue.author):
            control_commit = control_writer.read_head()
            if len(processed) >= max_requests:
                break
            queue.comment(
                issue.number,
                _acceptance_message(request, issue, str(control_commit)),
            )
            if issue.state != "closed" or issue.state_reason != "completed":
                queue.close(issue.number)
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": True,
                    "request_id": request["request_id"],
                    "kind": request["kind"],
                    "control_commit": control_commit,
                    "applied": False,
                    "sync_status": "accepted",
                    "recovered": True,
                }
            )
            continue
        if len(processed) >= max_requests:
            break
        try:
            # Membership is intentionally refreshed for every issue.  A revoke
            # between two queue entries must take effect before the later one.
            members = coordinator_adapter.list_collaborators(
                repository_id=authorization.contract.repository_id
            )
            inspection = _issue_inspection(
                issue, members=members, coordinator=coordinator_inspection
            )
            actor_adapter = _IssueActorAdapter(inspection)
            operation = request["operation"]
            if not isinstance(operation, dict):
                raise ConfigurationError("queue operation is invalid")
            if request["kind"] == "backlog":
                if set(operation) != {"action", "item_id", "payload"}:
                    raise ConfigurationError("queued backlog operation is invalid")
                result = submit_authenticated_backlog_action(
                    project,
                    adapter=actor_adapter,
                    control_writer=control_writer,
                    coordinator_integration_id=coordinator_integration_id,
                    expected_revision=int(request["expected_revision"]),
                    action=str(operation["action"]),
                    item_id=operation["item_id"],
                    payload=operation["payload"],
                    request_id=str(request["request_id"]),
                    requested_at=str(request["submitted_at"]),
                    git_environment=git_environment,
                )
            elif request["kind"] == "activity":
                if set(operation) != {"task_id", "stage", "branch", "note"}:
                    raise ConfigurationError("queued activity operation is invalid")
                result = submit_authenticated_activity(
                    project,
                    adapter=actor_adapter,
                    control_writer=control_writer,
                    expected_revision=int(request["expected_revision"]),
                    task_id=str(operation["task_id"]),
                    stage=str(operation["stage"]),
                    branch=str(operation["branch"]),
                    note=operation["note"],
                    event_id=str(request["request_id"]),
                    observed_at=str(request["submitted_at"]),
                    git_environment=git_environment,
                )
            else:
                raise ConfigurationError("queue request kind is invalid")
        except ProviderAdapterError as error:
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": False,
                    "retryable": True,
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
            continue
        except AriaError as error:
            if _retryable_processing_error(project, error):
                processed.append(
                    {
                        "issue_number": issue.number,
                        "ok": False,
                        "retryable": True,
                        "sync_status": "pending_sync",
                        "error": type(error).__name__,
                        "message": str(error),
                    }
                )
                continue
            _reject_terminal(project, queue, issue, reason=str(error))
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": False,
                    "rejected": True,
                    "sync_status": "rejected",
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
            continue
        try:
            control_proven = _control_commit_has_request(
                project,
                control_commit=str(result.get("control_commit")),
                request=request,
                expected_actor=issue.author,
            )
        except AriaError as error:
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": False,
                    "retryable": True,
                    "sync_status": "pending_sync",
                    "error": type(error).__name__,
                    "message": str(error),
                }
            )
            continue
        if not control_proven:
            outcome_reason = str(result.get("reason") or "no control mutation")
            reason = f"queue request has no complete control proof: {outcome_reason}"
            if result.get("applied") is not False:
                processed.append(
                    {
                        "issue_number": issue.number,
                        "ok": False,
                        "retryable": True,
                        "sync_status": "pending_sync",
                        "error": "WorkflowError",
                        "message": reason,
                    }
                )
                continue
            _reject_terminal(project, queue, issue, reason=reason)
            processed.append(
                {
                    "issue_number": issue.number,
                    "ok": False,
                    "rejected": True,
                    "sync_status": "rejected",
                    "request_id": request["request_id"],
                    "kind": request["kind"],
                    "control_commit": result.get("control_commit"),
                    "applied": False,
                    "reason": outcome_reason,
                }
            )
            continue
        queue.comment(
            issue.number,
            _acceptance_message(request, issue, str(result["control_commit"])),
        )
        queue.close(issue.number)
        processed.append(
            {
                "issue_number": issue.number,
                "ok": True,
                "request_id": request["request_id"],
                "kind": request["kind"],
                "control_commit": result["control_commit"],
                "applied": result["applied"],
                "sync_status": "accepted",
            }
        )
    return {
        "ok": all(row["ok"] for row in processed),
        "project": project.project_id,
        "selected": len(issues),
        "processed": processed,
    }
