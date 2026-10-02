from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import yaml

from aria import __version__
from aria.errors import ConfigurationError
from aria.io import atomic_write_bytes
from aria.access import access_template
from aria.backlog import backlog_template
from aria.migration_1_5 import team_template_1_5, trust_template_1_5
from aria.project import PROJECT_ID_RE, canonical_sha, git_snapshot
from aria.registry import read_registry, register_project, registry_path

MANIFEST_NAMES = {
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "Gemfile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
}
IGNORED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "vendor",
    "__pycache__",
}
SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".rb",
    ".php",
    ".swift",
}


def _git(code_root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(code_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as error:
        raise ConfigurationError(f"Git inspection failed for {code_root}: {error}") from error
    return result.stdout.strip()


def _slug(value: str, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:48] or fallback


def _tracked_files(code_root: Path) -> list[str]:
    raw = _git(code_root, "ls-files", "-z")
    return [item.replace("\\", "/") for item in raw.split("\0") if item]


def _detect_inventory(code_root: Path) -> tuple[list[str], list[dict[str, object]]]:
    tracked = _tracked_files(code_root)
    relevant = [
        path
        for path in tracked
        if not set(PurePosixPath(path).parts).intersection(IGNORED_PARTS)
    ]
    manifests = [path for path in relevant if PurePosixPath(path).name in MANIFEST_NAMES]
    if not manifests:
        manifests = [path for path in relevant if PurePosixPath(path).suffix in SOURCE_SUFFIXES][:1]
    if not manifests:
        raise ConfigurationError("aria init needs at least one tracked manifest or source file")
    source_files = [path for path in relevant if PurePosixPath(path).suffix in SOURCE_SUFFIXES]
    component_keys: list[str] = []
    for path in source_files or relevant:
        parts = PurePosixPath(path).parts
        key = parts[0] if len(parts) > 1 else "."
        if key not in component_keys:
            component_keys.append(key)
    component_keys = component_keys[:24] or ["."]
    components: list[dict[str, object]] = []
    used_ids: set[str] = set()
    for index, key in enumerate(component_keys, start=1):
        base_identifier = _slug(key, f"component-{index}")
        identifier = base_identifier
        suffix = 2
        while identifier in used_ids:
            identifier = f"{base_identifier[:44]}-{suffix}"
            suffix += 1
        used_ids.add(identifier)
        paths = ["**"] if key == "." else [f"{key}/**"]
        samples = [path for path in source_files if key == "." or path.startswith(key + "/")][:5]
        components.append(
            {
                "id": identifier,
                "name": key if key != "." else code_root.name,
                "paths": paths,
                "layer": "observed-source",
                "domain": _slug(code_root.name, "project"),
                "responsibilities": [
                    "Owns tracked source under " + (key if key != "." else "repository root")
                ],
                "depends_on": [],
                "risks": ["Behavior and dependencies require task-specific code inspection"],
                "test_seams": [
                    "Detected source samples: " + (", ".join(samples) if samples else "none")
                ],
            }
        )
    return manifests, components


def _verification_commands(code_root: Path, manifests: list[str]) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = []
    for relative in manifests:
        path = code_root / relative
        name = PurePosixPath(relative).name
        parent = PurePosixPath(relative).parent.as_posix()
        cwd = "." if parent == "." else parent
        identifier_suffix = _slug(cwd, "root")
        if name == "pyproject.toml":
            try:
                with path.open("rb") as stream:
                    pyproject = tomllib.load(stream)
            except (OSError, tomllib.TOMLDecodeError):
                continue
            tool = pyproject.get("tool", {})
            project = pyproject.get("project", {})
            dependency_text = json.dumps(
                {
                    "dependencies": project.get("dependencies", []),
                    "optional": project.get("optional-dependencies", {}),
                }
            ).lower()
            if (isinstance(tool, dict) and "pytest" in tool) or (
                "pytest" in dependency_text
            ):
                commands.append(
                    {
                        "id": f"python-tests-{identifier_suffix}",
                        "adapter": "python",
                        "argv": ["python", "-m", "pytest"],
                        "cwd": cwd,
                        "classes": ["focused"],
                        "timeout_seconds": 900,
                    }
                )
        elif name == "package.json":
            try:
                package = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            scripts = package.get("scripts") if isinstance(package, dict) else None
            test_script = scripts.get("test") if isinstance(scripts, dict) else None
            if (
                isinstance(test_script, str)
                and test_script.strip()
                and "no test specified" not in test_script.lower()
            ):
                commands.append(
                    {
                        "id": f"node-tests-{identifier_suffix}",
                        "adapter": "node",
                        "argv": ["npm", "test"],
                        "cwd": cwd,
                        "classes": ["focused"],
                        "timeout_seconds": 900,
                    }
                )
        elif name == "Cargo.toml":
            commands.append(
                {
                    "id": f"rust-tests-{identifier_suffix}",
                    "adapter": "rust",
                    "argv": ["cargo", "test"],
                    "cwd": cwd,
                    "classes": ["focused"],
                    "timeout_seconds": 900,
                }
            )
        elif name == "go.mod":
            commands.append(
                {
                    "id": f"go-tests-{identifier_suffix}",
                    "adapter": "go",
                    "argv": ["go", "test", "./..."],
                    "cwd": cwd,
                    "classes": ["focused"],
                    "timeout_seconds": 900,
                }
            )
    used: set[str] = set()
    for index, row in enumerate(commands, start=1):
        base = str(row["id"])
        identifier = base
        suffix = 2
        while identifier in used:
            identifier = f"{base[:58]}-{suffix}"
            suffix += 1
        row["id"] = identifier
        used.add(identifier)
    return commands


def _stack_markdown(manifests: list[str], commands: list[dict[str, object]]) -> str:
    sections = [
        "# Working stack",
        "",
        "Deterministic bootstrap generated from tracked Git inventory.",
        "Codex must inspect the real code and replace inferred details before treating this document as a semantic architecture description.",
        "",
        "## Observed manifests",
        "",
        *[f"- `{path}`" for path in manifests],
        "",
        "## Canonical verification candidates",
        "",
    ]
    if commands:
        command_lines = [
            subprocess.list2cmdline([str(value) for value in row["argv"]])
            for row in commands
        ]
        sections.extend(["```powershell", *command_lines, "```"])
    else:
        sections.append("No standard test command was inferred; inspect the observed manifest before the first run.")
    return "\n".join(sections).rstrip() + "\n"


def initialize_project(
    project_id: str,
    *,
    code_root: Path,
    docs_root: Path | None,
    display_name: str | None = None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    if PROJECT_ID_RE.fullmatch(project_id) is None:
        raise ConfigurationError(f"Invalid project id: {project_id!r}")
    code = code_root.resolve(strict=True)
    top = Path(_git(code, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != code:
        raise ConfigurationError(f"--code-root must be the Git top-level: {top}")
    docs = (
        docs_root.absolute()
        if docs_root is not None
        else code.parent / f"{code.name}-aria"
    )
    if docs.exists():
        raise ConfigurationError(f"aria init refuses to overwrite an existing docs root: {docs}")
    if docs == code or docs.is_relative_to(code) or code.is_relative_to(docs):
        raise ConfigurationError("Project docs and code roots must be separate")
    registry = read_registry(registry_path(runtime_root))
    existing = registry.get("projects", {}).get(project_id)
    if isinstance(existing, dict) and existing.get("mode") == "active":
        raise ConfigurationError(f"Refusing to overwrite active registration: {project_id}")
    manifests, components = _detect_inventory(code)
    verification_commands = _verification_commands(code, manifests)
    git = git_snapshot(code)
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    event: dict[str, object] = {
        "schema_version": 1,
        "sequence": 1,
        "timestamp": timestamp,
        "type": "project_created",
        "project_id": project_id,
        "task_id": None,
        "git_head": git["head"],
        "previous_event_sha256": None,
        "refs": [],
        "result": {
            "summary": "ARIA deterministic bootstrap from observed Git inventory",
            "manifest_count": len(manifests),
            "component_count": len(components),
            "semantic_review_required": True,
        },
    }
    event["event_sha256"] = canonical_sha(event)
    project_doc = {
        "schema_version": 1,
        "project_id": project_id,
        "display_name": display_name or code.name,
        "framework_version": __version__,
        "state_profile": "frontier",
        "documents": {
            "state": "STATE.yaml",
            "stack": "STACK.md",
            "history": "HISTORY.jsonl",
            "system_map": "SYSTEM_MAP.yaml",
            "specs": "specs",
            "adr": "adr",
            "knowledge": "knowledge",
            "team": "ARIA_TEAM.yaml",
            "trust": "TRUST.yaml",
            "access": "ACCESS.yaml",
            "backlog": "BACKLOG.yaml",
            **({"verification": "VERIFY.yaml"} if verification_commands else {}),
        },
        "governance": {
            "status_authority": "BACKLOG.yaml",
            "require_active_run_for_writes": True,
            **(
                {"decision_registry": "docs/decisions/DECISIONS.md"}
                if (code / "docs" / "decisions" / "DECISIONS.md").is_file()
                else {}
            ),
        },
        "context": {
            "default_budget_bytes": 131072,
            "state_budget_bytes": 32768,
            "state_projection_budget_bytes": 32768,
            "stack_manifests": manifests,
        },
    }
    state = {
        "schema_version": 1,
        "project_id": project_id,
        "frontier": {
            "task_id": None,
            "intent": None,
            "mode": None,
            "stage": None,
            "next_action": None,
            "spec": None,
        },
        "blockers": [],
        "last_completed": None,
        "history_checkpoint": {
            "sequence": 1,
            "event_sha256": event["event_sha256"],
        },
    }
    map_payload = {
        "schema_version": 1,
        "project_id": project_id,
        "generated_from": {
            "git_head": git["head"],
            "working_tree_sha256": canonical_sha(git["changes"]["paths"]),
        },
        "dimensions": {
            "layers": sorted({str(row["layer"]) for row in components}),
            "domains": sorted({str(row["domain"]) for row in components}),
            "runtime_surfaces": ["git-tracked-source"],
            "cross_cutting": ["correctness", "testing", "maintainability"],
        },
        "components": components,
        "shared_primitives": [],
        "critical_flows": [
            {
                "id": "repository-change-flow",
                "steps": [str(row["id"]) for row in components],
                "failure_modes": ["Unmapped dependency or unverified behavior"],
                "assurance": ["Task-specific focused tests and integration read-back"],
            }
        ],
        "unknowns": [
            "Dependencies and runtime interactions are inferred only after task-specific code inspection"
        ],
    }
    init_token = uuid.uuid4().hex
    registered_completed = False
    try:
        for relative in ("specs", "adr", "knowledge"):
            (docs / relative).mkdir(parents=True, exist_ok=False)
        atomic_write_bytes(docs / ".aria-init-token", init_token.encode("ascii"))
        atomic_write_bytes(
            docs / "PROJECT.yaml",
            yaml.safe_dump(project_doc, allow_unicode=True, sort_keys=False).encode("utf-8"),
        )
        atomic_write_bytes(
            docs / "STATE.yaml",
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False).encode("utf-8"),
        )
        atomic_write_bytes(
            docs / "STACK.md", _stack_markdown(manifests, verification_commands).encode("utf-8")
        )
        atomic_write_bytes(
            docs / "ARIA_TEAM.yaml",
            team_template_1_5(project_id),
        )
        atomic_write_bytes(
            docs / "TRUST.yaml",
            trust_template_1_5(),
        )
        atomic_write_bytes(
            docs / "ACCESS.yaml",
            access_template(project_id),
        )
        atomic_write_bytes(
            docs / "BACKLOG.yaml",
            backlog_template(project_id),
        )
        if verification_commands:
            atomic_write_bytes(
                docs / "VERIFY.yaml",
                yaml.safe_dump(
                    {"schema_version": 1, "commands": verification_commands},
                    allow_unicode=True,
                    sort_keys=False,
                ).encode("utf-8"),
            )
        atomic_write_bytes(
            docs / "SYSTEM_MAP.yaml",
            yaml.safe_dump(map_payload, allow_unicode=True, sort_keys=False).encode("utf-8"),
        )
        atomic_write_bytes(
            docs / "HISTORY.jsonl",
            (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        registered = register_project(
            project_id,
            docs_root=docs,
            code_root=code,
            runtime_root=runtime_root,
        )
        registered_completed = True
    except Exception:
        marker = docs / ".aria-init-token"
        if not registered_completed and marker.is_file():
            try:
                if marker.read_text(encoding="ascii") == init_token:
                    shutil.rmtree(docs)
            except OSError:
                pass
        raise
    marker = docs / ".aria-init-token"
    if marker.is_file():
        marker.unlink()
    return {
        **registered,
        "framework_version": __version__,
        "manifests": manifests,
        "components": [row["id"] for row in components],
        "verification_commands": [str(row["id"]) for row in verification_commands],
        "bootstrap_kind": "deterministic_git_inventory",
        "semantic_bootstrap": False,
        "semantic_review_required": True,
        "next_action": (
            "Codex: inspect the real code and review STACK.md and SYSTEM_MAP.yaml. "
            "Then run `aria identity enroll --actor <owner> --device <device> "
            "--output <request.json>`, "
            f"`aria --identity-actor <owner> --identity-device <device> access bootstrap "
            f"--project {project_id}`, `aria doctor --project {project_id}`, "
            f"`aria governance check --project {project_id}`, add/claim a backlog item, "
            f"then run `aria --identity-actor <owner> --identity-device <device> feature "
            f"--project {project_id} --task <task> --backlog-item <BLG-ID>` and the "
            "returned write preflight before product mutation."
        ),
    }
