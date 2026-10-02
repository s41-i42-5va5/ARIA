from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
from dataclasses import asdict
from pathlib import Path

from aria.collaboration import collaboration_plan, enable_collaboration
from aria.collaboration import load_control_contract
from aria.collaborative_doctor import run_collaborative_project_doctor
from aria.errors import ConfigurationError, WorkflowError
from aria.github import GitHubRepository, parse_github_remote
from aria.github_auth import CLIENT_ID_RE
from aria.github_git import github_git_environment, resolve_github_askpass
from aria.github_repository import GitHubRepositoryProvisioner
from aria.github_runtime import (
    build_authenticated_github_adapter,
    build_github_control_writer,
    build_github_repository_provisioner,
)
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE, default_runtime_root, load_project
from aria.provider import validate_provider_inspection
from aria.registry import register_project
from aria.github_control import CONTROL_DOCUMENTS
from aria.project_create import _paths
from aria.project_join import _run_git


CONNECT_SCHEMA_VERSION = 1
CONNECT_PHASES = {"prepared", "cloned", "branches_ready"}


def _load(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("project connect transaction is unreadable") from error
    if not isinstance(value, dict):
        raise ConfigurationError("project connect transaction is invalid")
    return value


def _save(path: Path, value: dict[str, object]) -> None:
    atomic_write_bytes(path, json_bytes(value))


def _identity(
    *, project_id: str, repository: GitHubRepository, code: Path, docs: Path,
    client_id: str, coordinator_integration_id: int, base_branch: str | None,
) -> dict[str, object]:
    return {
        "project_id": project_id,
        "repository": {"owner": repository.owner, "name": repository.name},
        "code_root": str(code),
        "docs_root": str(docs),
        "client_id": client_id,
        "coordinator_integration_id": coordinator_integration_id,
        "base_branch": base_branch,
    }


def connect_existing_project_plan(
    *, project_id: str, repository_url: str, code_root: Path,
    docs_root: Path | None, client_id: str, coordinator_integration_id: int,
    base_branch: str | None = None,
    provisioner: GitHubRepositoryProvisioner | None = None,
) -> dict[str, object]:
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    if CLIENT_ID_RE.fullmatch(client_id) is None:
        raise ConfigurationError("project connect GitHub client id is invalid")
    if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
        raise ConfigurationError("coordinator integration id must be positive")
    repository = parse_github_remote(repository_url)
    code, docs = _paths(code_root, docs_root)
    blockers: list[dict[str, str]] = []
    if code.exists():
        blockers.append({"code": "CODE_ROOT_EXISTS", "message": "code root exists"})
    if docs.exists():
        blockers.append({"code": "DOCS_ROOT_EXISTS", "message": "control root exists"})
    if not code.parent.is_dir() or not docs.parent.is_dir():
        blockers.append({"code": "DESTINATION_PARENT_MISSING", "message": "destination parent is missing"})
    service = provisioner or build_github_repository_provisioner(client_id=client_id)
    actor = service.actor()
    existing = service.read_existing(repository)
    read_heads = getattr(service, "read_branch_heads", None)
    branch_heads = read_heads(repository) if callable(read_heads) else {}
    agreed_base = base_branch or existing.default_branch
    if not isinstance(agreed_base, str) or not agreed_base or agreed_base.startswith("-"):
        raise ConfigurationError("project connect base branch is invalid")
    body = {
        "schema_version": CONNECT_SCHEMA_VERSION,
        "operation": "connect-existing-collaborative-project",
        **_identity(
            project_id=project_id, repository=repository, code=code, docs=docs,
            client_id=client_id,
            coordinator_integration_id=coordinator_integration_id,
            base_branch=agreed_base,
        ),
        "repository_id": existing.repository_id,
        "private": existing.private,
        "default_branch": existing.default_branch,
        "actor": actor.as_mapping(),
        "planned_branches": {
            "main": "preserve-or-create-from-agreed-base",
            "dev": "preserve-or-create-from-main",
            "aria-control": "preserve-only-if-valid-or-create",
        },
        "observed_branch_heads": branch_heads,
        "blockers": blockers,
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"ok": not blockers, "read_only": True, "plan_sha256": digest, **body}


def _remote_ref(code: Path, branch: str) -> str | None:
    result = _run_git(
        "show-ref", "--hash", "--verify", f"refs/remotes/origin/{branch}",
        cwd=code, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _clone_staging_root(code: Path, journal: dict[str, object]) -> Path:
    raw = journal.get("staging_root")
    if not isinstance(raw, str):
        raise ConfigurationError("project connect staging identity is invalid")
    staging = Path(raw)
    if (
        not staging.is_absolute()
        or staging.parent != code.parent
        or not staging.name.startswith(f".{code.name}.aria-connect-")
    ):
        raise ConfigurationError("project connect staging identity is invalid")
    return staging


def _prepare_clone_staging(
    *, code: Path, staging: Path, plan_sha256: str
) -> Path:
    marker = staging / "owner.json"
    expected = {
        "schema_version": 1,
        "operation": "project-connect-clone",
        "code_root": str(code),
        "plan_sha256": plan_sha256,
    }
    if staging.exists():
        if (
            not staging.is_dir()
            or staging.is_symlink()
            or getattr(staging, "is_junction", lambda: False)()
        ):
            raise WorkflowError("project connect staging path is unsafe")
        inventory = {path.name for path in staging.iterdir()}
        if marker.exists():
            if _load(marker) != expected or not inventory.issubset({"owner.json", "checkout"}):
                raise WorkflowError("project connect staging ownership mismatch")
        elif inventory:
            raise WorkflowError("project connect staging ownership is unproven")
        else:
            _save(marker, expected)
    else:
        staging.mkdir()
        _save(marker, expected)
    checkout = staging / "checkout"
    if checkout.exists():
        if checkout.is_symlink() or getattr(checkout, "is_junction", lambda: False)():
            raise WorkflowError("project connect staged checkout is unsafe")
        shutil.rmtree(checkout)
    return checkout


def connect_existing_project(
    *, project_id: str, repository_url: str, code_root: Path,
    docs_root: Path | None, client_id: str, coordinator_integration_id: int,
    expected_plan_sha256: str, confirm: bool, base_branch: str | None = None,
    runtime_root: Path | None = None, askpass_path: Path | None = None,
    provisioner: GitHubRepositoryProvisioner | None = None,
) -> dict[str, object]:
    if not confirm:
        raise WorkflowError("project connect requires explicit confirmation")
    if re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) is None:
        raise ConfigurationError("expected_plan_sha256 must be a lowercase SHA-256")
    repository = parse_github_remote(repository_url)
    code, docs = _paths(code_root, docs_root)
    runtime = (runtime_root or default_runtime_root()).absolute()
    transaction = runtime / "collaboration-connect" / project_id / "transaction.json"
    lock = runtime / "locks" / f"collaboration-connect-{project_id}.lock"
    service = provisioner or build_github_repository_provisioner(client_id=client_id)
    if transaction.exists():
        resumed = _load(transaction)
        if not hmac.compare_digest(
            str(resumed.get("plan_sha256")), expected_plan_sha256
        ):
            raise WorkflowError("project connect plan is stale")
        agreed_base = str(resumed.get("base_branch"))
        receipt = resumed.get("repository_receipt")
        if not isinstance(receipt, dict) or not isinstance(receipt.get("repository_id"), str):
            raise ConfigurationError("project connect repository receipt is invalid")
        repository_id = str(receipt["repository_id"])
    else:
        plan = connect_existing_project_plan(
            project_id=project_id, repository_url=repository_url, code_root=code,
            docs_root=docs, client_id=client_id,
            coordinator_integration_id=coordinator_integration_id,
            base_branch=base_branch, provisioner=service,
        )
        if not hmac.compare_digest(str(plan["plan_sha256"]), expected_plan_sha256):
            raise WorkflowError("project connect plan is stale")
        if plan["blockers"]:
            raise WorkflowError(f"project connect blocked: {plan['blockers']}")
        agreed_base = str(plan["base_branch"])
        repository_id = str(plan["repository_id"])
    identity = _identity(
        project_id=project_id, repository=repository, code=code, docs=docs,
        client_id=client_id, coordinator_integration_id=coordinator_integration_id,
        base_branch=agreed_base,
    )
    environment = github_git_environment(
        client_id=client_id, askpass=resolve_github_askpass(askpass_path)
    )
    with exclusive_lock(lock, timeout_seconds=120):
        if transaction.exists():
            journal = _load(transaction)
            if (
                journal.get("schema_version") != CONNECT_SCHEMA_VERSION
                or journal.get("phase") not in CONNECT_PHASES
                or journal.get("plan_sha256") != expected_plan_sha256
                or any(journal.get(key) != value for key, value in identity.items())
            ):
                raise ConfigurationError("project connect transaction identity is invalid")
            recovered = True
        else:
            journal = {
                "schema_version": CONNECT_SCHEMA_VERSION,
                "plan_sha256": expected_plan_sha256,
                **identity,
                "repository_receipt": {
                    **asdict(service.read_existing(repository)),
                    "repository": {"owner": repository.owner, "name": repository.name},
                },
                "phase": "prepared",
                "staging_root": str(
                    code.with_name(
                        f".{code.name}.aria-connect-{secrets.token_hex(8)}"
                    )
                ),
            }
            _save(transaction, journal)
            recovered = False
        if journal["phase"] == "prepared" and "staging_root" not in journal:
            if code.exists():
                if not (code / ".git").is_dir():
                    raise WorkflowError(
                        "legacy partial connect destination requires explicit manual recovery"
                    )
            else:
                journal["staging_root"] = str(
                    code.with_name(
                        f".{code.name}.aria-connect-{secrets.token_hex(8)}"
                    )
                )
                _save(transaction, journal)
        if journal["phase"] == "prepared":
            if not code.exists():
                staging = _clone_staging_root(code, journal)
                checkout = _prepare_clone_staging(
                    code=code,
                    staging=staging,
                    plan_sha256=expected_plan_sha256,
                )
                _run_git(
                    "clone", "--no-checkout", "--origin", "origin", repository_url,
                    str(checkout),
                    environment=environment,
                )
                cloned = parse_github_remote(
                    _run_git(
                        "config", "--get", "remote.origin.url", cwd=checkout
                    ).stdout.strip()
                )
                if cloned != repository:
                    raise WorkflowError("project connect staged repository identity mismatch")
                checkout.rename(code)
                (staging / "owner.json").unlink()
                staging.rmdir()
            elif not (code / ".git").is_dir():
                raise WorkflowError("project connect destination contains unowned files")
            cloned = parse_github_remote(
                _run_git("config", "--get", "remote.origin.url", cwd=code).stdout.strip()
            )
            if cloned != repository:
                raise WorkflowError("project connect cloned repository identity mismatch")
            journal["phase"] = "cloned"
            _save(transaction, journal)
        if journal["phase"] == "cloned":
            _run_git(
                "fetch", "origin", "+refs/heads/*:refs/remotes/origin/*",
                cwd=code, environment=environment,
            )
            base_commit = _remote_ref(code, agreed_base)
            if base_commit is None:
                raise WorkflowError("agreed existing-repository base branch is missing")
            main_commit = _remote_ref(code, "main")
            if main_commit is None:
                _run_git("branch", "main", base_commit, cwd=code)
                _run_git("push", "origin", "main:main", cwd=code, environment=environment)
                main_commit = _remote_ref(code, "main")
            dev_commit = _remote_ref(code, "dev")
            if dev_commit is None:
                _run_git("branch", "dev", main_commit, cwd=code)
                _run_git("push", "origin", "dev:dev", cwd=code, environment=environment)
                dev_commit = _remote_ref(code, "dev")
            if main_commit is None or dev_commit is None:
                raise WorkflowError("project connect branch read-back failed")
            _run_git("checkout", "-B", "dev", "origin/dev", cwd=code)
            journal["phase"] = "branches_ready"
            _save(transaction, journal)
        service.ensure_integration_protection(
            repository=repository,
            branch="dev",
            coordinator_integration_id=coordinator_integration_id,
        )
        service.ensure_queue_labels(repository=repository)
        adapter = build_authenticated_github_adapter(
            code_root=code, remote="origin", client_id=client_id,
            coordinator_integration_id=coordinator_integration_id,
        )
        writer = build_github_control_writer(
            code_root=code, remote="origin", repository_id=repository_id,
            app_id=coordinator_integration_id,
        )
        remote_control = _remote_ref(code, "aria-control")
        if remote_control is not None:
            if not docs.exists():
                _run_git(
                    "worktree", "add", "--track", "-b", "aria-control",
                    str(docs), "origin/aria-control", cwd=code,
                )
            contract = load_control_contract(docs / "CONTROL.yaml")
            inventory = {path.name for path in docs.iterdir() if path.name != ".git"}
            if (
                contract.project_id != project_id
                or contract.provider != "github"
                or contract.repository_id != repository_id
                or contract.remote != "origin"
                or contract.integration_branch != "dev"
                or contract.control_branch != "aria-control"
                or inventory != CONTROL_DOCUMENTS
            ):
                raise WorkflowError(
                    "existing aria-control branch is not a valid matching ARIA control plane"
                )
            inspection = adapter.inspect_collaboration(
                repository_id=repository_id, control_branch="aria-control"
            )
            validate_provider_inspection(
                inspection,
                expected_provider="github",
                expected_repository_id=repository_id,
            )
            if not inspection.membership.active:
                raise WorkflowError("connecting user is not an active repository member")
            if not inspection.protection.coordinator_only:
                ensure = getattr(adapter, "ensure_control_protection", None)
                if not callable(ensure):
                    raise WorkflowError("existing aria-control is not coordinator-only protected")
                ensure(repository_id=repository_id, control_branch="aria-control")
                inspection = adapter.inspect_collaboration(
                    repository_id=repository_id, control_branch="aria-control"
                )
                if not inspection.protection.coordinator_only:
                    raise WorkflowError("aria-control protection read-back failed")
            register_project(
                project_id, docs_root=docs, code_root=code, runtime_root=runtime
            )
            project = load_project(project_id, runtime_root=runtime)
            doctor = run_collaborative_project_doctor(project)
            if doctor.get("ok") is not True:
                raise WorkflowError("connected existing control plane doctor failed")
            transaction.unlink()
            return {
                "ok": True,
                "project": project_id,
                "collaboration": "connected",
                "repository": f"{repository.owner}/{repository.name}",
                "repository_id": repository_id,
                "preserved_existing_code": True,
                "attached_existing_control": True,
                "control_commit": remote_control,
                "main_commit": _remote_ref(code, "main"),
                "dev_commit": _remote_ref(code, "dev"),
                "recovered": recovered,
                "doctor_ok": True,
            }
        collaboration = collaboration_plan(
            project_id=project_id, code_root=code, docs_root=docs,
            provider="github", repository_id=repository_id, provider_adapter=adapter,
        )
        enabled = enable_collaboration(
            project_id=project_id, code_root=code, docs_root=docs,
            provider="github", repository_id=repository_id,
            expected_plan_sha256=str(collaboration["plan_sha256"]), confirm=True,
            provider_adapter=adapter, control_writer=writer, runtime_root=runtime,
            git_environment=environment,
            coordinator_integration_id=coordinator_integration_id,
        )
        project = load_project(project_id, runtime_root=runtime)
        doctor = run_collaborative_project_doctor(project)
        if doctor.get("ok") is not True:
            raise WorkflowError("connected project doctor failed")
        transaction.unlink()
        return {
            **enabled,
            "repository": f"{repository.owner}/{repository.name}",
            "repository_id": repository_id,
            "preserved_existing_code": True,
            "main_commit": _remote_ref(code, "main"),
            "dev_commit": _remote_ref(code, "dev"),
            "recovered": recovered or bool(enabled.get("recovered")),
            "doctor_ok": True,
        }
