from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from aria.collaborative_documents import CollaborativeDocumentSet
from aria.errors import ConfigurationError, WorkflowError
from aria.github_control import (
    CONTROL_DOCUMENTS,
    GitHubControlCommit,
    PreparedGitHubControlCommit,
)
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE, default_runtime_root
from aria.registry import register_project


TRANSACTION_SCHEMA_VERSION = 1
PHASES = {
    "prepared",
    "commit_prepared",
    "remote_committed",
    "fetched",
    "worktree_ready",
}


class ControlWriter(Protocol):
    def read_head(self) -> str | None: ...
    def prepare_documents(
        self, *, documents: dict[str, str], expected_head: str | None, message: str
    ) -> PreparedGitHubControlCommit: ...
    def install_prepared(
        self, prepared: PreparedGitHubControlCommit
    ) -> GitHubControlCommit: ...


@dataclass(frozen=True)
class CollaborationApplyPaths:
    transaction: Path
    lock: Path


def collaboration_apply_paths(
    *, runtime_root: Path, project_id: str
) -> CollaborationApplyPaths:
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError("collaboration apply project id is invalid")
    root = runtime_root / "collaboration-enable" / project_id
    return CollaborationApplyPaths(
        transaction=root / "transaction.json",
        lock=runtime_root / "locks" / f"collaboration-enable-{project_id}.lock",
    )


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _git(
    code_root: Path, *args: str, environment: dict[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(code_root), *args],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise WorkflowError("collaboration Git operation failed") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Git failed"
        raise WorkflowError(f"collaboration Git operation failed: {detail}")
    return result.stdout.strip()


def _load(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("collaboration transaction is unreadable") from error
    if not isinstance(value, dict):
        raise ConfigurationError("collaboration transaction is invalid")
    return value


def _validate_journal(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "project_id",
        "plan_sha256",
        "repository_id",
        "code_root",
        "docs_root",
        "remote",
        "control_branch",
        "phase",
        "document_sha256",
        "prepared_commit",
    }:
        raise ConfigurationError("collaboration transaction schema is invalid")
    if value.get("schema_version") != 1 or value.get("phase") not in PHASES:
        raise ConfigurationError("collaboration transaction version or phase is invalid")
    for key in (
        "project_id",
        "plan_sha256",
        "repository_id",
        "code_root",
        "docs_root",
        "remote",
        "control_branch",
    ):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ConfigurationError("collaboration transaction identity is invalid")
    if (
        PROJECT_ID_RE.fullmatch(str(value["project_id"])) is None
        or len(str(value["plan_sha256"])) != 64
        or any(character not in "0123456789abcdef" for character in str(value["plan_sha256"]))
        or not str(value["repository_id"]).isdecimal()
        or not Path(str(value["code_root"])).is_absolute()
        or not Path(str(value["docs_root"])).is_absolute()
    ):
        raise ConfigurationError("collaboration transaction identity is invalid")
    hashes = value.get("document_sha256")
    if (
        not isinstance(hashes, dict)
        or set(hashes) != CONTROL_DOCUMENTS
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            for digest in hashes.values()
        )
    ):
        raise ConfigurationError("collaboration transaction document hashes are invalid")
    prepared = value.get("prepared_commit")
    if value["phase"] == "prepared":
        if prepared is not None:
            raise ConfigurationError("prepared transaction cannot contain a commit")
    elif not isinstance(prepared, dict):
        raise ConfigurationError("collaboration transaction commit is missing")
    return value


def _save(path: Path, journal: dict[str, object]) -> None:
    _validate_journal(journal)
    atomic_write_bytes(path, json_bytes(journal))


def _prepared(value: object) -> PreparedGitHubControlCommit:
    if not isinstance(value, dict):
        raise ConfigurationError("prepared control commit is missing")
    if set(value) != {
        "repository_id",
        "branch",
        "previous_head",
        "commit_sha",
        "files",
    } or not isinstance(value.get("files"), (list, tuple)):
        raise ConfigurationError("prepared control commit is invalid")
    try:
        return PreparedGitHubControlCommit(
            repository_id=str(value["repository_id"]),
            branch=str(value["branch"]),
            previous_head=(
                str(value["previous_head"])
                if value.get("previous_head") is not None
                else None
            ),
            commit_sha=str(value["commit_sha"]),
            files=tuple(value["files"]),
        )
    except (KeyError, TypeError) as error:
        raise ConfigurationError("prepared control commit is invalid") from error


def _verify_documents(docs_root: Path, hashes: dict[str, object]) -> None:
    actual_names = {
        path.name
        for path in docs_root.iterdir()
        if path.is_file() and path.name != ".git"
    }
    expected_names = set(hashes)
    if actual_names != expected_names:
        raise WorkflowError("control worktree document inventory mismatch")
    for name, expected in hashes.items():
        path = docs_root / name
        if not isinstance(expected, str) or _sha(path.read_text(encoding="utf-8")) != expected:
            raise WorkflowError(f"control worktree document hash mismatch: {name}")


def apply_collaboration_transaction(
    *,
    project_id: str,
    repository_id: str,
    plan_sha256: str,
    code_root: Path,
    docs_root: Path,
    remote: str,
    control_branch: str,
    document_set: CollaborativeDocumentSet,
    writer: ControlWriter,
    runtime_root: Path | None = None,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    runtime = runtime_root or default_runtime_root()
    paths = collaboration_apply_paths(runtime_root=runtime, project_id=project_id)
    identity = {
        "project_id": project_id,
        "plan_sha256": plan_sha256,
        "repository_id": repository_id,
        "code_root": str(code_root.resolve(strict=True)),
        "docs_root": str(docs_root.resolve(strict=False)),
        "remote": remote,
        "control_branch": control_branch,
    }
    hashes = {name: _sha(content) for name, content in document_set.documents.items()}
    with exclusive_lock(paths.lock, timeout_seconds=120):
        if paths.transaction.exists():
            journal = _validate_journal(_load(paths.transaction))
            if any(journal[key] != value for key, value in identity.items()):
                raise WorkflowError("another collaboration enable transaction is pending")
            if journal["document_sha256"] != hashes:
                raise WorkflowError("collaboration document set changed during recovery")
            recovered = True
        else:
            if writer.read_head() is not None:
                raise WorkflowError("unmanaged aria-control branch already exists")
            journal = {
                "schema_version": TRANSACTION_SCHEMA_VERSION,
                **identity,
                "phase": "prepared",
                "document_sha256": hashes,
                "prepared_commit": None,
            }
            _save(paths.transaction, journal)
            recovered = False
        if journal["phase"] == "prepared":
            prepared = writer.prepare_documents(
                documents=document_set.documents,
                expected_head=None,
                message=f"ARIA: initialize control plane {plan_sha256[:12]}",
            )
            journal["prepared_commit"] = asdict(prepared)
            journal["phase"] = "commit_prepared"
            _save(paths.transaction, journal)
        prepared = _prepared(journal["prepared_commit"])
        if journal["phase"] == "commit_prepared":
            writer.install_prepared(prepared)
            journal["phase"] = "remote_committed"
            _save(paths.transaction, journal)
        if journal["phase"] == "remote_committed":
            _git(
                code_root,
                "fetch",
                remote,
                f"+refs/heads/{control_branch}:refs/heads/{control_branch}",
                environment=git_environment,
            )
            local_head = _git(code_root, "rev-parse", f"refs/heads/{control_branch}")
            if local_head != prepared.commit_sha:
                raise WorkflowError("fetched control branch does not match coordinator commit")
            journal["phase"] = "fetched"
            _save(paths.transaction, journal)
        if journal["phase"] == "fetched":
            if docs_root.exists():
                if not docs_root.is_dir():
                    raise WorkflowError("control worktree path is not a directory")
            else:
                _git(code_root, "worktree", "add", str(docs_root), control_branch)
            _verify_documents(docs_root, hashes)
            journal["phase"] = "worktree_ready"
            _save(paths.transaction, journal)
        if journal["phase"] == "worktree_ready":
            registered = register_project(
                project_id,
                docs_root=docs_root,
                code_root=code_root,
                runtime_root=runtime,
            )
            paths.transaction.unlink()
            return {
                **registered,
                "collaboration": "enabled",
                "repository_id": repository_id,
                "control_branch": control_branch,
                "control_commit": prepared.commit_sha,
                "already_enabled": False,
                "recovered": recovered,
            }
    raise WorkflowError("collaboration enable transaction did not converge")
