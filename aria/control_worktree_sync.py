from __future__ import annotations

import hashlib
import json
import re
import subprocess
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from aria.collaboration import load_control_contract
from aria.errors import ConfigurationError, WorkflowError
from aria.github_control import CONTROL_DOCUMENTS, PreparedGitHubControlCommit
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import ProjectConfig


OPERATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
GIT_OID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
JOURNAL_KEYS = {
    "schema_version",
    "project_id",
    "repository_id",
    "operation_id",
    "phase",
    "document_sha256",
    "prepared_commit",
}


class ControlWriter(Protocol):
    def read_head(self) -> str | None: ...
    def prepare_documents(
        self, *, documents: dict[str, str], expected_head: str | None, message: str
    ) -> PreparedGitHubControlCommit: ...
    def install_prepared(self, prepared: PreparedGitHubControlCommit): ...


@contextmanager
def serialized_control_operation(project: ProjectConfig):
    lock = project.runtime_root / "locks" / "control-operation.lock"
    with exclusive_lock(lock, timeout_seconds=120):
        yield


def prepare_control_mutation(
    project: ProjectConfig,
    *,
    operation_id: str,
    target_documents: dict[str, str] | None = None,
) -> Path:
    """Persist intent before the first local control-document mutation.

    The base hashes make a restart distinguish an operation that never changed
    the worktree from one that changed a complete, validated control snapshot.
    The latter is rolled forward by ``recover_pending_control_sync`` before a
    refresh is attempted.
    """
    if OPERATION_RE.fullmatch(operation_id) is None:
        raise ConfigurationError("control mutation operation id is invalid")
    pending = project.runtime_root / "control-sync" / "pending.json"
    transaction = project.runtime_root / "control-sync" / "transaction.json"
    if transaction.exists():
        raise WorkflowError("another control sync recovery is pending")
    documents = _documents(project.docs_root)
    if target_documents is not None and (
        set(target_documents) != CONTROL_DOCUMENTS
        or not all(isinstance(content, str) for content in target_documents.values())
    ):
        raise ConfigurationError("control mutation target snapshot is invalid")
    value = {
        "schema_version": 3 if target_documents is not None else 2,
        "project_id": project.project_id,
        "operation_id": operation_id,
        "phase": "local_mutation_prepared",
        "base_document_sha256": _hashes(documents),
    }
    if target_documents is not None:
        value["target_documents"] = target_documents
        value["target_document_sha256"] = _hashes(target_documents)
    if pending.exists():
        try:
            existing = json.loads(pending.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConfigurationError("control pending mutation is unreadable") from error
        if existing != value:
            raise WorkflowError("another control mutation recovery is pending")
        return pending
    atomic_write_bytes(pending, json_bytes(value))
    return pending


def _git(
    root: Path, *args: str, environment: dict[str, str] | None = None
) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise WorkflowError("control worktree Git operation failed") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Git failed"
        raise WorkflowError(f"control worktree Git operation failed: {detail}")
    return result.stdout.strip()


def _documents(root: Path) -> dict[str, str]:
    inventory = {path.name for path in root.iterdir() if path.name != ".git"}
    if inventory != CONTROL_DOCUMENTS:
        raise WorkflowError("control worktree inventory is invalid")
    result: dict[str, str] = {}
    for name in sorted(CONTROL_DOCUMENTS):
        path = root / name
        if not path.is_file():
            raise WorkflowError(f"control document is missing: {name}")
        try:
            result[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigurationError(f"control document is unreadable: {name}") from error
    return result


def _hashes(documents: dict[str, str]) -> dict[str, str]:
    return {
        name: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for name, content in documents.items()
    }


def _prepared(value: object) -> PreparedGitHubControlCommit:
    if not isinstance(value, dict) or set(value) != {
        "repository_id",
        "branch",
        "previous_head",
        "commit_sha",
        "files",
    }:
        raise ConfigurationError("control sync prepared commit is invalid")
    files = value.get("files")
    previous_head = value.get("previous_head")
    commit_sha = value.get("commit_sha")
    if (
        not isinstance(files, (list, tuple))
        or len(files) != len(CONTROL_DOCUMENTS)
        or set(files) != CONTROL_DOCUMENTS
        or not all(isinstance(name, str) for name in files)
    ):
        raise ConfigurationError("control sync prepared files are invalid")
    if (
        not isinstance(previous_head, str)
        or GIT_OID_RE.fullmatch(previous_head) is None
        or not isinstance(commit_sha, str)
        or GIT_OID_RE.fullmatch(commit_sha) is None
        or previous_head == commit_sha
    ):
        raise ConfigurationError("control sync prepared commit ids are invalid")
    repository_id = value.get("repository_id")
    branch = value.get("branch")
    if (
        not isinstance(repository_id, str)
        or not repository_id
        or not isinstance(branch, str)
        or not branch
    ):
        raise ConfigurationError("control sync prepared identity is invalid")
    return PreparedGitHubControlCommit(
        repository_id=repository_id,
        branch=branch,
        previous_head=previous_head,
        commit_sha=commit_sha,
        files=tuple(sorted(files)),
    )


def _validate_journal(
    value: object,
    *,
    project: ProjectConfig,
    repository_id: str,
    control_branch: str,
    operation_id: str,
) -> tuple[dict[str, object], PreparedGitHubControlCommit]:
    if (
        not isinstance(value, dict)
        or set(value) != JOURNAL_KEYS
        or value.get("schema_version") != 1
        or value.get("project_id") != project.project_id
        or value.get("repository_id") != repository_id
        or value.get("operation_id") != operation_id
        or value.get("phase") not in {"commit_prepared", "remote_committed"}
    ):
        raise ConfigurationError("control sync transaction is invalid")
    hashes = value.get("document_sha256")
    if (
        not isinstance(hashes, dict)
        or set(hashes) != CONTROL_DOCUMENTS
        or not all(
            isinstance(name, str)
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            for name, digest in hashes.items()
        )
    ):
        raise ConfigurationError("control sync document hashes are invalid")
    prepared = _prepared(value.get("prepared_commit"))
    if (
        prepared.repository_id != repository_id
        or prepared.branch != control_branch
    ):
        raise ConfigurationError("control sync prepared commit identity is invalid")
    return value, prepared


def synchronize_control_worktree(
    project: ProjectConfig,
    *,
    writer: ControlWriter,
    operation_id: str,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if project.collaboration_mode != "collaborative":
        raise WorkflowError("control sync requires a collaborative project")
    if OPERATION_RE.fullmatch(operation_id) is None:
        raise ConfigurationError("control sync operation id is invalid")
    contract = load_control_contract(project.docs_root / "CONTROL.yaml")
    transaction = project.runtime_root / "control-sync" / "transaction.json"
    pending = project.runtime_root / "control-sync" / "pending.json"
    lock = project.runtime_root / "locks" / "control-sync.lock"
    with exclusive_lock(lock, timeout_seconds=120):
        recovered = transaction.exists() or pending.exists()
        if pending.exists():
            try:
                pending_value = json.loads(pending.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ConfigurationError("control pending sync is unreadable") from error
            if (
                not isinstance(pending_value, dict)
                or pending_value.get("schema_version") not in {1, 2, 3}
                or pending_value.get("project_id") != project.project_id
                or pending_value.get("operation_id") != operation_id
            ):
                raise WorkflowError("another control sync recovery is pending")
            if pending_value.get("schema_version") in {2, 3}:
                if (
                    pending_value.get("phase") != "local_mutation_prepared"
                    or not isinstance(pending_value.get("base_document_sha256"), dict)
                ):
                    raise ConfigurationError("control pending mutation is invalid")
                documents = _documents(project.docs_root)
                current_hashes = _hashes(documents)
                if pending_value.get("schema_version") == 3:
                    targets = pending_value.get("target_documents")
                    target_hashes = pending_value.get("target_document_sha256")
                    bases = pending_value["base_document_sha256"]
                    if (
                        not isinstance(targets, dict)
                        or set(targets) != CONTROL_DOCUMENTS
                        or not all(isinstance(content, str) for content in targets.values())
                        or target_hashes != _hashes(targets)
                        or set(bases) != CONTROL_DOCUMENTS
                    ):
                        raise ConfigurationError("control mutation target snapshot is invalid")
                    if current_hashes != target_hashes:
                        if any(
                            current_hashes[name] not in {bases[name], target_hashes[name]}
                            for name in CONTROL_DOCUMENTS
                        ):
                            raise WorkflowError(
                                "control documents diverged during target-bound recovery"
                            )
                        for name in sorted(CONTROL_DOCUMENTS):
                            atomic_write_bytes(
                                project.docs_root / name,
                                targets[name].encode("utf-8"),
                            )
                        documents = _documents(project.docs_root)
                        current_hashes = _hashes(documents)
                    if current_hashes != target_hashes:
                        raise WorkflowError("control mutation target recovery failed")
                if current_hashes == pending_value["base_document_sha256"]:
                    if _git(project.docs_root, "status", "--porcelain=v1"):
                        raise WorkflowError(
                            "control worktree changed without a complete document mutation"
                        )
                    pending.unlink()
                    local_head = _git(project.docs_root, "rev-parse", "HEAD")
                    return {
                        "ok": True,
                        "applied": False,
                        "reason": "mutation_not_applied",
                        "recovered": True,
                        "control_commit": local_head,
                    }
                pending_value = {
                    "schema_version": 1,
                    "project_id": project.project_id,
                    "operation_id": operation_id,
                    "document_sha256": current_hashes,
                }
                atomic_write_bytes(pending, json_bytes(pending_value))
        if recovered:
            if not transaction.exists():
                documents = _documents(project.docs_root)
                if pending_value.get("document_sha256") != _hashes(documents):
                    raise WorkflowError("control documents changed during pending sync recovery")
            else:
                pending_value = None
        if transaction.exists():
            try:
                journal = json.loads(transaction.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ConfigurationError("control sync transaction is unreadable") from error
            journal, prepared = _validate_journal(
                journal,
                project=project,
                repository_id=contract.repository_id,
                control_branch=contract.control_branch,
                operation_id=operation_id,
            )
            documents = _documents(project.docs_root)
            if _hashes(documents) != journal["document_sha256"]:
                raise WorkflowError("control documents changed during remote recovery")
        else:
            documents = _documents(project.docs_root)
            atomic_write_bytes(
                pending,
                json_bytes(
                    {
                        "schema_version": 1,
                        "project_id": project.project_id,
                        "operation_id": operation_id,
                        "document_sha256": _hashes(documents),
                    }
                ),
            )
            status = _git(project.docs_root, "status", "--porcelain=v1")
            if not status:
                local_head = _git(project.docs_root, "rev-parse", "HEAD")
                if writer.read_head() != local_head:
                    raise WorkflowError(
                        "clean control worktree does not match coordinator remote head"
                    )
                pending.unlink()
                return {
                    "ok": True,
                    "applied": False,
                    "reason": "already_synchronized",
                    "recovered": False,
                    "control_commit": local_head,
                }
            local_head = _git(project.docs_root, "rev-parse", "HEAD")
            remote_head = writer.read_head()
            if remote_head != local_head:
                raise WorkflowError("control worktree is not based on coordinator remote head")
            prepared = writer.prepare_documents(
                documents=documents,
                expected_head=remote_head,
                message=f"ARIA: {operation_id}",
            )
            journal = {
                "schema_version": 1,
                "project_id": project.project_id,
                "repository_id": contract.repository_id,
                "operation_id": operation_id,
                "phase": "commit_prepared",
                "document_sha256": _hashes(documents),
                "prepared_commit": asdict(prepared),
            }
            journal, prepared = _validate_journal(
                journal,
                project=project,
                repository_id=contract.repository_id,
                control_branch=contract.control_branch,
                operation_id=operation_id,
            )
            atomic_write_bytes(transaction, json_bytes(journal))
        if journal["phase"] == "commit_prepared":
            writer.install_prepared(prepared)
            journal["phase"] = "remote_committed"
            atomic_write_bytes(transaction, json_bytes(journal))
        _git(
            project.code_root,
            "fetch",
            contract.remote,
            f"+refs/heads/{contract.control_branch}:"
            f"refs/remotes/{contract.remote}/{contract.control_branch}",
            environment=git_environment,
        )
        fetched = _git(
            project.code_root,
            "rev-parse",
            f"refs/remotes/{contract.remote}/{contract.control_branch}",
        )
        if fetched != prepared.commit_sha or writer.read_head() != prepared.commit_sha:
            raise WorkflowError("control remote read-back does not match prepared commit")
        _git(project.docs_root, "reset", "--hard", prepared.commit_sha)
        if _git(project.docs_root, "rev-parse", "HEAD") != prepared.commit_sha:
            raise WorkflowError("control worktree reset read-back failed")
        if _git(project.docs_root, "status", "--porcelain=v1"):
            raise WorkflowError("control worktree remains dirty after synchronization")
        if _hashes(_documents(project.docs_root)) != journal["document_sha256"]:
            raise WorkflowError("control worktree content changed after synchronization")
        transaction.unlink()
        pending.unlink(missing_ok=True)
        return {
            "ok": True,
            "applied": True,
            "reason": None,
            "recovered": recovered,
            "control_commit": prepared.commit_sha,
        }


def recover_pending_control_sync(
    project: ProjectConfig,
    *,
    writer: ControlWriter,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object] | None:
    pending = project.runtime_root / "control-sync" / "pending.json"
    transaction = project.runtime_root / "control-sync" / "transaction.json"
    source = transaction if transaction.exists() else pending
    if not source.exists():
        return None
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("control sync recovery marker is unreadable") from error
    operation_id = value.get("operation_id") if isinstance(value, dict) else None
    if not isinstance(operation_id, str) or OPERATION_RE.fullmatch(operation_id) is None:
        raise ConfigurationError("control sync recovery operation is invalid")
    return synchronize_control_worktree(
        project,
        writer=writer,
        operation_id=operation_id,
        git_environment=git_environment,
    )


def refresh_control_worktree(
    project: ProjectConfig,
    *,
    writer: ControlWriter,
    git_environment: dict[str, str] | None = None,
) -> dict[str, object]:
    if project.collaboration_mode != "collaborative":
        raise WorkflowError("control refresh requires a collaborative project")
    with serialized_control_operation(project):
        contract = load_control_contract(project.docs_root / "CONTROL.yaml")
        if _git(project.docs_root, "status", "--porcelain=v1"):
            raise WorkflowError("control refresh requires a clean worktree")
        _documents(project.docs_root)
        local_head = _git(project.docs_root, "rev-parse", "HEAD")
        remote_head = writer.read_head()
        if remote_head is None or GIT_OID_RE.fullmatch(remote_head) is None:
            raise WorkflowError("control refresh remote head is unavailable")
        _git(
            project.code_root,
            "fetch",
            contract.remote,
            f"+refs/heads/{contract.control_branch}:"
            f"refs/remotes/{contract.remote}/{contract.control_branch}",
            environment=git_environment,
        )
        fetched = _git(
            project.code_root,
            "rev-parse",
            f"refs/remotes/{contract.remote}/{contract.control_branch}",
        )
        if fetched != remote_head:
            raise WorkflowError("control refresh remote read-back mismatch")
        if local_head == remote_head:
            return {
                "ok": True,
                "updated": False,
                "control_commit": local_head,
            }
        merge_base = _git(project.code_root, "merge-base", local_head, remote_head)
        if merge_base != local_head:
            raise WorkflowError("control refresh rejected non-fast-forward history")
        _git(project.docs_root, "reset", "--hard", remote_head)
        if (
            _git(project.docs_root, "rev-parse", "HEAD") != remote_head
            or _git(project.docs_root, "status", "--porcelain=v1")
        ):
            raise WorkflowError("control refresh local read-back failed")
        _documents(project.docs_root)
        return {
            "ok": True,
            "updated": True,
            "control_commit": remote_head,
        }
