from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from aria import __version__
from aria.activity import validate_activity_snapshot
from aria.collaboration import load_control_contract
from aria.collaborative_backlog import validate_collaborative_backlog
from aria.collaborative_documents import (
    validate_collaborative_access,
    validate_collaborative_state,
)
from aria.collaborative_team import validate_collaborative_team
from aria.errors import ConfigurationError, WorkflowError
from aria.github_control import CONTROL_DOCUMENTS

if TYPE_CHECKING:
    from aria.project import ProjectConfig


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ConfigurationError("Cannot inspect collaborative Git worktree") from error
    if result.returncode != 0:
        raise ConfigurationError(
            result.stderr.strip() or "Cannot inspect collaborative Git worktree"
        )
    return result.stdout.strip()


def _yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Cannot read collaborative document: {path.name}") from error


def run_collaborative_project_doctor(project: ProjectConfig) -> dict[str, object]:
    checks: list[dict[str, object]] = []

    def add(check_id: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        checks.append(
            {"id": check_id, "ok": ok, "blocking": blocking, "detail": detail}
        )

    add("registry", project.registry_path.is_file(), str(project.registry_path))
    add(
        "framework_version",
        project.framework_version == __version__,
        f"project={project.framework_version}; runtime={__version__}",
    )
    roots_exist = all(
        root.is_dir() for root in (project.framework_root, project.docs_root, project.code_root)
    )
    add("roots_present", roots_exist, "framework/docs/code")
    separated = False
    if roots_exist:
        resolved = [
            root.resolve(strict=True)
            for root in (project.framework_root, project.docs_root, project.code_root)
        ]
        separated = all(
            left != right
            and not left.is_relative_to(right)
            and not right.is_relative_to(left)
            for index, left in enumerate(resolved)
            for right in resolved[index + 1 :]
        )
    add("framework_docs_code_separation", separated, "three distinct roots")
    contract = None
    try:
        contract = load_control_contract(project.docs_root / "CONTROL.yaml")
        if contract.project_id != project.project_id:
            raise ConfigurationError("control project id mismatch")
        add("control_contract", True, contract.repository_id)
    except ConfigurationError as error:
        add("control_contract", False, str(error))
    try:
        project_document = _yaml(project.docs_root / "PROJECT.yaml")
        if not isinstance(project_document, dict) or contract is None:
            raise ConfigurationError("collaborative repository contract is unavailable")
        repository_contract = project_document.get("repository")
        expected_repository_contract = {
            "provider": contract.provider,
            "repository_id": contract.repository_id,
            "remote": contract.remote,
            "main_branch": "main",
            "integration_branch": "dev",
            "working_branch_template": "work/{github_username}",
            "control_branch": "aria-control",
        }
        if repository_contract is None:
            add(
                "repository_contract",
                False,
                "legacy PROJECT.yaml; explicit collaborative upgrade is required",
                blocking=False,
            )
        elif not isinstance(repository_contract, dict):
            raise ConfigurationError("PROJECT.yaml repository contract mismatch")
        else:
            required_checks = repository_contract.get("required_checks")
            base_contract = {
                key: value
                for key, value in repository_contract.items()
                if key != "required_checks"
            }
            checks_valid = (
                required_checks is None
                or (
                    isinstance(required_checks, list)
                    and len(required_checks) == 1
                    and isinstance(required_checks[0], dict)
                    and required_checks[0].get("context") == "ARIA integration"
                    and type(required_checks[0].get("app_id")) is int
                    and required_checks[0]["app_id"] > 0
                )
            )
            if base_contract != expected_repository_contract or not checks_valid:
                raise ConfigurationError("PROJECT.yaml repository contract mismatch")
            detail = "main/dev/work/<github-username>/aria-control"
            if required_checks is None:
                add(
                    "repository_contract",
                    False,
                    detail + "; explicit App-pinned check upgrade is required",
                    blocking=False,
                )
            else:
                add(
                    "repository_contract",
                    True,
                    detail + "; ARIA integration App-pinned",
                )
    except ConfigurationError as error:
        add("repository_contract", False, str(error))
    try:
        code_common = Path(_git(project.code_root, "rev-parse", "--git-common-dir"))
        docs_common = Path(_git(project.docs_root, "rev-parse", "--git-common-dir"))
        if not code_common.is_absolute():
            code_common = (project.code_root / code_common).resolve(strict=True)
        if not docs_common.is_absolute():
            docs_common = (project.docs_root / docs_common).resolve(strict=True)
        same_repository = code_common == docs_common
        add("shared_git_repository", same_repository, str(code_common))
        branch = _git(project.docs_root, "branch", "--show-current")
        add(
            "control_branch",
            contract is not None and branch == contract.control_branch,
            branch,
        )
        clean = not _git(project.docs_root, "status", "--porcelain=v1")
        add("control_worktree_clean", clean, str(project.docs_root))
    except ConfigurationError as error:
        add("shared_git_repository", False, str(error))
        add("control_branch", False, str(error))
        add("control_worktree_clean", False, str(error))
    inventory = {
        path.name for path in project.docs_root.iterdir() if path.name != ".git"
    }
    add(
        "control_document_inventory",
        inventory == CONTROL_DOCUMENTS
        and all((project.docs_root / name).is_file() for name in CONTROL_DOCUMENTS),
        json.dumps(sorted(inventory)),
    )
    try:
        if contract is None:
            raise ConfigurationError("control contract is unavailable")
        validate_collaborative_backlog(_yaml(project.docs_root / "BACKLOG.yaml"))
        add("collaborative_backlog", True, "schema=2")
        validate_activity_snapshot(_yaml(project.docs_root / "ACTIVITY.yaml"))
        add("collaborative_activity", True, "schema=1")
        validate_collaborative_team(_yaml(project.docs_root / "ARIA_TEAM.yaml"))
        add("collaborative_team", True, "schema=2")
        validate_collaborative_access(_yaml(project.docs_root / "ACCESS.yaml"), contract)
        add("collaborative_access", True, "schema=2")
        state = validate_collaborative_state(
            _yaml(project.docs_root / "STATE.yaml"), contract
        )
        add("collaborative_state", True, f"revision={state['revision']}")
        history = (project.docs_root / "HISTORY.jsonl").read_text(encoding="utf-8")
        from aria.collaborative_state import validate_collaborative_history

        validate_collaborative_history(history, state)
        add("collaborative_history", True, f"events={state['revision']}")
    except (ConfigurationError, WorkflowError, OSError, UnicodeDecodeError) as error:
        add("collaborative_documents", False, str(error))
    try:
        project.runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=project.runtime_root, delete=True) as probe:
            probe.write(b"aria")
            probe.flush()
        add("runtime_write_readback", True, str(project.runtime_root))
    except OSError as error:
        add("runtime_write_readback", False, str(error))
    return {
        "schema_version": 1,
        "ok": all(
            bool(check["ok"]) for check in checks if check.get("blocking", True)
        ),
        "project": project.project_id,
        "mode": project.mode,
        "collaboration_mode": "collaborative",
        "checks": checks,
    }
