from __future__ import annotations

import json
import secrets
import shutil
import subprocess
from pathlib import Path

from aria.collaboration import load_control_contract
from aria.collaborative_doctor import run_collaborative_project_doctor
from aria.collaborative_team import active_team_identities, load_collaborative_team
from aria.errors import ConfigurationError, WorkflowError
from aria.github import parse_github_remote
from aria.github_auth import CLIENT_ID_RE
from aria.github_git import github_git_environment, resolve_github_askpass
from aria.github_runtime import build_authenticated_github_adapter
from aria.github_runtime import build_github_repository_provisioner
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE, default_runtime_root, load_project
from aria.provider import validate_provider_inspection
from aria.registry import register_project


JOIN_SCHEMA_VERSION = 1
JOIN_PHASES = {"prepared", "cloned", "branches_ready", "worktree_ready"}


def _run_git(
    *arguments: str,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise WorkflowError("project join Git operation failed") from error
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Git failed"
        raise WorkflowError(f"project join Git operation failed: {detail}")
    return result


def _branch(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError("project join code branch is invalid")
    result = _run_git("check-ref-format", "--branch", value, check=False)
    if result.returncode != 0:
        raise ConfigurationError("project join code branch is invalid")
    return value


def _journal(value: object, identity: dict[str, object]) -> dict[str, object]:
    legacy_keys = {
        "schema_version",
        "project_id",
        "repository_url_identity",
        "code_root",
        "docs_root",
        "code_branch",
        "client_id",
        "coordinator_integration_id",
        "phase",
    }
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(legacy_keys),
        frozenset({*legacy_keys, "staging_root"}),
    }:
        raise ConfigurationError("project join transaction is invalid")
    if (
        value.get("schema_version") != JOIN_SCHEMA_VERSION
        or value.get("phase") not in JOIN_PHASES
        or any(value.get(key) != expected for key, expected in identity.items())
    ):
        raise ConfigurationError("project join transaction identity is invalid")
    return value


def _save(path: Path, value: dict[str, object]) -> None:
    atomic_write_bytes(path, json_bytes(value))


def _load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("project join transaction is unreadable") from error


def _clone_staging_root(code: Path, journal: dict[str, object]) -> Path:
    raw = journal.get("staging_root")
    if not isinstance(raw, str):
        raise ConfigurationError("project join staging identity is invalid")
    staging = Path(raw)
    if (
        not staging.is_absolute()
        or staging.parent != code.parent
        or not staging.name.startswith(f".{code.name}.aria-join-")
    ):
        raise ConfigurationError("project join staging identity is invalid")
    return staging


def _prepare_clone_staging(
    *, code: Path, staging: Path, project_id: str, repository_identity: str
) -> Path:
    marker = staging / "owner.json"
    expected = {
        "schema_version": 1,
        "operation": "project-join-clone",
        "project_id": project_id,
        "repository_url_identity": repository_identity,
        "code_root": str(code),
    }
    if staging.exists():
        if (
            not staging.is_dir()
            or staging.is_symlink()
            or getattr(staging, "is_junction", lambda: False)()
        ):
            raise WorkflowError("project join staging path is unsafe")
        inventory = {path.name for path in staging.iterdir()}
        if marker.exists():
            if _load(marker) != expected or not inventory.issubset({"owner.json", "checkout"}):
                raise WorkflowError("project join staging ownership mismatch")
        elif inventory:
            raise WorkflowError("project join staging ownership is unproven")
        else:
            _save(marker, expected)
    else:
        staging.mkdir()
        _save(marker, expected)
    checkout = staging / "checkout"
    if checkout.exists():
        if checkout.is_symlink() or getattr(checkout, "is_junction", lambda: False)():
            raise WorkflowError("project join staged checkout is unsafe")
        shutil.rmtree(checkout)
    return checkout


def join_collaborative_project(
    *,
    project_id: str,
    repository_url: str,
    code_root: Path,
    docs_root: Path | None,
    code_branch: str | None,
    client_id: str,
    coordinator_integration_id: int,
    runtime_root: Path | None = None,
    askpass_path: Path | None = None,
    provisioner: object | None = None,
) -> dict[str, object]:
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    repository = parse_github_remote(repository_url)
    if CLIENT_ID_RE.fullmatch(client_id) is None:
        raise ConfigurationError("project join GitHub client id is invalid")
    if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
        raise ConfigurationError("coordinator integration id must be a positive integer")
    service = provisioner or build_github_repository_provisioner(client_id=client_id)
    actor = service.actor()
    expected_code_branch = f"work/{actor.username_snapshot}"
    if code_branch is None:
        code_branch = expected_code_branch
    code_branch = _branch(code_branch)
    if code_branch != expected_code_branch:
        raise ConfigurationError(
            "project join code branch must exactly match work/<authenticated-github-username>"
        )
    code = code_root.absolute()
    docs = (docs_root or code.with_name(f"{code.name}-aria-control")).absolute()
    if code == docs or code.is_relative_to(docs) or docs.is_relative_to(code):
        raise ConfigurationError("project join code and control roots must be separate")
    if not code.parent.is_dir() or not docs.parent.is_dir():
        raise ConfigurationError("project join destination parent is unavailable")
    askpass = resolve_github_askpass(askpass_path)
    runtime = (runtime_root or default_runtime_root()).absolute()
    transaction = runtime / "collaboration-join" / project_id / "transaction.json"
    lock = runtime / "locks" / f"collaboration-join-{project_id}.lock"
    repository_identity = f"github:{repository.owner.casefold()}/{repository.name.casefold()}"
    identity = {
        "project_id": project_id,
        "repository_url_identity": repository_identity,
        "code_root": str(code),
        "docs_root": str(docs),
        "code_branch": code_branch,
        "client_id": client_id,
        "coordinator_integration_id": coordinator_integration_id,
    }
    environment = github_git_environment(client_id=client_id, askpass=askpass)
    with exclusive_lock(lock, timeout_seconds=120):
        if transaction.exists():
            journal = _journal(_load(transaction), identity)
            recovered = True
        else:
            if code.exists() or docs.exists():
                raise WorkflowError("project join destination already exists")
            journal = {
                "schema_version": JOIN_SCHEMA_VERSION,
                **identity,
                "staging_root": str(
                    code.with_name(f".{code.name}.aria-join-{secrets.token_hex(8)}")
                ),
                "phase": "prepared",
            }
            _save(transaction, journal)
            recovered = False
        if journal["phase"] == "prepared" and "staging_root" not in journal:
            if code.exists():
                if not (code / ".git").is_dir():
                    raise WorkflowError(
                        "legacy partial join destination requires explicit manual recovery"
                    )
            else:
                journal["staging_root"] = str(
                    code.with_name(f".{code.name}.aria-join-{secrets.token_hex(8)}")
                )
                _save(transaction, journal)
        if journal["phase"] == "prepared":
            if code.exists():
                if not (code / ".git").is_dir():
                    raise WorkflowError("project join destination contains unowned files")
            else:
                staging = _clone_staging_root(code, journal)
                checkout = _prepare_clone_staging(
                    code=code,
                    staging=staging,
                    project_id=project_id,
                    repository_identity=repository_identity,
                )
                _run_git(
                    "clone",
                    "--no-checkout",
                    "--origin",
                    "origin",
                    repository_url,
                    str(checkout),
                    environment=environment,
                )
                remote_url = _run_git(
                    "config", "--get", "remote.origin.url", cwd=checkout,
                    environment=environment,
                ).stdout.strip()
                staged_repository = parse_github_remote(remote_url)
                if (
                    staged_repository.owner.casefold() != repository.owner.casefold()
                    or staged_repository.name.casefold() != repository.name.casefold()
                ):
                    raise WorkflowError("staged GitHub repository identity mismatch")
                checkout.rename(code)
                (staging / "owner.json").unlink()
                staging.rmdir()
            remote_url = _run_git(
                "config", "--get", "remote.origin.url", cwd=code, environment=environment
            ).stdout.strip()
            remote_repository = parse_github_remote(remote_url)
            if (
                remote_repository.owner.casefold() != repository.owner.casefold()
                or remote_repository.name.casefold() != repository.name.casefold()
            ):
                raise WorkflowError("cloned GitHub repository identity mismatch")
            journal["phase"] = "cloned"
            _save(transaction, journal)
        if journal["phase"] == "cloned":
            _run_git(
                "fetch",
                "origin",
                "+refs/heads/aria-control:refs/heads/aria-control",
                "+refs/heads/dev:refs/remotes/origin/dev",
                cwd=code,
                environment=environment,
            )
            remote_exists = _run_git(
                "ls-remote", "--exit-code", "--heads", "origin",
                f"refs/heads/{code_branch}", cwd=code,
                environment=environment, check=False,
            ).returncode == 0
            if remote_exists:
                _run_git(
                    "fetch", "origin",
                    f"+refs/heads/{code_branch}:refs/remotes/origin/{code_branch}",
                    cwd=code, environment=environment,
                )
            remote_branch = _run_git(
                "show-ref",
                "--verify",
                f"refs/remotes/origin/{code_branch}",
                cwd=code,
                check=False,
            )
            start = f"origin/{code_branch}" if remote_branch.returncode == 0 else "origin/dev"
            _run_git("checkout", "-B", code_branch, start, cwd=code)
            if not remote_exists:
                _run_git(
                    "push", "-u", "origin", code_branch,
                    cwd=code, environment=environment,
                )
                readback = _run_git(
                    "ls-remote", "--heads", "origin", f"refs/heads/{code_branch}",
                    cwd=code, environment=environment,
                ).stdout.split()
                if not readback or readback[0] != _run_git(
                    "rev-parse", "HEAD", cwd=code
                ).stdout.strip():
                    raise WorkflowError("project join working branch read-back mismatch")
            journal["phase"] = "branches_ready"
            _save(transaction, journal)
        if journal["phase"] == "branches_ready":
            if docs.exists():
                branch = _run_git(
                    "rev-parse", "--abbrev-ref", "HEAD", cwd=docs
                ).stdout.strip()
                if branch != "aria-control":
                    raise WorkflowError("existing control worktree uses another branch")
            else:
                _run_git("worktree", "add", str(docs), "aria-control", cwd=code)
            journal["phase"] = "worktree_ready"
            _save(transaction, journal)
        contract = load_control_contract(docs / "CONTROL.yaml")
        if (
            contract.project_id != project_id
            or contract.provider != "github"
            or contract.remote != "origin"
            or contract.control_branch != "aria-control"
            or contract.integration_branch != "dev"
        ):
            raise WorkflowError("remote collaborative control contract does not match join")
        adapter = build_authenticated_github_adapter(
            code_root=code,
            remote="origin",
            client_id=client_id,
            coordinator_integration_id=coordinator_integration_id,
        )
        inspection = adapter.inspect_collaboration(
            repository_id=contract.repository_id,
            control_branch=contract.control_branch,
        )
        validate_provider_inspection(
            inspection,
            expected_provider="github",
            expected_repository_id=contract.repository_id,
        )
        if not inspection.membership.active:
            raise WorkflowError("joining GitHub user is not an active project member")
        if not inspection.protection.coordinator_only:
            raise WorkflowError("joined control branch is not coordinator-only protected")
        team = load_collaborative_team(docs / "ARIA_TEAM.yaml")
        if inspection.actor.user_id not in {
            identity.actor.user_id for identity in active_team_identities(team)
        }:
            raise WorkflowError("joining GitHub user is absent from ARIA_TEAM.yaml")
        registered = register_project(
            project_id,
            docs_root=docs,
            code_root=code,
            runtime_root=runtime,
        )
        project = load_project(
            project_id,
            runtime_root=runtime,
        )
        doctor = run_collaborative_project_doctor(project)
        if doctor.get("ok") is not True:
            failed = [
                row["id"]
                for row in doctor.get("checks", [])
                if row.get("blocking") and not row.get("ok")
            ]
            raise WorkflowError(f"joined project doctor failed: {failed}")
        transaction.unlink()
        return {
            **registered,
            "collaboration": "joined",
            "repository_id": contract.repository_id,
            "code_branch": code_branch,
            "control_branch": contract.control_branch,
            "control_commit": _run_git("rev-parse", "HEAD", cwd=docs).stdout.strip(),
            "actor": inspection.actor.as_mapping(),
            "recovered": recovered,
            "doctor_ok": True,
        }
