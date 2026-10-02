from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from aria.activity import TASK_ID_RE
from aria.errors import ConfigurationError, WorkflowError
from aria.file_scope import normalize_scope_path, path_allowed
from aria.github import GitHubApiError, GitHubMutationTransport, GitHubRepository
from aria.provider import ProviderActor


GIT_OID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigurationError(f"GitHub integration {label} is invalid")
    return value


def _oid(value: object, label: str) -> str:
    if not isinstance(value, str) or GIT_OID_RE.fullmatch(value) is None:
        raise ConfigurationError(f"GitHub integration {label} is invalid")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigurationError(f"GitHub integration {label} is invalid")
    return value


def _string(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ConfigurationError(f"GitHub integration {label} is invalid")
    return value


def _stamp(value: object, label: str) -> str:
    from datetime import UTC, datetime

    text = _string(value, label)
    if not text.endswith("Z"):
        raise ConfigurationError(f"GitHub integration {label} is not UTC")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ConfigurationError(f"GitHub integration {label} is invalid") from error
    if parsed.astimezone(UTC) != parsed:
        raise ConfigurationError(f"GitHub integration {label} is not UTC")
    return text


def _backlog_binding(body: object) -> str | None:
    if body is None:
        return None
    if not isinstance(body, str):
        raise ConfigurationError("GitHub integration pull request body is invalid")
    bindings = re.findall(
        r"^ARIA-Backlog: (BLG-[A-Z0-9][A-Z0-9._-]{0,63})[ \t]*$",
        body,
        re.MULTILINE,
    )
    if not bindings:
        if "ARIA-Backlog:" in body:
            raise WorkflowError("pull request contains an invalid ARIA-Backlog binding")
        return None
    if len(bindings) != 1 or TASK_ID_RE.fullmatch(bindings[0]) is None:
        raise WorkflowError("pull request must contain one exact ARIA-Backlog binding")
    return bindings[0]


@dataclass(frozen=True)
class RequiredGitHubCheck:
    name: str
    app_id: int | None

    def __post_init__(self) -> None:
        _string(self.name, "required check name")
        if self.app_id is not None and (
            type(self.app_id) is not int or self.app_id == 0 or self.app_id < -1
        ):
            raise ConfigurationError("GitHub integration required check app is invalid")

    def as_mapping(self) -> dict[str, object]:
        return {"name": self.name, "app_id": self.app_id}


@dataclass(frozen=True)
class GitHubIntegrationAcceptance:
    repository_id: str
    pull_request_number: int
    integration_branch: str
    merge_commit: str
    merged_at: str
    pull_request_author: ProviderActor
    required_checks: tuple[RequiredGitHubCheck, ...]
    backlog_item_id: str
    source_branch: str
    changed_paths: tuple[str, ...]
    source_commit: str | None = None

    def __post_init__(self) -> None:
        if not self.repository_id.isdecimal() or int(self.repository_id) <= 0:
            raise ConfigurationError("GitHub integration repository id is invalid")
        _positive(self.pull_request_number, "pull request number")
        _string(self.integration_branch, "branch")
        _oid(self.merge_commit, "merge commit")
        _stamp(self.merged_at, "merged_at")
        if (
            not self.required_checks
            or not isinstance(self.required_checks, tuple)
            or not all(isinstance(check, RequiredGitHubCheck) for check in self.required_checks)
            or TASK_ID_RE.fullmatch(self.backlog_item_id) is None
        ):
            raise ConfigurationError("GitHub integration acceptance is invalid")
        _string(self.source_branch, "source branch")
        if (
            not self.changed_paths
            or not isinstance(self.changed_paths, tuple)
            or tuple(sorted(set(self.changed_paths))) != self.changed_paths
        ):
            raise ConfigurationError("GitHub integration changed paths are invalid")
        for path in self.changed_paths:
            normalize_scope_path(path)
        if self.source_commit is not None:
            _oid(self.source_commit, "source commit")

    def as_mapping(self) -> dict[str, object]:
        return {
            "provider": "github",
            "repository_id": self.repository_id,
            "pull_request_number": self.pull_request_number,
            "integration_branch": self.integration_branch,
            "merge_commit": self.merge_commit,
            "merged_at": self.merged_at,
            "pull_request_author": self.pull_request_author.as_mapping(),
            "required_checks": [check.as_mapping() for check in self.required_checks],
            "backlog_item_id": self.backlog_item_id,
            "source_branch": self.source_branch,
            "changed_paths": list(self.changed_paths),
            "source_commit": self.source_commit,
        }


@dataclass(frozen=True)
class GitHubBoundPullRequest:
    number: int
    backlog_item_id: str
    merged_at: str
    pull_request_author: ProviderActor | None = None
    branch: str | None = None

    def as_mapping(self) -> dict[str, object]:
        return {
            "number": self.number,
            "backlog_item_id": self.backlog_item_id,
            "merged_at": self.merged_at,
            "pull_request_author": (
                self.pull_request_author.as_mapping()
                if self.pull_request_author is not None
                else None
            ),
            "branch": self.branch,
        }


@dataclass(frozen=True)
class GitHubBoundOpenPullRequest:
    number: int
    backlog_item_id: str
    updated_at: str
    pull_request_author: ProviderActor
    branch: str
    head_sha: str | None = None
    changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _positive(self.number, "pull request number")
        if TASK_ID_RE.fullmatch(self.backlog_item_id) is None:
            raise ConfigurationError("GitHub open pull request backlog item is invalid")
        _stamp(self.updated_at, "updated_at")
        if not isinstance(self.pull_request_author, ProviderActor):
            raise ConfigurationError("GitHub open pull request author is invalid")
        _string(self.branch, "pull request branch")
        if self.head_sha is not None:
            _oid(self.head_sha, "pull request head")
        for path in self.changed_paths:
            normalize_scope_path(path)

    def as_mapping(self) -> dict[str, object]:
        return {
            "number": self.number,
            "backlog_item_id": self.backlog_item_id,
            "updated_at": self.updated_at,
            "pull_request_author": self.pull_request_author.as_mapping(),
            "branch": self.branch,
            "head_sha": self.head_sha,
            "changed_paths": list(self.changed_paths),
        }


class GitHubIntegrationVerifier:
    def __init__(
        self,
        *,
        repository: GitHubRepository,
        transport: GitHubMutationTransport,
        coordinator_integration_id: int | None = None,
    ) -> None:
        if not isinstance(repository, GitHubRepository):
            raise ConfigurationError("GitHub integration repository is invalid")
        if not all(
            hasattr(transport, method)
            for method in ("get_json", "post_json", "patch_json")
        ):
            raise ConfigurationError("GitHub integration transport is invalid")
        self.repository = repository
        self._transport = transport
        if coordinator_integration_id is not None:
            _positive(coordinator_integration_id, "coordinator integration id")
        self._coordinator_integration_id = coordinator_integration_id

    def _require_ancestor(self, source: str, target: str, label: str) -> None:
        if source == target:
            return
        comparison = _mapping(
            self._transport.get_json(
                f"{self.repository.api_path}/compare/{source}...{target}"
            ),
            "commit comparison",
        )
        merge_base = _mapping(
            comparison.get("merge_base_commit"), "comparison merge base"
        )
        if (
            comparison.get("status") != "ahead"
            or type(comparison.get("ahead_by")) is not int
            or comparison["ahead_by"] <= 0
            or _oid(merge_base.get("sha"), "comparison merge base") != source
        ):
            raise WorkflowError(label)

    def _verify_repository(self, repository_id: str) -> None:
        if not isinstance(repository_id, str) or not repository_id.isdecimal():
            raise ConfigurationError("GitHub integration repository id is invalid")
        repository = _mapping(
            self._transport.get_json(self.repository.api_path), "repository"
        )
        if str(_positive(repository.get("id"), "repository id")) != repository_id:
            raise WorkflowError("GitHub integration repository id mismatch")
        full_name = repository.get("full_name")
        expected_name = f"{self.repository.owner}/{self.repository.name}"
        if not isinstance(full_name, str) or full_name.casefold() != expected_name.casefold():
            raise WorkflowError("GitHub integration repository identity mismatch")

    def _required_checks(self, branch: str) -> tuple[RequiredGitHubCheck, ...]:
        encoded = urllib.parse.quote(branch, safe="")
        try:
            value = self._transport.get_json(
                f"{self.repository.api_path}/branches/{encoded}"
                "/protection/required_status_checks"
            )
        except GitHubApiError as error:
            if error.status == 404:
                raise WorkflowError(
                    "integration branch has no required status checks"
                ) from error
            raise
        protection = _mapping(value, "required status checks")
        if protection.get("strict") is not True:
            raise WorkflowError("integration required status checks are not strict")
        checks_value = protection.get("checks")
        checks: list[RequiredGitHubCheck] = []
        if checks_value is not None:
            if not isinstance(checks_value, list):
                raise ConfigurationError("GitHub integration required checks are invalid")
            for index, value in enumerate(checks_value):
                check = _mapping(value, f"required check {index}")
                if set(check) != {"context", "app_id"}:
                    raise ConfigurationError("GitHub integration required check is invalid")
                app_id = check.get("app_id")
                if app_id is not None and (
                    type(app_id) is not int or app_id == 0 or app_id < -1
                ):
                    raise ConfigurationError("GitHub integration required check app is invalid")
                checks.append(RequiredGitHubCheck(_string(check.get("context"), "check name"), app_id))
        else:
            contexts = protection.get("contexts")
            if not isinstance(contexts, list) or not all(
                isinstance(context, str) for context in contexts
            ):
                raise ConfigurationError("GitHub integration required contexts are invalid")
            checks.extend(RequiredGitHubCheck(_string(context, "check name"), None) for context in contexts)
        if not checks or len({(check.name, check.app_id) for check in checks}) != len(checks):
            raise WorkflowError("integration branch required checks are empty or duplicated")
        if self._coordinator_integration_id is not None and not any(
            check.name == "ARIA integration"
            and check.app_id == self._coordinator_integration_id
            for check in checks
        ):
            raise WorkflowError(
                "integration branch must require ARIA integration from the coordinator App"
            )
        return tuple(checks)

    def _changed_paths(self, number: int) -> tuple[str, ...]:
        changed_paths: list[str] = []
        for page in range(1, 31):
            files = self._transport.get_json(
                f"{self.repository.api_path}/pulls/{number}/files?per_page=100&page={page}"
            )
            if not isinstance(files, list):
                raise ConfigurationError("GitHub integration pull request files are invalid")
            for index, value in enumerate(files):
                file = _mapping(value, f"pull request file {index}")
                changed_paths.append(normalize_scope_path(file.get("filename")))
                previous = file.get("previous_filename")
                if previous is not None:
                    changed_paths.append(normalize_scope_path(previous))
            if len(files) < 100:
                break
        else:
            raise WorkflowError("pull request exceeds the 3000-file verification limit")
        result = tuple(sorted(set(changed_paths)))
        if not result:
            raise WorkflowError("pull request has no changed files")
        return result

    def _immutable_compare(
        self, *, base_sha: str, head_sha: str, expected_actor_id: str
    ) -> tuple[str, ...]:
        comparison = _mapping(
            self._transport.get_json(
                f"{self.repository.api_path}/compare/{base_sha}...{head_sha}"
            ),
            "immutable pull request comparison",
        )
        commits = comparison.get("commits")
        files = comparison.get("files")
        total_commits = comparison.get("total_commits")
        if (
            not isinstance(commits, list)
            or not commits
            or type(total_commits) is not int
            or total_commits != len(commits)
            or len(commits) >= 250
        ):
            raise WorkflowError("pull request immutable commit set is unavailable or too large")
        if not isinstance(files, list) or not files or len(files) >= 300:
            raise WorkflowError("pull request immutable file set is unavailable or too large")
        if comparison.get("status") not in {"ahead", "identical"}:
            raise WorkflowError("pull request head is not based on the verified base commit")
        for value in commits:
            commit = _mapping(value, "pull request commit")
            commit_author = _mapping(commit.get("author"), "pull request commit author")
            commit_committer = _mapping(
                commit.get("committer"), "pull request commit committer"
            )
            verification = _mapping(
                _mapping(commit.get("commit"), "pull request git commit").get(
                    "verification"
                ),
                "pull request commit verification",
            )
            if (
                str(_positive(commit_author.get("id"), "commit author id"))
                != expected_actor_id
                or str(_positive(commit_committer.get("id"), "commit committer id"))
                != expected_actor_id
                or verification.get("verified") is not True
                or verification.get("reason") != "valid"
            ):
                raise WorkflowError(
                    "pull request commits must be validly signed and authored and committed "
                    "by the task assignee"
                )
        changed_paths: list[str] = []
        for index, value in enumerate(files):
            file = _mapping(value, f"pull request file {index}")
            changed_paths.append(normalize_scope_path(file.get("filename")))
            previous = file.get("previous_filename")
            if previous is not None:
                changed_paths.append(normalize_scope_path(previous))
        return tuple(sorted(set(changed_paths)))

    def verify_open(
        self,
        *,
        repository_id: str,
        integration_branch: str,
        pull_request_number: int,
        backlog_item_id: str,
        expected_actor_id: str,
        expected_branch: str,
        scope_paths: list[str],
    ) -> GitHubBoundOpenPullRequest:
        """Verify the exact PR head and publish the required fail-closed App check."""
        branch = _string(integration_branch, "branch")
        number = _positive(pull_request_number, "pull request number")
        if TASK_ID_RE.fullmatch(backlog_item_id) is None:
            raise ConfigurationError("GitHub integration backlog item is invalid")
        self._verify_repository(repository_id)
        branch_value = _mapping(
            self._transport.get_json(
                f"{self.repository.api_path}/branches/{urllib.parse.quote(branch, safe='')}"
            ),
            "branch",
        )
        if branch_value.get("name") != branch or branch_value.get("protected") is not True:
            raise WorkflowError("integration branch identity or protection mismatch")
        self._required_checks(branch)
        pull = _mapping(
            self._transport.get_json(f"{self.repository.api_path}/pulls/{number}"),
            "pull request",
        )
        base = _mapping(pull.get("base"), "pull request base")
        head = _mapping(pull.get("head"), "pull request head")
        author = _mapping(pull.get("user"), "pull request author")
        binding = _backlog_binding(pull.get("body"))
        base_sha = _oid(base.get("sha"), "pull request base")
        head_sha = _oid(head.get("sha"), "pull request head")
        actual_actor_id = str(_positive(author.get("id"), "pull request author id"))
        if (
            pull.get("number") != number
            or pull.get("state") != "open"
            or binding != backlog_item_id
            or base.get("ref") != branch
            or str(_positive(_mapping(base.get("repo"), "base repository").get("id"), "base repository id")) != repository_id
            or str(_positive(_mapping(head.get("repo"), "head repository").get("id"), "head repository id")) != repository_id
            or head.get("ref") != expected_branch
            or actual_actor_id != expected_actor_id
        ):
            raise WorkflowError("open pull request does not match the active ARIA task lease")

        changed_paths = self._immutable_compare(
            base_sha=base_sha,
            head_sha=head_sha,
            expected_actor_id=expected_actor_id,
        )
        pull_readback = _mapping(
            self._transport.get_json(f"{self.repository.api_path}/pulls/{number}"),
            "pull request head read-back",
        )
        if (
            pull_readback.get("state") != "open"
            or _oid(
                _mapping(pull_readback.get("head"), "pull request head read-back").get(
                    "sha"
                ),
                "pull request head read-back",
            )
            != head_sha
        ):
            raise WorkflowError("pull request head changed during immutable verification")
        outside = [path for path in changed_paths if not path_allowed(path, scope_paths)]
        conclusion = "success" if not outside else "failure"
        summary = (
            "ARIA verified assignee, signed commits and file scope."
            if not outside
            else f"Paths outside task scope: {', '.join(outside[:20])}"
        )
        check = _mapping(
            self._transport.post_json(
                f"{self.repository.api_path}/check-runs",
                {
                    "name": "ARIA integration",
                    "head_sha": head_sha,
                    "status": "completed",
                    "conclusion": conclusion,
                    "output": {"title": "ARIA task lease verification", "summary": summary},
                },
            ),
            "created check run",
        )
        if check.get("name") != "ARIA integration" or check.get("head_sha") != head_sha:
            raise WorkflowError("ARIA integration check read-back mismatch")
        if outside:
            raise WorkflowError(f"pull request changes paths outside task scope: {outside}")
        return GitHubBoundOpenPullRequest(
            number=number,
            backlog_item_id=backlog_item_id,
            updated_at=_stamp(pull.get("updated_at"), "updated_at"),
            pull_request_author=ProviderActor(
                actual_actor_id, _string(author.get("login"), "pull request author login"), None
            ),
            branch=expected_branch,
            head_sha=head_sha,
            changed_paths=changed_paths,
        )

    def list_bound_merged_pull_requests(
        self,
        *,
        integration_branch: str,
        maximum: int = 20,
        repository_id: str | None = None,
    ) -> tuple[GitHubBoundPullRequest, ...]:
        branch = _string(integration_branch, "branch")
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise ConfigurationError("GitHub integration pull request limit is invalid")
        if repository_id is not None:
            self._verify_repository(repository_id)
        encoded = urllib.parse.quote(branch, safe="")
        candidates: list[GitHubBoundPullRequest] = []
        for page in range(1, 11):
            values = self._transport.get_json(
                f"{self.repository.api_path}/pulls?state=closed&base={encoded}"
                f"&sort=updated&direction=asc&per_page=100&page={page}"
            )
            if not isinstance(values, list):
                raise ConfigurationError("GitHub integration pull request list is invalid")
            for value in values:
                pull = _mapping(value, "pull request list item")
                if pull.get("merged_at") is None:
                    continue
                binding = _backlog_binding(pull.get("body"))
                if binding is None:
                    continue
                candidates.append(
                    GitHubBoundPullRequest(
                        number=_positive(pull.get("number"), "pull request number"),
                        backlog_item_id=binding,
                        merged_at=_stamp(pull.get("merged_at"), "merged_at"),
                        pull_request_author=(
                            ProviderActor(
                                str(
                                    _positive(
                                        _mapping(
                                            pull.get("user"), "pull request author"
                                        ).get("id"),
                                        "pull request author id",
                                    )
                                ),
                                _string(
                                    _mapping(
                                        pull.get("user"), "pull request author"
                                    ).get("login"),
                                    "pull request author login",
                                ),
                                None,
                            )
                            if pull.get("user") is not None
                            else None
                        ),
                        branch=(
                            _string(
                                _mapping(pull.get("head"), "pull request head").get(
                                    "ref"
                                ),
                                "pull request branch",
                            )
                            if pull.get("head") is not None
                            else None
                        ),
                    )
                )
            if len(values) < 100:
                break
        else:
            raise WorkflowError("GitHub integration exceeds the 1000-pull-request scan limit")
        candidates.sort(key=lambda value: (value.merged_at, value.number))
        return tuple(candidates[:maximum])

    def list_bound_open_pull_requests(
        self,
        *,
        repository_id: str,
        integration_branch: str,
        maximum: int = 1000,
    ) -> tuple[GitHubBoundOpenPullRequest, ...]:
        branch = _string(integration_branch, "branch")
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise ConfigurationError("GitHub integration pull request limit is invalid")
        self._verify_repository(repository_id)
        encoded = urllib.parse.quote(branch, safe="")
        candidates: list[GitHubBoundOpenPullRequest] = []
        for page in range(1, 11):
            values = self._transport.get_json(
                f"{self.repository.api_path}/pulls?state=open&base={encoded}"
                f"&sort=updated&direction=asc&per_page=100&page={page}"
            )
            if not isinstance(values, list):
                raise ConfigurationError("GitHub integration pull request list is invalid")
            for value in values:
                pull = _mapping(value, "open pull request list item")
                binding = _backlog_binding(pull.get("body"))
                if binding is None:
                    continue
                author = _mapping(pull.get("user"), "pull request author")
                head = _mapping(pull.get("head"), "pull request head")
                candidates.append(
                    GitHubBoundOpenPullRequest(
                        number=_positive(pull.get("number"), "pull request number"),
                        backlog_item_id=binding,
                        updated_at=_stamp(pull.get("updated_at"), "updated_at"),
                        pull_request_author=ProviderActor(
                            str(_positive(author.get("id"), "pull request author id")),
                            _string(author.get("login"), "pull request author login"),
                            None,
                        ),
                        branch=_string(head.get("ref"), "pull request branch"),
                    )
                )
            if len(values) < 100:
                break
        else:
            raise WorkflowError("GitHub integration exceeds the 1000-pull-request scan limit")
        candidates.sort(key=lambda value: (value.updated_at, value.number))
        return tuple(candidates[:maximum])

    def verify(
        self,
        *,
        repository_id: str,
        integration_branch: str,
        pull_request_number: int,
        previous_accepted_head: str | None = None,
    ) -> GitHubIntegrationAcceptance:
        if not isinstance(repository_id, str) or not repository_id.isdecimal():
            raise ConfigurationError("GitHub integration repository id is invalid")
        branch = _string(integration_branch, "branch")
        number = _positive(pull_request_number, "pull request number")
        self._verify_repository(repository_id)

        encoded_branch = urllib.parse.quote(branch, safe="")
        branch_value = _mapping(
            self._transport.get_json(
                f"{self.repository.api_path}/branches/{encoded_branch}"
            ),
            "branch",
        )
        branch_commit = _oid(
            _mapping(branch_value.get("commit"), "branch commit").get("sha"),
            "branch commit",
        )
        if branch_value.get("name") != branch or branch_value.get("protected") is not True:
            raise WorkflowError("integration branch identity or protection mismatch")

        pull = _mapping(
            self._transport.get_json(f"{self.repository.api_path}/pulls/{number}"),
            "pull request",
        )
        base = _mapping(pull.get("base"), "pull request base")
        base_repository = _mapping(base.get("repo"), "pull request base repository")
        head = _mapping(pull.get("head"), "pull request head")
        head_repository = _mapping(head.get("repo"), "pull request head repository")
        author = _mapping(pull.get("user"), "pull request author")
        binding = _backlog_binding(pull.get("body"))
        if binding is None:
            raise WorkflowError("pull request has no ARIA-Backlog binding")
        merge_commit = _oid(pull.get("merge_commit_sha"), "merge commit")
        merged_at = _stamp(pull.get("merged_at"), "merged_at")
        if (
            pull.get("number") != number
            or pull.get("state") != "closed"
            or pull.get("merged") is not True
            or base.get("ref") != branch
            or str(_positive(base_repository.get("id"), "base repository id"))
            != repository_id
            or str(_positive(head_repository.get("id"), "head repository id"))
            != repository_id
        ):
            raise WorkflowError("pull request is not merged into the integration branch")
        self._require_ancestor(
            merge_commit,
            branch_commit,
            "pull request merge commit is absent from remote integration branch",
        )
        if previous_accepted_head is not None:
            previous = _oid(previous_accepted_head, "previous accepted head")
            self._require_ancestor(
                previous,
                merge_commit,
                "merge commit does not descend from accepted state",
            )
        actor = ProviderActor(
            str(_positive(author.get("id"), "pull request author id")),
            _string(author.get("login"), "pull request author login"),
            None,
        )
        required = self._required_checks(branch)
        head_sha = _oid(head.get("sha"), "pull request head")
        self._require_ancestor(
            head_sha,
            merge_commit,
            "pull request head is absent from the verified merge commit",
        )
        changed_paths = self._changed_paths(number)
        check_response = _mapping(
            self._transport.get_json(
                f"{self.repository.api_path}/commits/{head_sha}"
                "/check-runs?filter=latest&per_page=100"
            ),
            "check runs",
        )
        runs = check_response.get("check_runs")
        if type(check_response.get("total_count")) is not int or not isinstance(runs, list):
            raise ConfigurationError("GitHub integration check runs are invalid")
        statuses_response = _mapping(
            self._transport.get_json(
                f"{self.repository.api_path}/commits/{head_sha}/status"
            ),
            "commit status",
        )
        statuses = statuses_response.get("statuses")
        if not isinstance(statuses, list):
            raise ConfigurationError("GitHub integration commit statuses are invalid")
        for required_check in required:
            matching_runs = []
            for value in runs:
                run = _mapping(value, "check run")
                app = _mapping(run.get("app"), "check run app")
                if run.get("name") == required_check.name and (
                    required_check.app_id in {None, -1}
                    or app.get("id") == required_check.app_id
                ):
                    matching_runs.append(run)
            run_passed = any(
                run.get("status") == "completed" and run.get("conclusion") == "success"
                for run in matching_runs
            )
            status_passed = required_check.app_id in {None, -1} and any(
                isinstance(value, dict)
                and value.get("context") == required_check.name
                and value.get("state") == "success"
                for value in statuses
            )
            if not (run_passed or status_passed):
                raise WorkflowError(
                    f"required integration check did not pass: {required_check.name}"
                )
        return GitHubIntegrationAcceptance(
            repository_id=repository_id,
            pull_request_number=number,
            integration_branch=branch,
            merge_commit=merge_commit,
            merged_at=merged_at,
            pull_request_author=actor,
            required_checks=required,
            backlog_item_id=binding,
            source_branch=_string(head.get("ref"), "pull request source branch"),
            changed_paths=tuple(changed_paths),
            source_commit=head_sha,
        )
