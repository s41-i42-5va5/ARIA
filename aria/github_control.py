from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from aria.errors import ConfigurationError, WorkflowError
from aria.github import (
    GitHubApiError,
    GitHubMutationTransport,
    GitHubRepository,
)


GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
CONTROL_DOCUMENTS = frozenset(
    {
        "CONTROL.yaml",
        "PROJECT.yaml",
        "BACKLOG.yaml",
        "ACTIVITY.yaml",
        "STATE.yaml",
        "HISTORY.jsonl",
        "ARIA_TEAM.yaml",
        "ACCESS.yaml",
        "SYSTEM_MAP.yaml",
    }
)
MAX_CONTROL_FILE_BYTES = 1024 * 1024
MAX_CONTROL_COMMIT_BYTES = 4 * 1024 * 1024


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GitHubApiError(f"GitHub {label} response has invalid shape")
    return value


def _oid(value: object, label: str) -> str:
    if not isinstance(value, str) or GIT_OID_RE.fullmatch(value) is None:
        raise GitHubApiError(f"GitHub {label} is invalid")
    return value


def _repository_id(value: object) -> str:
    if type(value) is not int or value <= 0:
        raise GitHubApiError("GitHub repository id is invalid")
    return str(value)


def _files(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ConfigurationError("control commit files must be a non-empty mapping")
    if set(value) - CONTROL_DOCUMENTS:
        raise ConfigurationError("control commit contains a non-control document")
    result: dict[str, str] = {}
    total = 0
    for path in sorted(value):
        content = value[path]
        if not isinstance(content, str) or not content:
            raise ConfigurationError(f"control document is empty or invalid: {path}")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_CONTROL_FILE_BYTES:
            raise ConfigurationError(f"control document exceeds the size limit: {path}")
        total += len(encoded)
        result[path] = content
    if total > MAX_CONTROL_COMMIT_BYTES:
        raise ConfigurationError("control commit exceeds the size limit")
    return result


@dataclass(frozen=True)
class PreparedGitHubControlCommit:
    repository_id: str
    branch: str
    previous_head: str | None
    commit_sha: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class GitHubControlCommit:
    repository_id: str
    branch: str
    previous_head: str | None
    commit_sha: str
    created_branch: bool
    files: tuple[str, ...]

    def as_mapping(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "branch": self.branch,
            "previous_head": self.previous_head,
            "commit_sha": self.commit_sha,
            "created_branch": self.created_branch,
            "files": list(self.files),
        }


class GitHubControlPlaneWriter:
    def __init__(
        self,
        *,
        repository: GitHubRepository,
        repository_id: str,
        control_branch: str,
        transport: GitHubMutationTransport,
    ) -> None:
        if not isinstance(repository, GitHubRepository):
            raise ConfigurationError("GitHub repository locator is invalid")
        if not isinstance(repository_id, str) or not repository_id.isdecimal():
            raise ConfigurationError("GitHub repository id must be numeric")
        if (
            not isinstance(control_branch, str)
            or not control_branch
            or control_branch.startswith("-")
            or any(character in control_branch for character in " ~^:?*[]\\")
        ):
            raise ConfigurationError("GitHub control branch name is invalid")
        if not all(
            hasattr(transport, method) for method in ("get_json", "post_json", "patch_json")
        ):
            raise ConfigurationError("GitHub mutation transport is invalid")
        self.repository = repository
        self.repository_id = repository_id
        self.control_branch = control_branch
        self._transport = transport

    @property
    def _ref_path(self) -> str:
        branch = urllib.parse.quote(self.control_branch, safe="/")
        return f"{self.repository.api_path}/git/ref/heads/{branch}"

    def _repository_readback(self) -> None:
        repository = _mapping(
            self._transport.get_json(self.repository.api_path), "repository"
        )
        if _repository_id(repository.get("id")) != self.repository_id:
            raise WorkflowError("GitHub control writer repository id mismatch")
        full_name = repository.get("full_name")
        expected = f"{self.repository.owner}/{self.repository.name}"
        if not isinstance(full_name, str) or full_name.casefold() != expected.casefold():
            raise WorkflowError("GitHub control writer repository locator mismatch")

    def read_head(self) -> str | None:
        try:
            ref = _mapping(self._transport.get_json(self._ref_path), "control ref")
        except GitHubApiError as error:
            if error.status == 404:
                return None
            raise
        obj = _mapping(ref.get("object"), "control ref object")
        return _oid(obj.get("sha"), "control ref sha")

    def commit_documents(
        self,
        *,
        documents: dict[str, str],
        expected_head: str | None,
        message: str,
    ) -> GitHubControlCommit:
        prepared = self.prepare_documents(
            documents=documents,
            expected_head=expected_head,
            message=message,
        )
        return self.install_prepared(prepared)

    def prepare_documents(
        self,
        *,
        documents: dict[str, str],
        expected_head: str | None,
        message: str,
    ) -> PreparedGitHubControlCommit:
        documents = _files(documents)
        if expected_head is not None:
            expected_head = _oid(expected_head, "expected control head")
        if (
            not isinstance(message, str)
            or not message
            or message != message.strip()
            or len(message) > 200
            or "\n" in message
            or "\r" in message
        ):
            raise ConfigurationError("control commit message is invalid")
        self._repository_readback()
        current = self.read_head()
        if current != expected_head:
            raise WorkflowError(
                f"Stale aria-control head: expected {expected_head!r}, actual {current!r}"
            )
        tree = _mapping(
            self._transport.post_json(
                f"{self.repository.api_path}/git/trees",
                {
                    "tree": [
                        {
                            "path": path,
                            "mode": "100644",
                            "type": "blob",
                            "content": documents[path],
                        }
                        for path in sorted(documents)
                    ]
                },
            ),
            "created tree",
        )
        tree_sha = _oid(tree.get("sha"), "tree sha")
        commit = _mapping(
            self._transport.post_json(
                f"{self.repository.api_path}/git/commits",
                {
                    "message": message,
                    "tree": tree_sha,
                    "parents": [current] if current is not None else [],
                },
            ),
            "created commit",
        )
        commit_sha = _oid(commit.get("sha"), "commit sha")
        return PreparedGitHubControlCommit(
            repository_id=self.repository_id,
            branch=self.control_branch,
            previous_head=current,
            commit_sha=commit_sha,
            files=tuple(sorted(documents)),
        )

    def install_prepared(
        self, prepared: PreparedGitHubControlCommit
    ) -> GitHubControlCommit:
        if not isinstance(prepared, PreparedGitHubControlCommit):
            raise ConfigurationError("prepared control commit is invalid")
        if (
            prepared.repository_id != self.repository_id
            or prepared.branch != self.control_branch
            or not prepared.files
            or set(prepared.files) - CONTROL_DOCUMENTS
        ):
            raise ConfigurationError("prepared control commit identity is invalid")
        previous = prepared.previous_head
        if previous is not None:
            previous = _oid(previous, "prepared previous head")
        commit_sha = _oid(prepared.commit_sha, "prepared commit sha")
        self._repository_readback()
        current = self.read_head()
        if current == commit_sha:
            installed = True
        elif current != previous:
            raise WorkflowError(
                f"Stale aria-control head: expected {previous!r}, actual {current!r}"
            )
        else:
            installed = False
        if not installed and current is None:
            self._transport.post_json(
                f"{self.repository.api_path}/git/refs",
                {"ref": f"refs/heads/{self.control_branch}", "sha": commit_sha},
            )
        elif not installed:
            self._transport.patch_json(
                self._ref_path,
                {"sha": commit_sha, "force": False},
            )
        read_back = self.read_head()
        if read_back != commit_sha:
            raise WorkflowError("GitHub control commit read-back mismatch")
        return GitHubControlCommit(
            repository_id=self.repository_id,
            branch=self.control_branch,
            previous_head=previous,
            commit_sha=commit_sha,
            created_branch=previous is None,
            files=prepared.files,
        )
