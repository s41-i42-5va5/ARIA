from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict
from pathlib import Path

from aria.collaboration import collaboration_plan, enable_collaboration
from aria.collaborative_doctor import run_collaborative_project_doctor
from aria.errors import ConfigurationError, WorkflowError
from aria.github import GitHubRepository, parse_github_remote
from aria.github_auth import CLIENT_ID_RE
from aria.github_repository import GitHubRepositoryProvisioner, ProvisionedGitHubRepository
from aria.github_runtime import (
    build_authenticated_github_adapter,
    build_github_control_writer,
    build_github_repository_provisioner,
)
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.project import PROJECT_ID_RE, default_runtime_root, load_project
from aria.github_git import github_git_environment, resolve_github_askpass
from aria.project_join import _run_git


CREATE_SCHEMA_VERSION = 1
CREATE_PHASES = {"prepared", "repository_created", "local_committed", "dev_pushed"}
CREATE_JOURNAL_KEYS = {
    "schema_version", "plan_sha256", "phase", "project_id", "owner",
    "repository_name", "private", "code_root", "docs_root", "client_id",
    "coordinator_integration_id", "repository",
}
REPOSITORY_RECEIPT_KEYS = {
    "repository", "repository_id", "clone_url", "private", "default_branch"
}


def _paths(code_root: Path, docs_root: Path | None) -> tuple[Path, Path]:
    code = code_root.absolute()
    docs = (docs_root or code.with_name(f"{code.name}-aria-control")).absolute()
    if code == docs or code.is_relative_to(docs) or docs.is_relative_to(code):
        raise ConfigurationError("project create code and control roots must be separate")
    return code, docs


def _identity(
    *, project_id: str, owner: str, repository_name: str, private: bool,
    code: Path, docs: Path, client_id: str, coordinator_integration_id: int,
) -> dict[str, object]:
    return {
        "project_id": project_id,
        "owner": owner,
        "repository_name": repository_name,
        "private": private,
        "code_root": str(code),
        "docs_root": str(docs),
        "client_id": client_id,
        "coordinator_integration_id": coordinator_integration_id,
    }


def _load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("project create transaction is unreadable") from error


def _save(path: Path, value: dict[str, object]) -> None:
    atomic_write_bytes(path, json_bytes(value))


def _receipt(
    value: object, *, repository: GitHubRepository, private: bool,
) -> ProvisionedGitHubRepository:
    if not isinstance(value, dict) or set(value) != REPOSITORY_RECEIPT_KEYS:
        raise ConfigurationError("project create repository receipt is invalid")
    repository_value = value.get("repository")
    if not isinstance(repository_value, dict) or set(repository_value) != {"owner", "name"}:
        raise ConfigurationError("project create repository identity is invalid")
    repository_id = value.get("repository_id")
    clone_url = value.get("clone_url")
    default_branch = value.get("default_branch")
    if (
        repository_value != {"owner": repository.owner, "name": repository.name}
        or not isinstance(repository_id, str)
        or not repository_id.isdecimal()
        or int(repository_id) <= 0
        or clone_url != f"https://github.com/{repository.owner}/{repository.name}.git"
        or value.get("private") is not private
        or default_branch != "main"
    ):
        raise ConfigurationError("project create repository receipt mismatch")
    return ProvisionedGitHubRepository(
        repository, repository_id, clone_url, private, default_branch
    )


def _journal(
    value: object, *, identity: dict[str, object], expected_plan_sha256: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != CREATE_JOURNAL_KEYS:
        raise ConfigurationError("project create transaction is invalid")
    if (
        value.get("schema_version") != CREATE_SCHEMA_VERSION
        or value.get("phase") not in CREATE_PHASES
        or value.get("plan_sha256") != expected_plan_sha256
        or any(value.get(key) != expected for key, expected in identity.items())
    ):
        raise ConfigurationError("project create transaction identity is invalid")
    if value["phase"] == "prepared":
        if value.get("repository") is not None:
            raise ConfigurationError("prepared project create has a repository receipt")
    else:
        _receipt(
            value.get("repository"),
            repository=GitHubRepository(str(identity["owner"]), str(identity["repository_name"])),
            private=identity["private"],
        )
    return value


def _validate_partial_checkout(
    *, code: Path, repository: GitHubRepository, expected_readme: str,
) -> None:
    if not (code / ".git").is_dir():
        raise WorkflowError("partial project checkout is not recoverable")
    remote = _run_git("config", "--get", "remote.origin.url", cwd=code, check=False)
    if remote.returncode == 0:
        actual = parse_github_remote(remote.stdout.strip())
        if (
            actual.owner.casefold() != repository.owner.casefold()
            or actual.name.casefold() != repository.name.casefold()
        ):
            raise WorkflowError("partial project checkout remote identity mismatch")
    elif _run_git("remote", cwd=code).stdout.strip():
        raise WorkflowError("partial project checkout has unexpected Git remotes")
    readme = code / "README.md"
    if readme.exists():
        try:
            content = readme.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise WorkflowError("partial project README is unreadable") from error
        if content != expected_readme:
            raise WorkflowError("partial project README content mismatch")


def _ensure_initial_commit(
    *, code: Path, created: ProvisionedGitHubRepository, actor_name: str,
    actor_email: str, repository_name: str,
) -> str:
    expected_readme = f"# {repository_name}\n"
    if code.exists():
        if (
            not code.is_dir()
            or code.is_symlink()
            or getattr(code, "is_junction", lambda: False)()
        ):
            raise WorkflowError("partial project checkout path is unsafe")
        if not (code / ".git").is_dir():
            inventory = {path.name for path in code.iterdir()}
            if not inventory.issubset({"README.md"}):
                raise WorkflowError("partial project checkout has unexpected files")
            if "README.md" in inventory:
                try:
                    if (code / "README.md").read_text(encoding="utf-8") != expected_readme:
                        raise WorkflowError("partial project README content mismatch")
                except (OSError, UnicodeDecodeError) as error:
                    raise WorkflowError("partial project README is unreadable") from error
            _run_git("init", "-q", cwd=code)
        _validate_partial_checkout(
            code=code, repository=created.repository, expected_readme=expected_readme
        )
    else:
        code.mkdir()
        _run_git("init", "-q", cwd=code)
    _run_git("config", "user.name", actor_name, cwd=code)
    _run_git("config", "user.email", actor_email, cwd=code)
    readme = code / "README.md"
    if not readme.exists():
        readme.write_text(expected_readme, encoding="utf-8")
    head = _run_git("rev-parse", "--verify", "HEAD", cwd=code, check=False)
    if head.returncode != 0:
        _run_git("add", "README.md", cwd=code)
        _run_git("commit", "-m", "Initial project", cwd=code)
    else:
        status = _run_git(
            "status", "--porcelain=v1", "--untracked-files=all", cwd=code
        ).stdout.strip()
        files = _run_git("ls-tree", "-r", "--name-only", "HEAD", cwd=code).stdout.splitlines()
        committed = _run_git("show", "HEAD:README.md", cwd=code, check=False)
        if status or files != ["README.md"] or committed.stdout != expected_readme:
            raise WorkflowError("partial project initial commit mismatch")
    initial_commit = _run_git("rev-parse", "HEAD", cwd=code).stdout.strip()
    _run_git("branch", "-f", "main", initial_commit, cwd=code)
    _run_git("branch", "-M", "dev", cwd=code)
    remote = _run_git("config", "--get", "remote.origin.url", cwd=code, check=False)
    if remote.returncode != 0:
        _run_git("remote", "add", "origin", created.clone_url, cwd=code)
    else:
        actual = parse_github_remote(remote.stdout.strip())
        if (
            actual.owner.casefold() != created.repository.owner.casefold()
            or actual.name.casefold() != created.repository.name.casefold()
        ):
            raise WorkflowError("project create origin identity mismatch")
    return initial_commit


def _read_initial_commit(
    *, code: Path, repository: GitHubRepository, repository_name: str,
) -> str:
    expected_readme = f"# {repository_name}\n"
    _validate_partial_checkout(
        code=code, repository=repository, expected_readme=expected_readme
    )
    branch = _run_git("branch", "--show-current", cwd=code).stdout.strip()
    status = _run_git(
        "status", "--porcelain=v1", "--untracked-files=all", cwd=code
    ).stdout.strip()
    files = _run_git("ls-tree", "-r", "--name-only", "HEAD", cwd=code).stdout.splitlines()
    committed = _run_git("show", "HEAD:README.md", cwd=code, check=False)
    if (
        branch != "dev"
        or status
        or files != ["README.md"]
        or committed.returncode != 0
        or committed.stdout != expected_readme
    ):
        raise WorkflowError("project create initial checkout mismatch")
    return _run_git("rev-parse", "HEAD", cwd=code).stdout.strip()


def create_collaborative_project_plan(
    *, project_id: str, owner: str, repository_name: str, code_root: Path,
    docs_root: Path | None, private: bool, client_id: str,
    coordinator_integration_id: int,
    provisioner: GitHubRepositoryProvisioner | None = None,
) -> dict[str, object]:
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    repository = GitHubRepository(owner, repository_name)
    if type(private) is not bool or CLIENT_ID_RE.fullmatch(client_id) is None:
        raise ConfigurationError("project create configuration is invalid")
    if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
        raise ConfigurationError("coordinator integration id must be positive")
    code, docs = _paths(code_root, docs_root)
    blockers: list[dict[str, str]] = []
    if code.exists():
        blockers.append({"code": "CODE_ROOT_EXISTS", "message": "code root exists"})
    if docs.exists():
        blockers.append({"code": "DOCS_ROOT_EXISTS", "message": "control root exists"})
    if not code.parent.is_dir():
        blockers.append({"code": "CODE_PARENT_MISSING", "message": "code parent is missing"})
    if not docs.parent.is_dir():
        blockers.append({"code": "DOCS_PARENT_MISSING", "message": "control parent is missing"})
    service = provisioner or build_github_repository_provisioner(client_id=client_id)
    actor = service.actor()
    if service.exists(repository):
        blockers.append({"code": "REPOSITORY_EXISTS", "message": "repository exists"})
    body = {
        "schema_version": CREATE_SCHEMA_VERSION,
        "operation": "create-collaborative-project",
        **_identity(
            project_id=project_id, owner=repository.owner,
            repository_name=repository.name, private=private, code=code, docs=docs,
            client_id=client_id, coordinator_integration_id=coordinator_integration_id,
        ),
        "actor": actor.as_mapping(),
        "blockers": blockers,
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"ok": not blockers, "read_only": True, "plan_sha256": digest, **body}


def create_collaborative_project(
    *, project_id: str, owner: str, repository_name: str, code_root: Path,
    docs_root: Path | None, private: bool, client_id: str,
    coordinator_integration_id: int, expected_plan_sha256: str, confirm: bool,
    runtime_root: Path | None = None, askpass_path: Path | None = None,
    provisioner: GitHubRepositoryProvisioner | None = None,
) -> dict[str, object]:
    if not confirm:
        raise WorkflowError("project create requires explicit confirmation")
    if re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) is None:
        raise ConfigurationError("expected_plan_sha256 must be a lowercase SHA-256")
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    if type(private) is not bool or CLIENT_ID_RE.fullmatch(client_id) is None:
        raise ConfigurationError("project create configuration is invalid")
    if type(coordinator_integration_id) is not int or coordinator_integration_id <= 0:
        raise ConfigurationError("coordinator integration id must be positive")
    code, docs = _paths(code_root, docs_root)
    runtime = (runtime_root or default_runtime_root()).absolute()
    transaction = runtime / "collaboration-create" / project_id / "transaction.json"
    lock = runtime / "locks" / f"collaboration-create-{project_id}.lock"
    service = provisioner or build_github_repository_provisioner(client_id=client_id)
    repository = GitHubRepository(owner, repository_name)
    askpass = resolve_github_askpass(askpass_path)
    environment = github_git_environment(client_id=client_id, askpass=askpass)
    identity = _identity(
        project_id=project_id, owner=repository.owner, repository_name=repository.name,
        private=private, code=code, docs=docs, client_id=client_id,
        coordinator_integration_id=coordinator_integration_id,
    )
    with exclusive_lock(lock, timeout_seconds=120):
        if transaction.exists():
            journal = _journal(
                _load(transaction), identity=identity,
                expected_plan_sha256=expected_plan_sha256,
            )
            recovered = True
        else:
            plan = create_collaborative_project_plan(
                project_id=project_id, owner=owner, repository_name=repository_name,
                code_root=code, docs_root=docs, private=private, client_id=client_id,
                coordinator_integration_id=coordinator_integration_id,
                provisioner=service,
            )
            if not hmac.compare_digest(str(plan["plan_sha256"]), expected_plan_sha256):
                raise WorkflowError("project create plan is stale")
            if plan["blockers"]:
                raise WorkflowError(f"project create blocked: {plan['blockers']}")
            journal = {
                "schema_version": CREATE_SCHEMA_VERSION,
                "plan_sha256": expected_plan_sha256,
                "phase": "prepared",
                **identity,
                "repository": None,
            }
            _save(transaction, journal)
            recovered = False
        if journal["phase"] == "prepared":
            if service.exists(repository):
                created = service.read_existing(repository)
                if created.private is not private or service.read_branch_heads(repository):
                    raise WorkflowError(
                        "existing repository cannot be reconciled with interrupted creation"
                    )
            else:
                created = service.create(
                    repository=repository, private=private,
                    description=f"ARIA collaborative project {project_id}",
                )
            journal["repository"] = asdict(created)
            journal["phase"] = "repository_created"
            _save(transaction, journal)
        created = _receipt(journal.get("repository"), repository=repository, private=private)
        if journal["phase"] == "repository_created":
            actor = service.actor()
            initial_commit = _ensure_initial_commit(
                code=code, created=created, actor_name=actor.username_snapshot,
                actor_email=f"{actor.user_id}+{actor.username_snapshot}@users.noreply.github.com",
                repository_name=repository_name,
            )
            journal["phase"] = "local_committed"
            _save(transaction, journal)
        else:
            initial_commit = _read_initial_commit(
                code=code, repository=repository, repository_name=repository_name
            )
        if journal["phase"] == "local_committed":
            _run_git(
                "push", "origin", "main:main", "dev:dev",
                cwd=code, environment=environment,
            )
            _run_git(
                "branch", "--set-upstream-to=origin/dev", "dev", cwd=code,
            )
            remote_main = _run_git(
                "rev-parse", "refs/remotes/origin/main", cwd=code
            ).stdout.strip()
            remote_dev = _run_git(
                "rev-parse", "refs/remotes/origin/dev", cwd=code
            ).stdout.strip()
            if remote_main != initial_commit or remote_dev != initial_commit:
                raise WorkflowError("project create main/dev push read-back mismatch")
            journal["phase"] = "dev_pushed"
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
            code_root=code, remote="origin", repository_id=created.repository_id,
            app_id=coordinator_integration_id,
        )
        collaboration = collaboration_plan(
            project_id=project_id, code_root=code, docs_root=docs, provider="github",
            repository_id=created.repository_id, provider_adapter=adapter,
        )
        enabled = enable_collaboration(
            project_id=project_id, code_root=code, docs_root=docs, provider="github",
            repository_id=created.repository_id,
            expected_plan_sha256=str(collaboration["plan_sha256"]), confirm=True,
            provider_adapter=adapter, control_writer=writer, runtime_root=runtime,
            git_environment=environment,
            coordinator_integration_id=coordinator_integration_id,
        )
        project = load_project(project_id, runtime_root=runtime)
        doctor = run_collaborative_project_doctor(project)
        if doctor.get("ok") is not True:
            failed = [
                row["id"]
                for row in doctor.get("checks", [])
                if row.get("blocking") and not row.get("ok")
            ]
            raise WorkflowError(f"created project doctor failed: {failed}")
        transaction.unlink()
        return {
            **enabled,
            "repository": f"{repository.owner}/{repository.name}",
            "repository_id": created.repository_id,
            "private": private,
            "initial_commit": initial_commit,
            "recovered": recovered or bool(enabled.get("recovered")),
            "doctor_ok": True,
        }
