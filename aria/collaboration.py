from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from aria.errors import (
    ConfigurationError,
    ProviderAdapterError,
    ProviderCapabilityError,
    WorkflowError,
)
from aria.project import DEFAULT_GIT_TIMEOUT_SECONDS, PROJECT_ID_RE
from aria.project import default_runtime_root
from aria.github_control import CONTROL_DOCUMENTS
from aria.registry import read_registry, registry_path
from aria.provider import (
    ProviderAdapter,
    ProviderInspection,
    validate_provider_inspection,
)

if TYPE_CHECKING:
    from aria.collaboration_apply import ControlWriter


CONTROL_SCHEMA_VERSION = 1
CONTROL_KIND = "aria-collaboration-control"
PROVIDER_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DOCUMENT_SCHEMA_VERSIONS = {
    "project": 1,
    "backlog": 2,
    "activity": 1,
    "state": 2,
    "team": 2,
    "access": 2,
}


@dataclass(frozen=True)
class ControlContract:
    project_id: str
    provider: str
    repository_id: str
    remote: str
    integration_branch: str
    control_branch: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "kind": CONTROL_KIND,
            "project_id": self.project_id,
            "mode": "collaborative",
            "provider": {
                "kind": self.provider,
                "repository_id": self.repository_id,
            },
            "git": {
                "remote": self.remote,
                "integration_branch": self.integration_branch,
                "control_branch": self.control_branch,
            },
            "authority": {
                "identity": "provider",
                "membership": "provider",
                "canonical_writer": "coordinator",
                "direct_control_push": False,
            },
            "documents": dict(DOCUMENT_SCHEMA_VERSIONS),
            "migration": {
                "from_mode": "offline",
                "explicit": True,
            },
        }


def _exact_keys(
    mapping: dict[str, object], expected: set[str], label: str
) -> None:
    actual = set(mapping)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ConfigurationError(
            f"{label} keys mismatch: missing={missing}, unexpected={unexpected}"
        )


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        raise ConfigurationError(f"{label} must be a string-keyed mapping")
    return value


def _required_string(mapping: dict[str, object], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ConfigurationError(f"{label}.{key} must be a non-empty trimmed string")
    return value


def _validate_branch(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} must be a string")
    parts = value.split("/")
    invalid = (
        value in {"@", "HEAD"}
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(character in value for character in " ~^:?*[\\")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(
            not part
            or part.startswith(".")
            or part.endswith((".", ".lock"))
            for part in parts
        )
    )
    if invalid:
        raise ConfigurationError(f"{label} is not a safe Git branch name: {value!r}")


def build_control_contract(
    *,
    project_id: str,
    provider: str,
    repository_id: str,
    remote: str = "origin",
    integration_branch: str = "dev",
    control_branch: str = "aria-control",
) -> ControlContract:
    if (
        not isinstance(project_id, str)
        or PROJECT_ID_RE.fullmatch(project_id) is None
    ):
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    if not isinstance(provider, str) or PROVIDER_RE.fullmatch(provider) is None:
        raise ConfigurationError(f"Invalid provider adapter id: {provider!r}")
    if (
        not isinstance(repository_id, str)
        or not repository_id
        or repository_id != repository_id.strip()
        or len(repository_id) > 256
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in repository_id
        )
    ):
        raise ConfigurationError("repository_id must be a non-empty trimmed string")
    if not isinstance(remote, str) or REMOTE_RE.fullmatch(remote) is None:
        raise ConfigurationError("remote must be a non-empty Git remote name")
    _validate_branch(integration_branch, "integration_branch")
    _validate_branch(control_branch, "control_branch")
    if integration_branch == control_branch:
        raise ConfigurationError(
            "integration_branch and control_branch must be different"
        )
    return ControlContract(
        project_id=project_id,
        provider=provider,
        repository_id=repository_id,
        remote=remote,
        integration_branch=integration_branch,
        control_branch=control_branch,
    )


def parse_control_contract(value: object) -> ControlContract:
    root = _mapping(value, "CONTROL.yaml")
    _exact_keys(
        root,
        {
            "schema_version",
            "kind",
            "project_id",
            "mode",
            "provider",
            "git",
            "authority",
            "documents",
            "migration",
        },
        "CONTROL.yaml",
    )
    if (
        type(root.get("schema_version")) is not int
        or root.get("schema_version") != CONTROL_SCHEMA_VERSION
    ):
        raise ConfigurationError("CONTROL.yaml.schema_version must be 1")
    if root.get("kind") != CONTROL_KIND:
        raise ConfigurationError(f"CONTROL.yaml.kind must be {CONTROL_KIND!r}")
    if root.get("mode") != "collaborative":
        raise ConfigurationError(
            "CONTROL.yaml.mode must be 'collaborative'; offline/local projects "
            "must not contain this contract"
        )

    provider = _mapping(root.get("provider"), "CONTROL.yaml.provider")
    _exact_keys(provider, {"kind", "repository_id"}, "CONTROL.yaml.provider")
    git = _mapping(root.get("git"), "CONTROL.yaml.git")
    _exact_keys(
        git,
        {"remote", "integration_branch", "control_branch"},
        "CONTROL.yaml.git",
    )
    authority = _mapping(root.get("authority"), "CONTROL.yaml.authority")
    _exact_keys(
        authority,
        {
            "identity",
            "membership",
            "canonical_writer",
            "direct_control_push",
        },
        "CONTROL.yaml.authority",
    )
    expected_authority = {
        "identity": "provider",
        "membership": "provider",
        "canonical_writer": "coordinator",
        "direct_control_push": False,
    }
    if (
        authority != expected_authority
        or type(authority.get("direct_control_push")) is not bool
    ):
        raise ConfigurationError(
            "CONTROL.yaml.authority violates collaborative fail-closed policy"
        )
    documents = _mapping(root.get("documents"), "CONTROL.yaml.documents")
    if documents != DOCUMENT_SCHEMA_VERSIONS or any(
        type(value) is not int for value in documents.values()
    ):
        raise ConfigurationError(
            "CONTROL.yaml.documents does not match the collaborative schema set"
        )
    migration = _mapping(root.get("migration"), "CONTROL.yaml.migration")
    if (
        migration != {"from_mode": "offline", "explicit": True}
        or type(migration.get("explicit")) is not bool
    ):
        raise ConfigurationError(
            "CONTROL.yaml.migration must require an explicit offline migration"
        )

    return build_control_contract(
        project_id=_required_string(root, "project_id", "CONTROL.yaml"),
        provider=_required_string(provider, "kind", "CONTROL.yaml.provider"),
        repository_id=_required_string(
            provider, "repository_id", "CONTROL.yaml.provider"
        ),
        remote=_required_string(git, "remote", "CONTROL.yaml.git"),
        integration_branch=_required_string(
            git, "integration_branch", "CONTROL.yaml.git"
        ),
        control_branch=_required_string(git, "control_branch", "CONTROL.yaml.git"),
    )


def dump_control_contract(contract: ControlContract) -> str:
    content = yaml.safe_dump(
        contract.as_mapping(), allow_unicode=True, sort_keys=False
    )
    parsed = yaml.safe_load(content)
    if parse_control_contract(parsed) != contract:
        raise ConfigurationError("CONTROL.yaml deterministic round-trip failed")
    return content


def load_control_contract(path: Path) -> ControlContract:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot read CONTROL.yaml: {path}: {error}") from error
    return parse_control_contract(raw)


def _git(code_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(code_root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ConfigurationError(f"Cannot inspect Git checkout: {error}") from error


def _git_value(code_root: Path, *args: str, label: str) -> str:
    result = _git(code_root, *args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Git command failed"
        raise ConfigurationError(f"Cannot read {label}: {detail}")
    return result.stdout.strip()


def _ref_exists(code_root: Path, reference: str) -> bool:
    result = _git(code_root, "show-ref", "--verify", "--quiet", reference)
    if result.returncode not in {0, 1}:
        raise ConfigurationError(
            f"Cannot inspect Git ref {reference!r}: {result.stderr.strip()}"
        )
    return result.returncode == 0


def _worktrees(code_root: Path) -> list[dict[str, str]]:
    result = _git(code_root, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        raise ConfigurationError(
            f"Cannot inspect Git worktrees: {result.stderr.strip()}"
        )
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for line in [*result.stdout.splitlines(), ""]:
        if not line:
            if record:
                records.append(record)
                record = {}
            continue
        key, _, value = line.partition(" ")
        record[key] = value
    return records


def _blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def collaboration_plan(
    *,
    project_id: str,
    code_root: Path,
    provider: str,
    repository_id: str,
    docs_root: Path | None = None,
    remote: str = "origin",
    integration_branch: str = "dev",
    control_branch: str = "aria-control",
    provider_adapter: ProviderAdapter | None = None,
) -> dict[str, object]:
    contract = build_control_contract(
        project_id=project_id,
        provider=provider,
        repository_id=repository_id,
        remote=remote,
        integration_branch=integration_branch,
        control_branch=control_branch,
    )
    try:
        requested_code_root = code_root.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(f"code_root is unreadable: {code_root}") from error
    if not requested_code_root.is_dir():
        raise ConfigurationError(f"code_root is not a directory: {code_root}")
    git_root = Path(
        _git_value(
            requested_code_root,
            "rev-parse",
            "--show-toplevel",
            label="Git top-level",
        )
    ).resolve(strict=True)
    if git_root != requested_code_root:
        raise ConfigurationError(
            f"code_root must be the Git top-level: requested={requested_code_root}, "
            f"actual={git_root}"
        )

    target_docs_root = (
        docs_root.resolve(strict=False)
        if docs_root is not None
        else git_root.with_name(f"{git_root.name}-aria-control")
    )
    blockers: list[dict[str, str]] = []
    provider_inspection: ProviderInspection | None = None
    provider_blocker_code: str | None = None
    if provider_adapter is None:
        provider_blocker_code = "PROVIDER_ADAPTER_UNAVAILABLE"
        blockers.append(
            _blocker(
                "PROVIDER_ADAPTER_UNAVAILABLE",
                "Provider identity, membership, and immutable repository identity "
                "cannot be verified yet",
            )
        )
    else:
        if provider_adapter.provider_id != provider:
            raise ConfigurationError(
                "Provider adapter does not match CONTROL.yaml provider"
            )
        try:
            provider_inspection = provider_adapter.inspect_collaboration(
                repository_id=repository_id,
                control_branch=control_branch,
            )
        except ProviderAdapterError as error:
            provider_blocker_code = (
                "PROVIDER_CAPABILITY_UNAVAILABLE"
                if isinstance(error, ProviderCapabilityError)
                else "PROVIDER_ADAPTER_UNAVAILABLE"
            )
            blockers.append(
                _blocker(provider_blocker_code, str(error))
            )
        if provider_inspection is not None:
            validate_provider_inspection(
                provider_inspection,
                expected_provider=provider,
                expected_repository_id=repository_id,
            )
            if not provider_inspection.membership.active:
                blockers.append(
                    _blocker(
                        "MEMBERSHIP_DENIED",
                        "Authenticated provider user is not an active project member",
                    )
                )
    remote_result = _git(git_root, "remote", "get-url", remote)
    remote_configured = remote_result.returncode == 0
    if not remote_configured:
        blockers.append(
            _blocker("REMOTE_NOT_FOUND", f"Git remote {remote!r} is not configured")
        )

    head = _git_value(git_root, "rev-parse", "--verify", "HEAD", label="Git HEAD")
    branch_result = _git(git_root, "branch", "--show-current")
    if branch_result.returncode != 0:
        raise ConfigurationError(
            f"Cannot read current Git branch: {branch_result.stderr.strip()}"
        )
    current_branch = branch_result.stdout.strip() or None
    status = _git_value(
        git_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        label="Git status",
    )
    clean = not bool(status)
    if not clean:
        blockers.append(
            _blocker(
                "CODE_WORKTREE_DIRTY",
                "code_root has uncommitted or untracked changes",
            )
        )

    local_integration = _ref_exists(
        git_root, f"refs/heads/{integration_branch}"
    )
    remote_integration = _ref_exists(
        git_root, f"refs/remotes/{remote}/{integration_branch}"
    )
    if not (local_integration or remote_integration):
        blockers.append(
            _blocker(
                "INTEGRATION_BRANCH_NOT_FOUND",
                f"Integration branch {integration_branch!r} is not available locally",
            )
        )

    local_control = _ref_exists(git_root, f"refs/heads/{control_branch}")
    remote_control = _ref_exists(
        git_root, f"refs/remotes/{remote}/{control_branch}"
    )
    worktrees = _worktrees(git_root)
    control_ref = f"refs/heads/{control_branch}"
    control_worktrees = [
        record for record in worktrees if record.get("branch") == control_ref
    ]
    target_text = str(target_docs_root)
    matching_worktree = next(
        (
            record
            for record in control_worktrees
            if Path(record.get("worktree", "")).resolve(strict=False)
            == target_docs_root
        ),
        None,
    )

    if target_docs_root == git_root or target_docs_root.is_relative_to(git_root):
        blockers.append(
            _blocker(
                "DOCS_ROOT_OVERLAP",
                "docs_root must be physically separate from code_root",
            )
        )
    if control_worktrees and matching_worktree is None:
        blockers.append(
            _blocker(
                "CONTROL_WORKTREE_CONFLICT",
                "control branch is already attached to a different worktree",
            )
        )
    if target_docs_root.exists() and matching_worktree is None:
        try:
            occupied = not target_docs_root.is_dir() or any(target_docs_root.iterdir())
        except OSError as error:
            raise ConfigurationError(
                f"Cannot inspect docs_root: {target_docs_root}: {error}"
            ) from error
        if occupied:
            blockers.append(
                _blocker(
                    "DOCS_ROOT_OCCUPIED",
                    "docs_root exists and is not the expected control worktree",
                )
            )

    protection_configurable = (
        provider_inspection is not None
        and provider_inspection.membership.active
        and "admin" in provider_inspection.membership.roles
        and callable(getattr(provider_adapter, "ensure_control_protection", None))
    )
    if (
        provider_inspection is None
        or (
            not provider_inspection.protection.coordinator_only
            and not protection_configurable
        )
    ):
        blockers.append(
            _blocker(
                "UNPROTECTED",
                "Branch protection is not verified; a provider adapter must confirm "
                "that only ARIA Coordinator can write aria-control",
            )
        )

    actions = [
        {
            "action": "verify_provider_identity_and_membership",
            "status": "verified"
            if provider_inspection is not None
            and provider_inspection.membership.active
            else "blocked",
            "reason": None
            if provider_inspection is not None
            and provider_inspection.membership.active
            else provider_blocker_code or "MEMBERSHIP_DENIED",
        },
        {
            "action": "create_or_reuse_control_branch",
            "status": "reuse" if local_control or remote_control else "planned",
        },
        {
            "action": "create_or_reuse_control_worktree",
            "status": "reuse" if matching_worktree is not None else "planned",
            "path": target_text,
        },
        {
            "action": "write_control_documents_and_register_project",
            "status": "planned",
        },
        {
            "action": "verify_control_branch_protection",
            "status": (
                "verified"
                if provider_inspection is not None
                and provider_inspection.protection.coordinator_only
                else "planned"
                if protection_configurable
                else "blocked"
            ),
            "reason": (
                None
                if provider_inspection is not None
                and provider_inspection.protection.coordinator_only
                else "CONFIGURE_COORDINATOR_RULESET"
                if protection_configurable
                else "UNPROTECTED"
            ),
        },
    ]
    plan_body: dict[str, object] = {
        "operation": "collaboration.enable",
        "project_id": project_id,
        "code_root": str(git_root),
        "docs_root": target_text,
        "contract": contract.as_mapping(),
        "provider_readback": provider_inspection.as_mapping()
        if provider_inspection is not None
        else None,
        "git": {
            "head": head,
            "current_branch": current_branch,
            "clean": clean,
            "remote": remote,
            "remote_configured": remote_configured,
            "integration_branch": {
                "name": integration_branch,
                "local": local_integration,
                "remote_tracking": remote_integration,
            },
            "control_branch": {
                "name": control_branch,
                "local": local_control,
                "remote_tracking": remote_control,
                "worktree": matching_worktree.get("worktree")
                if matching_worktree
                else None,
            },
        },
        "actions": actions,
        "blockers": blockers,
    }
    canonical = json.dumps(
        plan_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "ok": True,
        "ready": not blockers,
        "read_only": True,
        "plan_sha256": hashlib.sha256(canonical).hexdigest(),
        **plan_body,
    }


def enable_collaboration(
    *,
    project_id: str,
    code_root: Path,
    provider: str,
    repository_id: str,
    expected_plan_sha256: str,
    confirm: bool,
    docs_root: Path | None = None,
    remote: str = "origin",
    integration_branch: str = "dev",
    control_branch: str = "aria-control",
    provider_adapter: ProviderAdapter | None = None,
    control_writer: ControlWriter | None = None,
    runtime_root: Path | None = None,
    git_environment: dict[str, str] | None = None,
    coordinator_integration_id: int | None = None,
) -> dict[str, object]:
    from aria.collaboration_apply import (
        apply_collaboration_transaction,
        collaboration_apply_paths,
    )
    from aria.collaborative_documents import build_initial_collaborative_documents

    if not confirm:
        raise WorkflowError("Collaboration enable requires explicit confirmation")
    if re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256) is None:
        raise ConfigurationError("expected_plan_sha256 must be a lowercase SHA-256")
    runtime = runtime_root or default_runtime_root()
    pending = collaboration_apply_paths(
        runtime_root=runtime, project_id=project_id
    ).transaction.exists()
    current = collaboration_plan(
        project_id=project_id,
        code_root=code_root,
        provider=provider,
        repository_id=repository_id,
        docs_root=docs_root,
        remote=remote,
        integration_branch=integration_branch,
        control_branch=control_branch,
        provider_adapter=provider_adapter,
    )
    current_digest = str(current["plan_sha256"])
    if not pending and not hmac.compare_digest(current_digest, expected_plan_sha256):
        raise WorkflowError(
            "Collaboration plan is stale or does not match these arguments: "
            f"expected={expected_plan_sha256}, current={current_digest}"
        )
    blockers = current.get("blockers")
    if not isinstance(blockers, list):
        raise WorkflowError("Collaboration plan returned invalid blockers")
    if blockers:
        summary = "; ".join(
            f"{item.get('code', 'UNKNOWN')}: {item.get('message', '')}"
            for item in blockers
            if isinstance(item, dict)
        )
        raise WorkflowError(f"Collaboration enable blocked: {summary}")
    if control_writer is None:
        raise WorkflowError("Collaboration enable requires an ARIA Coordinator writer")
    inspection_mapping = current.get("provider_readback")
    protection_mapping = (
        inspection_mapping.get("protection")
        if isinstance(inspection_mapping, dict)
        else None
    )
    if not (
        isinstance(protection_mapping, dict)
        and protection_mapping.get("coordinator_only") is True
    ):
        ensure = getattr(provider_adapter, "ensure_control_protection", None)
        if not callable(ensure):
            raise WorkflowError("Provider cannot configure aria-control protection")
        ensure(repository_id=repository_id, control_branch=control_branch)
        verified = provider_adapter.inspect_collaboration(
            repository_id=repository_id,
            control_branch=control_branch,
        )
        validate_provider_inspection(
            verified,
            expected_provider=provider,
            expected_repository_id=repository_id,
        )
        if not verified.protection.coordinator_only:
            raise WorkflowError("aria-control protection read-back failed after creation")
    contract = build_control_contract(
        project_id=project_id,
        provider=provider,
        repository_id=repository_id,
        remote=remote,
        integration_branch=integration_branch,
        control_branch=control_branch,
    )
    target_docs = Path(str(current["docs_root"]))
    control_git = current.get("git", {}).get("control_branch")
    if not pending and isinstance(control_git, dict) and control_git.get("worktree"):
        existing_contract = load_control_contract(target_docs / "CONTROL.yaml")
        if existing_contract != contract:
            raise WorkflowError("existing control worktree belongs to another contract")
        actual_documents = {
            path.name for path in target_docs.iterdir() if path.name != ".git"
        }
        if actual_documents != CONTROL_DOCUMENTS or any(
            not (target_docs / name).is_file() for name in CONTROL_DOCUMENTS
        ):
            raise WorkflowError("existing control worktree document inventory is invalid")
        local_head = _git_value(
            code_root,
            "rev-parse",
            f"refs/heads/{control_branch}",
            label="local control head",
        )
        remote_head = control_writer.read_head()
        if remote_head != local_head:
            raise WorkflowError("existing control worktree is not at coordinator remote head")
        registry = read_registry(registry_path(runtime))
        entry = registry.get("projects", {}).get(project_id)
        if not isinstance(entry, dict):
            raise WorkflowError("existing collaborative project is not registered")
        if (
            Path(str(entry.get("docs_root", ""))).resolve(strict=False)
            != target_docs.resolve(strict=True)
            or Path(str(entry.get("code_root", ""))).resolve(strict=False)
            != code_root.resolve(strict=True)
        ):
            raise WorkflowError("existing collaborative project registry roots mismatch")
        return {
            "ok": True,
            "project": project_id,
            "mode": entry.get("mode", "shadow"),
            "collaboration": "enabled",
            "repository_id": repository_id,
            "control_branch": control_branch,
            "control_commit": local_head,
            "already_enabled": True,
            "recovered": False,
        }
    documents = build_initial_collaborative_documents(
        contract,
        display_name=code_root.resolve(strict=True).name,
        coordinator_integration_id=coordinator_integration_id,
    )
    return apply_collaboration_transaction(
        project_id=project_id,
        repository_id=repository_id,
        plan_sha256=expected_plan_sha256,
        code_root=code_root,
        docs_root=target_docs,
        remote=remote,
        control_branch=control_branch,
        document_set=documents,
        writer=control_writer,
        runtime_root=runtime,
        git_environment=git_environment,
    )
