from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from aria.activity import EVENT_ID_RE
from aria.errors import ConfigurationError, WorkflowError
from aria.github import GitHubMutationTransport, GitHubRepository
from aria.project import PROJECT_ID_RE
from aria.provider import ProviderActor


MAX_REQUEST_BODY_BYTES = 64 * 1024
REQUEST_KINDS = {"backlog", "activity"}
QUEUE_LABEL = "aria:request"
QUEUE_KIND_LABELS = {
    "backlog": "aria:backlog",
    "activity": "aria:activity",
}


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"GitHub queue {label} must be a mapping")
    return value


def _string(value: object, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ConfigurationError(f"GitHub queue {label} is invalid")
    return value


def _stamp(value: object, label: str) -> str:
    from datetime import UTC, datetime

    text = _string(value, label, maximum=64)
    if not text.endswith("Z"):
        raise ConfigurationError(f"GitHub queue {label} must be UTC")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"GitHub queue {label} is invalid") from error
    if parsed.astimezone(UTC) != parsed:
        raise ConfigurationError(f"GitHub queue {label} must be UTC")
    return text


def _body(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_REQUEST_BODY_BYTES
        or "\x00" in value
    ):
        raise ConfigurationError("GitHub queue issue body is invalid")
    return value


def validate_queue_request(value: object) -> dict[str, object]:
    request = _mapping(value, "request")
    if set(request) != {
        "schema_version",
        "request_id",
        "project_id",
        "kind",
        "expected_revision",
        "operation",
        "submitted_at",
    }:
        raise ConfigurationError("GitHub queue request schema is invalid")
    if type(request.get("schema_version")) is not int or request["schema_version"] != 1:
        raise ConfigurationError("GitHub queue request schema_version must be 1")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or EVENT_ID_RE.fullmatch(request_id) is None:
        raise ConfigurationError("GitHub queue request_id is invalid")
    project_id = request.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError("GitHub queue project_id is invalid")
    if request.get("kind") not in REQUEST_KINDS:
        raise ConfigurationError("GitHub queue request kind is invalid")
    revision = request.get("expected_revision")
    if type(revision) is not int or revision < 0:
        raise ConfigurationError("GitHub queue expected_revision is invalid")
    operation = _mapping(request.get("operation"), "operation")
    if not operation:
        raise ConfigurationError("GitHub queue operation is empty")
    _stamp(request.get("submitted_at"), "submitted_at")
    encoded = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BODY_BYTES:
        raise ConfigurationError("GitHub queue request exceeds the size limit")
    return request


def dump_queue_request(value: object) -> str:
    request = validate_queue_request(value)
    return json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def load_queue_request(content: str) -> dict[str, object]:
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_REQUEST_BODY_BYTES:
        raise ConfigurationError("GitHub queue request body is invalid")
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ConfigurationError("GitHub queue request body is not valid JSON") from error
    return validate_queue_request(value)


@dataclass(frozen=True)
class GitHubRequestIssue:
    number: int
    issue_id: int
    node_id: str
    state: str
    title: str
    body: str
    author: ProviderActor
    updated_at: str
    state_reason: str | None
    labels: tuple[str, ...] = ()

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


class GitHubRequestQueue:
    def __init__(
        self,
        *,
        repository: GitHubRepository,
        transport: GitHubMutationTransport,
    ) -> None:
        if not isinstance(repository, GitHubRepository):
            raise ConfigurationError("GitHub queue repository is invalid")
        if not all(
            hasattr(transport, method)
            for method in ("get_json", "post_json", "patch_json")
        ):
            raise ConfigurationError("GitHub queue transport is invalid")
        self.repository = repository
        self._transport = transport

    @staticmethod
    def _issue(value: object) -> GitHubRequestIssue:
        issue = _mapping(value, "issue")
        number = issue.get("number")
        issue_id = issue.get("id")
        if type(number) is not int or number <= 0 or type(issue_id) is not int or issue_id <= 0:
            raise ConfigurationError("GitHub queue issue identity is invalid")
        user = _mapping(issue.get("user"), "issue author")
        node_id = _string(issue.get("node_id"), "issue node id", maximum=128)
        user_id = user.get("id")
        if type(user_id) is not int or user_id <= 0:
            raise ConfigurationError("GitHub queue issue author id is invalid")
        state_reason = issue.get("state_reason")
        if state_reason is not None and state_reason not in {"completed", "not_planned", "reopened"}:
            raise ConfigurationError("GitHub queue issue state reason is invalid")
        labels_value = issue.get("labels", [])
        if not isinstance(labels_value, list):
            raise ConfigurationError("GitHub queue issue labels are invalid")
        labels: list[str] = []
        for value in labels_value:
            name = value.get("name") if isinstance(value, dict) else value
            if not isinstance(name, str) or not name or name in labels:
                raise ConfigurationError("GitHub queue issue labels are invalid")
            labels.append(name)
        return GitHubRequestIssue(
            number=number,
            issue_id=issue_id,
            node_id=node_id,
            state=_string(issue.get("state"), "issue state", maximum=16),
            title=_string(issue.get("title"), "issue title", maximum=256),
            body=_body(issue.get("body")),
            author=ProviderActor(
                user_id=str(user_id),
                username_snapshot=_string(user.get("login"), "author login", maximum=128),
                display_name_snapshot=None,
            ),
            updated_at=_stamp(issue.get("updated_at"), "issue updated_at"),
            state_reason=state_reason,
            labels=tuple(sorted(labels)),
        )

    @staticmethod
    def _title(project_id: str, request_id: str) -> str:
        return f"ARIA request {project_id}: {request_id}"

    def submit(
        self,
        request_value: object,
        *,
        expected_actor: ProviderActor,
    ) -> GitHubRequestIssue:
        request = validate_queue_request(request_value)
        body = dump_queue_request(request)
        title = self._title(str(request["project_id"]), str(request["request_id"]))
        expected_labels = tuple(
            sorted((QUEUE_LABEL, QUEUE_KIND_LABELS[str(request["kind"])]))
        )
        existing = next(
            (
                issue
                for issue in self._list_requests(
                    project_id=str(request["project_id"]), state="all"
                )
                if issue.title == title
            ),
            None,
        )
        if existing is not None:
            self.verify_immutable(existing)
            if existing.state_reason == "reopened":
                raise WorkflowError("GitHub queue terminal request was reopened")
            if (
                existing.body != body
                or existing.author.user_id != expected_actor.user_id
            ):
                raise WorkflowError("GitHub queue request identity is already in use")
            return existing
        issue = self._issue(
            self._transport.post_json(
                f"{self.repository.api_path}/issues",
                {"title": title, "body": body, "labels": list(expected_labels)},
            )
        )
        if (
            issue.state != "open"
            or issue.title != title
            or issue.body != body
            or issue.author.user_id != expected_actor.user_id
            or issue.labels != expected_labels
        ):
            raise WorkflowError("GitHub queue issue read-back mismatch")
        self.verify_immutable(issue)
        return issue

    def read(self, issue_number: int) -> GitHubRequestIssue:
        if type(issue_number) is not int or issue_number <= 0:
            raise ConfigurationError("GitHub queue issue number is invalid")
        return self._issue(
            self._transport.get_json(
                f"{self.repository.api_path}/issues/{issue_number}"
            )
        )

    def verify_immutable(self, issue: GitHubRequestIssue) -> None:
        """Reject queue issues whose user-controlled content was ever edited.

        Repository collaborators may edit another user's issue through GitHub.
        The GraphQL audit field remains populated even when content is changed
        back to its original bytes, so REST-only body comparisons are not an
        adequate authentication boundary.
        """
        if not isinstance(issue, GitHubRequestIssue):
            raise ConfigurationError("GitHub queue immutable issue is invalid")
        response = _mapping(
            self._transport.post_json(
                "/graphql",
                {
                    "query": (
                        "query($id:ID!){node(id:$id){... on Issue{"
                        "id number title body state lastEditedAt author{login}}}}"
                    ),
                    "variables": {"id": issue.node_id},
                },
            ),
            "GraphQL response",
        )
        if response.get("errors") is not None:
            raise WorkflowError("GitHub queue issue edit audit is unavailable")
        node = _mapping(
            _mapping(response.get("data"), "GraphQL data").get("node"),
            "GraphQL issue",
        )
        author = _mapping(node.get("author"), "GraphQL issue author")
        if (
            node.get("id") != issue.node_id
            or node.get("number") != issue.number
            or node.get("title") != issue.title
            or node.get("body") != issue.body
            or str(node.get("state", "")).casefold() != issue.state.casefold()
            or str(author.get("login", "")).casefold()
            != issue.author.username_snapshot.casefold()
        ):
            raise WorkflowError("GitHub queue issue GraphQL read-back mismatch")
        if node.get("lastEditedAt") is not None:
            raise WorkflowError("GitHub queue issue was edited after creation")

    def _acceptance_commit(
        self,
        issue: GitHubRequestIssue,
        request: dict[str, object],
        *,
        coordinator_integration_id: int,
    ) -> str | None:
        prefix = (
            f"ARIA accepted request_id={request['request_id']} "
            f"body_sha256={issue.body_sha256} control_commit="
        )
        page = 1
        while True:
            values = self._transport.get_json(
                f"{self.repository.api_path}/issues/{issue.number}/comments"
                f"?per_page=100&page={page}"
            )
            if not isinstance(values, list):
                raise ConfigurationError("GitHub queue comments list is invalid")
            for value in values:
                body = value.get("body") if isinstance(value, dict) else None
                if not isinstance(body, str) or not body.startswith(prefix):
                    continue
                app = value.get("performed_via_github_app")
                if not isinstance(app, dict) or app.get("id") != coordinator_integration_id:
                    continue
                commit = body[len(prefix):]
                if (
                    value.get("created_at") != value.get("updated_at")
                    or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None
                    or body != prefix + commit
                ):
                    raise WorkflowError("GitHub queue acceptance receipt is invalid")
                return commit
            if len(values) < 100:
                return None
            page += 1

    def acceptance_receipt(
        self,
        issue: GitHubRequestIssue,
        request_value: object,
        *,
        coordinator_integration_id: int,
    ) -> str | None:
        """Read an immutable App-authored receipt for a completed request."""
        request = validate_queue_request(request_value)
        if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
            raise ConfigurationError("GitHub queue coordinator integration id is invalid")
        if issue.state != "closed" or issue.state_reason != "completed":
            return None
        return self._acceptance_commit(
            issue,
            request,
            coordinator_integration_id=coordinator_integration_id,
        )

    def acceptance_intent(
        self,
        issue: GitHubRequestIssue,
        request_value: object,
        *,
        coordinator_integration_id: int,
    ) -> str | None:
        """Read App acceptance written before the terminal Issue close."""
        request = validate_queue_request(request_value)
        if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
            raise ConfigurationError("GitHub queue coordinator integration id is invalid")
        return self._acceptance_commit(
            issue,
            request,
            coordinator_integration_id=coordinator_integration_id,
        )

    def rejection_receipt(
        self,
        issue: GitHubRequestIssue,
        request_value: object,
        *,
        coordinator_integration_id: int,
    ) -> str | None:
        """Read an immutable App-authored proof that a request was not applied."""
        request = validate_queue_request(request_value)
        if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
            raise ConfigurationError("GitHub queue coordinator integration id is invalid")
        if issue.state != "closed" or issue.state_reason != "not_planned":
            return None
        prefix = (
            f"ARIA rejected request_id={request['request_id']} "
            f"body_sha256={issue.body_sha256} without_apply=true reason_sha256="
        )
        page = 1
        while True:
            values = self._transport.get_json(
                f"{self.repository.api_path}/issues/{issue.number}/comments"
                f"?per_page=100&page={page}"
            )
            if not isinstance(values, list):
                raise ConfigurationError("GitHub queue comments list is invalid")
            for value in values:
                body = value.get("body") if isinstance(value, dict) else None
                if not isinstance(body, str) or not body.startswith(prefix):
                    continue
                app = value.get("performed_via_github_app")
                if not isinstance(app, dict) or app.get("id") != coordinator_integration_id:
                    continue
                reason_sha256 = body[len(prefix):]
                if (
                    value.get("created_at") != value.get("updated_at")
                    or re.fullmatch(r"[0-9a-f]{64}", reason_sha256) is None
                    or body != prefix + reason_sha256
                ):
                    raise WorkflowError("GitHub queue rejection receipt is invalid")
                return reason_sha256
            if len(values) < 100:
                return None
            page += 1

    def _list_requests(
        self, *, project_id: str, state: str
    ) -> tuple[GitHubRequestIssue, ...]:
        if PROJECT_ID_RE.fullmatch(project_id) is None:
            raise ConfigurationError("GitHub queue project id is invalid")
        if state not in {"open", "all"}:
            raise ConfigurationError("GitHub queue issue state filter is invalid")
        prefix = f"ARIA request {project_id}: "
        result: list[GitHubRequestIssue] = []
        seen_numbers: set[int] = set()
        page = 1
        while True:
            values = self._transport.get_json(
                f"{self.repository.api_path}/issues?state={state}&per_page=100"
                f"&sort=updated&direction=desc&page={page}"
            )
            if not isinstance(values, list):
                raise ConfigurationError("GitHub queue issue list is invalid")
            for value in values:
                if (
                    isinstance(value, dict)
                    and "pull_request" not in value
                    and isinstance(value.get("title"), str)
                    and value["title"].startswith(prefix)
                ):
                    parsed = self._issue(value)
                    if parsed.number in seen_numbers:
                        raise WorkflowError("GitHub queue issue pagination did not advance")
                    seen_numbers.add(parsed.number)
                    result.append(parsed)
            if len(values) < 100:
                break
            page += 1
        # New work is attempted first, while already-authoritative history is
        # skipped by the processor without consuming its per-run action limit.
        return tuple(
            sorted(result, key=lambda issue: (issue.updated_at, issue.number), reverse=True)
        )

    def list_open(self, *, project_id: str) -> tuple[GitHubRequestIssue, ...]:
        return self._list_requests(project_id=project_id, state="open")

    def list_all(self, *, project_id: str) -> tuple[GitHubRequestIssue, ...]:
        return self._list_requests(project_id=project_id, state="all")

    def comment(self, issue_number: int, message: str) -> None:
        if type(issue_number) is not int or issue_number <= 0:
            raise ConfigurationError("GitHub queue issue number is invalid")
        message = _string(message, "comment", maximum=4000)
        response = _mapping(
            self._transport.post_json(
                f"{self.repository.api_path}/issues/{issue_number}/comments",
                {"body": message},
            ),
            "comment",
        )
        if type(response.get("id")) is not int or response["id"] <= 0:
            raise WorkflowError("GitHub queue comment read-back failed")

    def close(self, issue_number: int, *, state_reason: str = "completed") -> None:
        if type(issue_number) is not int or issue_number <= 0:
            raise ConfigurationError("GitHub queue issue number is invalid")
        if state_reason not in {"completed", "not_planned"}:
            raise ConfigurationError("GitHub queue issue state reason is invalid")
        issue = self._issue(
            self._transport.patch_json(
                f"{self.repository.api_path}/issues/{issue_number}",
                {"state": "closed", "state_reason": state_reason},
            )
        )
        if issue.state != "closed":
            raise WorkflowError("GitHub queue issue close read-back failed")

    def reject(self, issue_number: int, *, reason: str) -> None:
        reason = _string(reason, "rejection reason", maximum=1000)
        self.comment(issue_number, f"ARIA rejected this request without applying it: {reason}")
        self.close(issue_number, state_reason="not_planned")
