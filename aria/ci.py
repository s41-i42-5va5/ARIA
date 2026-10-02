from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from aria import __version__
from aria.assurance import TEST_CLASSES
from aria.errors import ConfigurationError, WorkflowError
from aria.evidence_package import (
    ZIP_TIMESTAMP,
    _archive_entries,
    _read_package_bytes,
    build_signed_package,
    inspect_package,
    verify_package,
)
from aria.execution import _normalized_command, _run_command
from aria.io import atomic_write_bytes, atomic_write_json, exclusive_lock
from aria.project import canonical_sha, git_snapshot
from aria.signing import sign_bytes, verify_bytes
from aria.trust import evaluate_trust, load_trust_policy

MAX_JOB_TTL_SECONDS = 86400
HEX_64_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}")
JOB_ID_RE = re.compile(r"job-[0-9a-f]{32}")
ACTOR_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _execution_classes(commands: list[dict[str, object]]) -> set[str]:
    return {
        str(value)
        for command in commands
        for value in command.get("classes", [])
        if isinstance(value, str)
    }


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _read_json(path: Path, label: str) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"{label} is unreadable: {path}") from error
    if not isinstance(raw, dict):
        raise WorkflowError(f"{label} must contain a JSON object")
    return raw


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise WorkflowError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise WorkflowError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise WorkflowError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def authorize_ci_job(
    payload: dict[str, object], *, private_key_path: Path, actor_id: str
) -> dict[str, object]:
    if ACTOR_ID_RE.fullmatch(actor_id) is None:
        raise ConfigurationError(f"Invalid job authorizer actor id: {actor_id!r}")
    if "authorization" in payload:
        raise ConfigurationError("CI job payload is already authorized")
    authorization = sign_bytes(_canonical_json(payload), private_key_path)
    authorization["actor_id"] = actor_id
    authorization["signed_at"] = payload.get("created_at")
    return {**payload, "authorization": authorization}


def _verify_job_authorization(
    job: dict[str, object],
    *,
    trust_policy_path: Path,
    policy_name: str,
    now: datetime | None = None,
) -> str:
    authorization = job.get("authorization")
    if not isinstance(authorization, dict):
        raise WorkflowError("CI job has no signed authorization")
    payload = {key: value for key, value in job.items() if key != "authorization"}
    actor_id = authorization.get("actor_id")
    signed_at_value = authorization.get("signed_at")
    if (
        not isinstance(actor_id, str)
        or not isinstance(signed_at_value, str)
        or signed_at_value != payload.get("created_at")
    ):
        raise WorkflowError("CI job authorization identity or timestamp is malformed")
    key_id = verify_bytes(
        _canonical_json(payload),
        public_key_b64=authorization.get("public_key"),
        signature_b64=authorization.get("signature"),
        key_id=authorization.get("key_id"),
    )
    public_key = authorization.get("public_key")
    if not isinstance(public_key, str):
        raise WorkflowError("CI job authorization public key is malformed")
    evaluate_trust(
        policy=load_trust_policy(trust_policy_path),
        policy_name=policy_name,
        key_id=key_id,
        public_key_b64=public_key,
        actor_id=actor_id,
        trust_level="signed",
        signed_at=_parse_time(signed_at_value, "CI job authorization signed_at"),
        now=now,
    )
    return key_id


def _validate_job(job: dict[str, object], *, now: datetime | None = None) -> list[dict[str, object]]:
    if job.get("schema_version") != 1 or job.get("kind") != "aria-ci-job":
        raise WorkflowError("Unsupported ARIA CI job schema")
    for key in (
        "job_id",
        "nonce",
        "project_id",
        "run_id",
        "source_commit",
        "execution_contract_sha256",
    ):
        value = job.get(key)
        if not isinstance(value, str) or not value:
            raise WorkflowError(f"CI job {key} is missing")
    if (
        JOB_ID_RE.fullmatch(str(job["job_id"])) is None
        or HEX_64_RE.fullmatch(str(job["nonce"])) is None
        or COMMIT_RE.fullmatch(str(job["source_commit"])) is None
        or HEX_64_RE.fullmatch(str(job["execution_contract_sha256"])) is None
    ):
        raise WorkflowError("CI job identity, nonce, commit or contract SHA is invalid")
    feature_sha = job.get("feature_contract_sha256")
    if feature_sha is not None and (
        not isinstance(feature_sha, str) or HEX_64_RE.fullmatch(feature_sha) is None
    ):
        raise WorkflowError("CI job Feature Contract SHA is invalid")
    purpose = job.get("purpose", "verification")
    source_hashes = job.get("source_evidence_sha256", [])
    if (
        purpose not in {"verification", "integration"}
        or not isinstance(source_hashes, list)
        or not all(
            isinstance(value, str) and HEX_64_RE.fullmatch(value) is not None
            for value in source_hashes
        )
        or len(source_hashes) != len(set(source_hashes))
        or (purpose == "integration" and len(source_hashes) < 2)
        or (purpose == "verification" and source_hashes)
    ):
        raise WorkflowError("CI job purpose or source evidence binding is invalid")
    created = _parse_time(job.get("created_at"), "CI job created_at")
    expires = _parse_time(job.get("expires_at"), "CI job expires_at")
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    if (
        expires <= created
        or expires <= reference
        or created > reference + timedelta(minutes=5)
        or expires - created > timedelta(seconds=MAX_JOB_TTL_SECONDS)
    ):
        raise WorkflowError("CI job has expired")
    raw_commands = job.get("commands")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise WorkflowError("CI job commands must be a non-empty list")
    commands = [
        _normalized_command(raw, index) for index, raw in enumerate(raw_commands)
    ]
    if len({str(row["id"]) for row in commands}) != len(commands):
        raise WorkflowError("CI job command ids must be unique")
    required = job.get("required_execution_classes", [])
    if (
        not isinstance(required, list)
        or not all(isinstance(value, str) and value in TEST_CLASSES for value in required)
        or len(required) != len(set(required))
    ):
        raise WorkflowError("CI job required execution classes are malformed")
    missing = sorted(set(required) - _execution_classes(commands))
    if missing:
        raise WorkflowError(
            f"CI job commands do not cover required execution classes: {missing}"
        )
    return commands


def prepare_ci_job(
    project: object,
    *,
    run_id: str,
    output_path: Path,
    private_key_path: Path,
    actor_id: str,
    command_ids: list[str] | None = None,
    integration_source_packages: list[Path] | None = None,
    ttl_seconds: int = 3600,
    now: datetime | None = None,
) -> dict[str, object]:
    from aria.simple_run import _run_root, read_project_run

    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or ttl_seconds < 1
        or ttl_seconds > MAX_JOB_TTL_SECONDS
    ):
        raise ConfigurationError("CI job TTL is outside the allowed range")
    if output_path.exists():
        raise ConfigurationError(f"Refusing to overwrite CI job: {output_path}")
    run_root = _run_root(project, run_id)
    manifest = read_project_run(project, run_id)
    descriptor = manifest.get("execution_contract")
    if not isinstance(descriptor, dict):
        raise WorkflowError("Run has no immutable execution contract")
    relative = descriptor.get("path")
    expected_sha = descriptor.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected_sha, str):
        raise WorkflowError("Execution contract descriptor is malformed")
    contract_path = run_root / relative
    if not contract_path.is_file() or _sha256(contract_path.read_bytes()) != expected_sha:
        raise WorkflowError("Execution contract is missing or has changed")
    contract = _read_json(contract_path, "Execution contract")
    raw_commands = contract.get("commands")
    if not isinstance(raw_commands, list):
        raise WorkflowError("Execution contract commands are malformed")
    indexed = {
        str(row.get("id")): row
        for row in raw_commands
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    selected_ids = list(dict.fromkeys(command_ids or indexed.keys()))
    unknown = sorted(set(selected_ids) - set(indexed))
    if unknown or not selected_ids:
        raise WorkflowError(f"Unknown or empty CI command selection: {unknown}")
    commands = [
        _normalized_command(indexed[command_id], index)
        for index, command_id in enumerate(selected_ids)
    ]
    assurance = manifest.get("assurance_plan")
    raw_required = (
        assurance.get("required_execution_classes")
        if isinstance(assurance, dict)
        else []
    )
    if (
        not isinstance(raw_required, list)
        or not all(
            isinstance(value, str) and value in TEST_CLASSES for value in raw_required
        )
    ):
        raise WorkflowError("Run assurance plan is malformed")
    required_classes = sorted(set(raw_required))
    missing_classes = sorted(set(required_classes) - _execution_classes(commands))
    if missing_classes:
        raise WorkflowError(
            "Selected CI commands do not cover required execution classes: "
            f"{missing_classes}"
        )
    snapshot = git_snapshot(project.code_root, project.git_ignore_prefixes)
    if snapshot.get("dirty") is not False:
        raise WorkflowError("CI prepare requires a clean source checkout")
    feature_lock = manifest.get("feature_contract_lock")
    feature_sha = (
        feature_lock.get("sha256") if isinstance(feature_lock, dict) else None
    )
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    source_package_hashes = sorted(
        {_sha256(path.read_bytes()) for path in integration_source_packages or []}
    )
    if integration_source_packages and len(source_package_hashes) < 2:
        raise WorkflowError(
            "An integration CI job requires at least two distinct source packages"
        )
    payload = {
        "schema_version": 1,
        "kind": "aria-ci-job",
        "job_id": f"job-{uuid.uuid4().hex}",
        "nonce": uuid.uuid4().hex + uuid.uuid4().hex,
        "created_at": _stamp(reference),
        "expires_at": _stamp(reference + timedelta(seconds=ttl_seconds)),
        "project_id": project.project_id,
        "run_id": run_id,
        "source_commit": snapshot["head"],
        "feature_contract_sha256": feature_sha,
        "execution_contract_sha256": expected_sha,
        "purpose": "integration" if source_package_hashes else "verification",
        "source_evidence_sha256": source_package_hashes,
        "required_execution_classes": required_classes,
        "commands": commands,
    }
    job = authorize_ci_job(
        payload,
        private_key_path=private_key_path,
        actor_id=actor_id,
    )
    atomic_write_json(output_path, job)
    return {
        "ok": True,
        "job": str(output_path.resolve()),
        "job_id": job["job_id"],
        "job_sha256": _sha256(output_path.read_bytes()),
        "nonce": job["nonce"],
        "source_commit": job["source_commit"],
        "expires_at": job["expires_at"],
        "command_ids": selected_ids,
        "authorization_key_id": job["authorization"]["key_id"],
        "authorization_actor_id": actor_id,
    }


def _git(checkout: Path, *args: str) -> str:
    command = [
        "git",
        "-c",
        f"safe.directory={checkout.resolve()}",
        *args,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise WorkflowError(f"Git operation failed for CI isolation: {args}") from error
    return completed.stdout.strip()


def _write_unsigned_result(execution_root: Path, output_path: Path) -> dict[str, object]:
    files = [
        (
            f"ci/{path.relative_to(execution_root).as_posix()}",
            path.read_bytes(),
        )
        for path in sorted(execution_root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, content in files:
                info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        atomic_write_bytes(output_path, temporary.read_bytes())
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "unsigned_result": str(output_path.resolve()),
        "unsigned_result_sha256": _sha256(output_path.read_bytes()),
        "files": len(files),
    }


def execute_ci_job(
    *,
    job_path: Path,
    checkout: Path,
    output_path: Path,
    job_trust_policy_path: Path,
    job_policy_name: str,
    now: datetime | None = None,
) -> dict[str, object]:
    if output_path.exists():
        raise ConfigurationError(f"Refusing to overwrite CI result: {output_path}")
    job_bytes = job_path.read_bytes()
    job = _read_json(job_path, "CI job")
    authorization_key_id = _verify_job_authorization(
        job,
        trust_policy_path=job_trust_policy_path,
        policy_name=job_policy_name,
        now=now,
    )
    commands = _validate_job(job, now=now)
    source_root = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve(strict=True)
    before = git_snapshot(source_root)
    if before.get("dirty") is not False:
        raise WorkflowError("CI execute requires a clean source checkout")
    if before.get("head") != job.get("source_commit"):
        raise WorkflowError("CI job source commit does not match checkout HEAD")
    with tempfile.TemporaryDirectory(prefix="aria-ci-worktree-") as temporary_name:
        workspace = Path(temporary_name) / "checkout"
        _git(
            source_root,
            "worktree",
            "add",
            "--detach",
            str(workspace),
            str(job["source_commit"]),
        )
        execution_root = Path(temporary_name) / str(job["job_id"])
        receipts: list[dict[str, object]] = []
        descriptors: list[dict[str, object]] = []
        try:
            isolated_project = SimpleNamespace(
                code_root=workspace,
                git_ignore_prefixes=(),
            )
            isolated_git = {
                "head": job["source_commit"],
                "working_tree_sha256": canonical_sha({}),
                "changed_count": 0,
            }
            for command in commands:
                receipt, descriptor = _run_command(
                    isolated_project,
                    execution_root,
                    command,
                    git=isolated_git,
                    links={"requirement_ids": [], "acceptance_ids": []},
                    contract_sha256=str(job["execution_contract_sha256"]),
                    links_sha256=None,
                )
                receipts.append(receipt)
                descriptors.append(descriptor)
            failed = [
                str(row["command_id"])
                for row in receipts
                if row.get("status") != "passed"
            ]
            passed_classes = sorted(
                _execution_classes(
                    [
                        command
                        for command, receipt in zip(commands, receipts, strict=True)
                        if receipt.get("status") == "passed"
                    ]
                )
            )
            required_classes = list(job.get("required_execution_classes", []))
            missing_classes = sorted(set(required_classes) - set(passed_classes))
            result = {
                "schema_version": 1,
                "kind": "aria-ci-result",
                "job_id": job["job_id"],
                "job_sha256": _sha256(job_bytes),
                "nonce": job["nonce"],
                "project_id": job["project_id"],
                "run_id": job["run_id"],
                "source_commit": job["source_commit"],
                "feature_contract_sha256": job.get("feature_contract_sha256"),
                "execution_contract_sha256": job["execution_contract_sha256"],
                "purpose": job.get("purpose", "verification"),
                "source_evidence_sha256": job.get("source_evidence_sha256", []),
                "required_execution_classes": required_classes,
                "passed_execution_classes": passed_classes,
                "missing_execution_classes": missing_classes,
                "executed_at": _stamp(now),
                "executions": descriptors,
                "failed_command_ids": failed,
                "ok": not failed and not missing_classes,
            }
            atomic_write_json(execution_root / "ci-result.json", result)
            atomic_write_bytes(execution_root / "ci-job.json", job_bytes)
            unsigned = _write_unsigned_result(execution_root, output_path)
        finally:
            _git(source_root, "worktree", "remove", "--force", str(workspace))
    after = git_snapshot(source_root)
    if after != before:
        raise WorkflowError("CI isolation changed the source checkout")
    return {
        **unsigned,
        "ok": result["ok"],
        "job_id": job["job_id"],
        "authorization_key_id": authorization_key_id,
        "failed_command_ids": result["failed_command_ids"],
        "source_checkout_unchanged": True,
    }


def attest_ci_result(
    *,
    job_path: Path,
    unsigned_result_path: Path,
    output_path: Path,
    private_key_path: Path,
    actor_id: str,
    job_trust_policy_path: Path,
    job_policy_name: str,
) -> dict[str, object]:
    if ACTOR_ID_RE.fullmatch(actor_id) is None:
        raise ConfigurationError(f"Invalid CI attester actor id: {actor_id!r}")
    if output_path.exists():
        raise ConfigurationError(f"Refusing to overwrite CI package: {output_path}")
    job_bytes = job_path.read_bytes()
    job = _read_json(job_path, "CI job")
    _verify_job_authorization(
        job,
        trust_policy_path=job_trust_policy_path,
        policy_name=job_policy_name,
    )
    commands = _validate_job(job)
    unsigned_bytes = _read_package_bytes(
        unsigned_result_path, label="Unsigned CI result"
    )
    archive, entries = _archive_entries(
        unsigned_result_path, content=unsigned_bytes
    )
    try:
        if not {
            "ci/ci-job.json",
            "ci/ci-result.json",
        }.issubset(entries):
            raise WorkflowError("Unsigned CI result metadata is incomplete")
        archived_job = archive.read(entries["ci/ci-job.json"])
        if archived_job != job_bytes:
            raise WorkflowError("Unsigned CI result belongs to another authorized job")
        try:
            result = json.loads(archive.read(entries["ci/ci-result.json"]))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowError("Unsigned CI result JSON is malformed") from error
        if not isinstance(result, dict):
            raise WorkflowError("Unsigned CI result must be a JSON object")
        bindings = {
            "job_id": job["job_id"],
            "job_sha256": _sha256(job_bytes),
            "nonce": job["nonce"],
            "project_id": job["project_id"],
            "run_id": job["run_id"],
            "source_commit": job["source_commit"],
            "feature_contract_sha256": job.get("feature_contract_sha256"),
            "execution_contract_sha256": job["execution_contract_sha256"],
            "purpose": job.get("purpose", "verification"),
            "source_evidence_sha256": job.get("source_evidence_sha256", []),
            "required_execution_classes": job.get(
                "required_execution_classes", []
            ),
        }
        if any(result.get(key) != value for key, value in bindings.items()):
            raise WorkflowError("Unsigned CI result job binding is invalid")
        descriptors = result.get("executions")
        if (
            not isinstance(descriptors, list)
            or len(descriptors) != len(commands)
            or [
                descriptor.get("command_id")
                for descriptor in descriptors
                if isinstance(descriptor, dict)
            ]
            != [command["id"] for command in commands]
        ):
            raise WorkflowError("Unsigned CI result has no execution receipts")
        failed: list[str] = []
        expected_git = {
            "head": job["source_commit"],
            "working_tree_sha256": canonical_sha({}),
            "changed_count": 0,
        }
        for command, descriptor in zip(commands, descriptors, strict=True):
            if not isinstance(descriptor, dict):
                raise WorkflowError("Unsigned CI execution descriptor is malformed")
            receipt_relative = descriptor.get("receipt_path")
            if not isinstance(receipt_relative, str):
                raise WorkflowError("Unsigned CI receipt path is malformed")
            receipt_name = f"ci/{receipt_relative}"
            receipt_info = entries.get(receipt_name)
            if receipt_info is None:
                raise WorkflowError(f"Unsigned CI receipt is missing: {receipt_name}")
            receipt_bytes = archive.read(receipt_info)
            if descriptor.get("receipt_sha256") != _sha256(receipt_bytes):
                raise WorkflowError(f"Unsigned CI receipt changed: {receipt_name}")
            try:
                receipt = json.loads(receipt_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise WorkflowError(f"Unsigned CI receipt is malformed: {receipt_name}") from error
            if not isinstance(receipt, dict):
                raise WorkflowError(f"Unsigned CI receipt is malformed: {receipt_name}")
            receipt_argv = receipt.get("argv")
            started_at = _parse_time(
                receipt.get("started_at"), "Unsigned CI receipt started_at"
            )
            finished_at = _parse_time(
                receipt.get("finished_at"), "Unsigned CI receipt finished_at"
            )
            duration = receipt.get("duration_seconds")
            if (
                receipt.get("schema_version") != 1
                or receipt.get("run_id") != job["job_id"]
                or receipt.get("command_id") != command["id"]
                or receipt.get("adapter") != command["adapter"]
                or receipt.get("command_contract_sha256") != canonical_sha(command)
                or receipt.get("execution_contract_sha256")
                != job["execution_contract_sha256"]
                or receipt.get("links_sha256") is not None
                or receipt.get("cwd") != command["cwd"]
                or receipt.get("classes") != command["classes"]
                or receipt.get("timeout_seconds") != command["timeout_seconds"]
                or receipt.get("requirement_ids") != []
                or receipt.get("acceptance_ids") != []
                or receipt.get("git") != expected_git
                or receipt.get("git_after") != expected_git
                or receipt.get("resolved_executable_sha256")
                != receipt.get("resolved_executable_sha256_after")
                or not isinstance(receipt_argv, list)
                or len(receipt_argv) != len(command["argv"])
                or receipt_argv[1:] != command["argv"][1:]
                or finished_at < started_at
                or isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or duration < 0
                or duration > int(command["timeout_seconds"]) + 60
            ):
                raise WorkflowError(
                    f"Unsigned CI receipt contract is invalid: {receipt_name}"
                )
            output_relative = receipt.get("output_path")
            if not isinstance(output_relative, str):
                raise WorkflowError("Unsigned CI output path is malformed")
            output_name = f"ci/{output_relative}"
            output_info = entries.get(output_name)
            if (
                output_info is None
                or receipt.get("output_sha256")
                != _sha256(archive.read(output_info))
                or receipt.get("output_size") != output_info.file_size
                or receipt.get("execution_id") != descriptor.get("execution_id")
                or receipt.get("command_id") != descriptor.get("command_id")
                or receipt.get("status") != descriptor.get("status")
            ):
                raise WorkflowError(f"Unsigned CI output or identity changed: {output_name}")
            if receipt.get("status") == "passed":
                if (
                    receipt.get("exit_code") != 0
                    or receipt.get("launch_error") is not None
                ):
                    raise WorkflowError(
                        f"Unsigned CI PASS receipt is invalid: {receipt_name}"
                    )
            else:
                failed.append(str(receipt.get("command_id")))
        expected_passed = sorted(
            _execution_classes(
                [
                    command
                    for command, descriptor in zip(commands, descriptors, strict=True)
                    if str(descriptor.get("command_id")) not in failed
                ]
            )
        )
        expected_missing = sorted(
            set(job.get("required_execution_classes", [])) - set(expected_passed)
        )
        if (
            result.get("failed_command_ids") != failed
            or result.get("passed_execution_classes") != expected_passed
            or result.get("missing_execution_classes") != expected_missing
            or result.get("ok") is not (not failed and not expected_missing)
        ):
            raise WorkflowError("Unsigned CI aggregate verdict differs from receipts")
        files = [
            (name, archive.read(info))
            for name, info in sorted(entries.items())
        ]
        subject = {
            "kind": "ci-result",
            **bindings,
            "result_ok": result["ok"],
            "passed_execution_classes": result["passed_execution_classes"],
            "missing_execution_classes": result["missing_execution_classes"],
        }
    finally:
        archive.close()
    package = build_signed_package(
        files=files,
        output_path=output_path,
        private_key_path=private_key_path,
        subject=subject,
        trust_level="ci-signed",
        actor_id=actor_id,
    )
    return {
        **package,
        "ok": result["ok"],
        "failed_command_ids": result["failed_command_ids"],
    }


def import_ci_result(
    project: object,
    *,
    run_id: str,
    job_path: Path,
    package_path: Path,
    trust_policy_path: Path,
    policy_name: str,
    job_trust_policy_path: Path,
    job_policy_name: str,
) -> dict[str, object]:
    from aria.simple_run import read_project_run

    job_bytes = job_path.read_bytes()
    job = _read_json(job_path, "CI job")
    _verify_job_authorization(
        job,
        trust_policy_path=job_trust_policy_path,
        policy_name=job_policy_name,
    )
    _validate_job(job)
    destination = (
        Path(project.runtime_root)
        / "ci"
        / "imports"
        / f"{job['job_id']}.aria-evidence"
    )
    record_path = destination.with_suffix(".json")
    lock_path = (
        Path(project.runtime_root) / "locks" / f"ci-import-{job['job_id']}.lock"
    )
    run_lock_path = Path(project.runtime_root) / "locks" / f"{run_id}.lock"
    with (
        exclusive_lock(run_lock_path, timeout_seconds=120.0),
        exclusive_lock(lock_path),
    ):
        manifest = read_project_run(project, run_id)
        if destination.exists() and record_path.exists():
            raise WorkflowError(f"CI job has already been imported: {job['job_id']}")
        incoming = _read_package_bytes(
            package_path, label="CI evidence package"
        )
        staged = destination.with_suffix(f".{uuid.uuid4().hex}.pending")
        atomic_write_bytes(staged, incoming)
        try:
            selected_policy = load_trust_policy(trust_policy_path)[
                "policies"
            ].get(policy_name)
            if not isinstance(selected_policy, dict):
                raise WorkflowError(f"Unknown trust policy: {policy_name}")
            actor_roles: list[object] | None = None
            if selected_policy.get("allowed_actor_roles"):
                from aria.team import load_team

                inspected_actor = inspect_package(staged).get("actor_id")
                actors = load_team(project)
                actor = actors.get(str(inspected_actor))
                if not isinstance(actor, dict):
                    raise WorkflowError(
                        "CI evidence actor is absent from ARIA_TEAM.yaml"
                    )
                actor_roles = list(actor["roles"])
            verdict = verify_package(
                staged,
                trust_policy_path=trust_policy_path,
                policy_name=policy_name,
                actor_roles=actor_roles,
                approval_count=0,
            )
            subject = verdict.get("subject")
            expected = {
                "kind": "ci-result",
                "job_id": job["job_id"],
                "job_sha256": _sha256(job_bytes),
                "nonce": job["nonce"],
                "project_id": project.project_id,
                "run_id": run_id,
                "source_commit": job["source_commit"],
                "feature_contract_sha256": job.get("feature_contract_sha256"),
                "execution_contract_sha256": job["execution_contract_sha256"],
                "purpose": job.get("purpose", "verification"),
                "source_evidence_sha256": job.get("source_evidence_sha256", []),
                "required_execution_classes": job.get(
                    "required_execution_classes", []
                ),
            }
            if not isinstance(subject, dict) or any(
                subject.get(key) != value for key, value in expected.items()
            ):
                raise WorkflowError(
                    "CI result does not match job, commit, contract or nonce"
                )
            if subject.get("result_ok") is not True:
                raise WorkflowError("CI result contains failed commands")
            passed_classes = subject.get("passed_execution_classes")
            if (
                subject.get("missing_execution_classes") != []
                or not isinstance(passed_classes, list)
                or not all(isinstance(value, str) for value in passed_classes)
                or not set(job.get("required_execution_classes", [])).issubset(
                    set(passed_classes)
                )
            ):
                raise WorkflowError(
                    "CI result does not cover required execution classes"
                )
            current_git = git_snapshot(
                project.code_root, project.git_ignore_prefixes
            )
            if (
                current_git.get("dirty") is not False
                or current_git.get("head") != job["source_commit"]
            ):
                raise WorkflowError(
                    "CI result source commit differs from current clean checkout"
                )
            execution = manifest.get("execution_contract")
            feature_lock = manifest.get("feature_contract_lock")
            assurance = manifest.get("assurance_plan")
            current_required = sorted(
                set(
                    assurance.get("required_execution_classes", [])
                    if isinstance(assurance, dict)
                    else []
                )
            )
            if (
                not isinstance(execution, dict)
                or execution.get("sha256")
                != job["execution_contract_sha256"]
                or (
                    job.get("feature_contract_sha256") is not None
                    and (
                        not isinstance(feature_lock, dict)
                        or feature_lock.get("sha256")
                        != job["feature_contract_sha256"]
                    )
                )
                or current_required
                != sorted(job.get("required_execution_classes", []))
            ):
                raise WorkflowError(
                    "CI result contract binding differs from current run"
                )
            if destination.exists():
                if destination.read_bytes() != incoming:
                    raise WorkflowError("Incomplete CI import contains different bytes")
                recovered = True
            else:
                os.replace(staged, destination)
                recovered = False
            record = {
                "schema_version": 1,
                "job_id": job["job_id"],
                "run_id": run_id,
                "package_path": str(destination),
                "package_sha256": verdict["package_sha256"],
                "key_id": verdict["key_id"],
                "actor_id": verdict.get("actor_id"),
                "trust": verdict["trust"],
                "imported_at": _stamp(),
                "recovered": recovered,
            }
            atomic_write_json(record_path, record)
            return {"ok": True, **record}
        finally:
            staged.unlink(missing_ok=True)


def github_workflow(*, project_id: str) -> str:
    if not project_id or any(character in project_id for character in "\r\n'\""):
        raise ConfigurationError("Invalid project id for GitHub workflow")
    return f"""name: ARIA trusted CI

on:
  workflow_dispatch:
    inputs:
      job_json_b64:
        description: Base64 ARIA CI job produced by `aria ci prepare`
        required: true

permissions:
  contents: read

jobs:
  execute:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          fetch-depth: 0
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: '3.12'
      - name: Install trusted ARIA wheel
        env:
          ARIA_WHEELHOUSE_URL: ${{{{ vars.ARIA_WHEELHOUSE_URL }}}}
          ARIA_WHEELHOUSE_SHA256: ${{{{ vars.ARIA_WHEELHOUSE_SHA256 }}}}
        run: |
          curl -fsSL "$ARIA_WHEELHOUSE_URL" -o "$RUNNER_TEMP/aria-wheelhouse.zip"
          printf '%s  %s\\n' "$ARIA_WHEELHOUSE_SHA256" "$RUNNER_TEMP/aria-wheelhouse.zip" | sha256sum -c -
          python -m zipfile -e "$RUNNER_TEMP/aria-wheelhouse.zip" "$RUNNER_TEMP/wheelhouse"
          python -m pip install --no-index --find-links "$RUNNER_TEMP/wheelhouse" aria-codex=={__version__}
          python -m pip check
      - name: Materialize authorized job and public trust policy
        env:
          ARIA_JOB_B64: ${{{{ inputs.job_json_b64 }}}}
          ARIA_JOB_TRUST_B64: ${{{{ secrets.ARIA_JOB_TRUST_B64 }}}}
        run: |
          printf '%s' "$ARIA_JOB_B64" | base64 --decode > "$RUNNER_TEMP/aria-ci-job.json"
          printf '%s' "$ARIA_JOB_TRUST_B64" | base64 --decode > "$RUNNER_TEMP/job-trust.yaml"
      - name: Execute authorized job without signing secrets
        run: |
          aria ci execute --job "$RUNNER_TEMP/aria-ci-job.json" --checkout . --output "$RUNNER_TEMP/{project_id}-unsigned.zip" --job-trust-policy "$RUNNER_TEMP/job-trust.yaml" --job-policy job
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: aria-ci-unsigned
          path: ${{{{ runner.temp }}}}/{project_id}-unsigned.zip
          if-no-files-found: error

  attest:
    needs: execute
    runs-on: ubuntu-latest
    environment: aria-signing
    steps:
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5.6.0
        with:
          python-version: '3.12'
      - name: Install trusted ARIA wheel
        env:
          ARIA_WHEELHOUSE_URL: ${{{{ vars.ARIA_WHEELHOUSE_URL }}}}
          ARIA_WHEELHOUSE_SHA256: ${{{{ vars.ARIA_WHEELHOUSE_SHA256 }}}}
        run: |
          curl -fsSL "$ARIA_WHEELHOUSE_URL" -o "$RUNNER_TEMP/aria-wheelhouse.zip"
          printf '%s  %s\\n' "$ARIA_WHEELHOUSE_SHA256" "$RUNNER_TEMP/aria-wheelhouse.zip" | sha256sum -c -
          python -m zipfile -e "$RUNNER_TEMP/aria-wheelhouse.zip" "$RUNNER_TEMP/wheelhouse"
          python -m pip install --no-index --find-links "$RUNNER_TEMP/wheelhouse" aria-codex=={__version__}
          python -m pip check
      - uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0
        with:
          name: aria-ci-unsigned
          path: ${{{{ runner.temp }}}}
      - name: Materialize attestation inputs
        env:
          ARIA_JOB_B64: ${{{{ inputs.job_json_b64 }}}}
          ARIA_JOB_TRUST_B64: ${{{{ secrets.ARIA_JOB_TRUST_B64 }}}}
          ARIA_SIGNING_KEY_PEM: ${{{{ secrets.ARIA_SIGNING_KEY_PEM }}}}
        run: |
          printf '%s' "$ARIA_JOB_B64" | base64 --decode > "$RUNNER_TEMP/aria-ci-job.json"
          printf '%s' "$ARIA_JOB_TRUST_B64" | base64 --decode > "$RUNNER_TEMP/job-trust.yaml"
          printf '%s' "$ARIA_SIGNING_KEY_PEM" > "$RUNNER_TEMP/aria-ci-private.pem"
          chmod 600 "$RUNNER_TEMP/aria-ci-private.pem"
      - name: Attest verified receipts outside the tested checkout
        run: >-
          aria ci attest
          --job "$RUNNER_TEMP/aria-ci-job.json"
          --result "$RUNNER_TEMP/{project_id}-unsigned.zip"
          --output "$RUNNER_TEMP/{project_id}-ci.aria-evidence"
          --private-key "$RUNNER_TEMP/aria-ci-private.pem"
          --actor github-actions
          --job-trust-policy "$RUNNER_TEMP/job-trust.yaml"
          --job-policy job
      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: aria-ci-evidence
          path: ${{{{ runner.temp }}}}/{project_id}-ci.aria-evidence
          if-no-files-found: error
"""


def write_github_workflow(*, project_id: str, output_path: Path) -> dict[str, object]:
    if output_path.exists():
        raise ConfigurationError(f"Refusing to overwrite GitHub workflow: {output_path}")
    content = github_workflow(project_id=project_id).encode("utf-8")
    atomic_write_bytes(output_path, content)
    return {
        "ok": True,
        "workflow": str(output_path.resolve()),
        "sha256": _sha256(content),
        "adapter": "github-actions",
        "protocol": "aria-ci-job/v1",
    }
