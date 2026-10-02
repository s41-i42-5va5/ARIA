from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes
from aria.signing import sign_bytes, verify_bytes
from aria.trust import (
    TRUST_LEVELS,
    evaluate_policy_context,
    evaluate_trust,
    load_trust_policy,
)

PACKAGE_SCHEMA_VERSION = 2
MAX_PACKAGE_FILES = 4096
MAX_PACKAGE_FILE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
SAFE_ARCHIVE_PATH = re.compile(r"[A-Za-z0-9._/-]+")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _safe_archive_name(value: str) -> str:
    normalized = value.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or ".." in pure.parts
        or "\\" in value
        or SAFE_ARCHIVE_PATH.fullmatch(normalized) is None
    ):
        raise WorkflowError(f"Unsafe evidence package path: {value!r}")
    return pure.as_posix()


def _read_json(content: bytes, label: str) -> dict[str, object]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise WorkflowError(f"{label} must contain a JSON object")
    return payload


def _run_files(run_root: Path) -> list[tuple[str, bytes]]:
    resolved_root = run_root.resolve(strict=True)
    files: list[tuple[str, bytes]] = []
    total = 0
    for path in sorted(run_root.rglob("*")):
        if not path.is_file() or path.name.endswith(".lock"):
            continue
        if path.is_symlink():
            raise WorkflowError(f"Evidence source contains a symlink: {path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise WorkflowError(f"Evidence source escapes run root: {path}")
        relative = _safe_archive_name(path.relative_to(run_root).as_posix())
        content = path.read_bytes()
        if len(content) > MAX_PACKAGE_FILE_BYTES:
            raise WorkflowError(f"Evidence file exceeds size limit: {relative}")
        total += len(content)
        if total > MAX_PACKAGE_BYTES or len(files) >= MAX_PACKAGE_FILES:
            raise WorkflowError("Evidence source exceeds package limits")
        files.append((f"evidence/{relative}", content))
    if not files:
        raise WorkflowError("Evidence run contains no files")
    return files


def build_signed_package(
    *,
    files: list[tuple[str, bytes]],
    output_path: Path,
    private_key_path: Path,
    subject: dict[str, object],
    trust_level: str,
    actor_id: str | None = None,
) -> dict[str, object]:
    if trust_level not in {"signed", "ci-signed"}:
        raise ConfigurationError("Signed package trust_level must be signed or ci-signed")
    if output_path.exists():
        raise ConfigurationError(f"Refusing to overwrite evidence package: {output_path}")
    seen: set[str] = set()
    inventory: list[dict[str, object]] = []
    normalized_files: list[tuple[str, bytes]] = []
    total = 0
    for raw_name, content in sorted(files):
        name = _safe_archive_name(raw_name)
        if name in {"package.json", "signature.json"} or name in seen:
            raise WorkflowError(f"Duplicate or reserved package path: {name}")
        if not isinstance(content, bytes):
            raise WorkflowError(f"Package content must be bytes: {name}")
        if len(content) > MAX_PACKAGE_FILE_BYTES:
            raise WorkflowError(f"Package file exceeds size limit: {name}")
        total += len(content)
        if total > MAX_PACKAGE_BYTES or len(seen) >= MAX_PACKAGE_FILES:
            raise WorkflowError("Evidence package exceeds limits")
        seen.add(name)
        normalized_files.append((name, content))
        inventory.append(
            {"path": name, "size": len(content), "sha256": _sha256(content)}
        )
    if not normalized_files:
        raise WorkflowError("Evidence package requires at least one evidence file")
    created_at = _stamp()
    manifest: dict[str, object] = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "kind": "aria-evidence-package",
        "created_at": created_at,
        "trust_level": trust_level,
        "subject": subject,
        "files": inventory,
    }
    if actor_id is not None:
        if not actor_id.strip():
            raise ConfigurationError("actor_id cannot be empty")
        manifest["actor_id"] = actor_id.strip()
    manifest_bytes = _canonical_json(manifest)
    signature = sign_bytes(manifest_bytes, private_key_path)
    signature["signed_at"] = created_at
    signature["subject_sha256"] = _sha256(manifest_bytes)
    signature_bytes = _canonical_json(signature)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp", delete=False
    ) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for name, content in [
                ("package.json", manifest_bytes),
                ("signature.json", signature_bytes),
                *normalized_files,
            ]:
                info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        package_bytes = temporary.read_bytes()
        if len(package_bytes) > MAX_PACKAGE_BYTES:
            raise WorkflowError("Compressed evidence package exceeds size limit")
        atomic_write_bytes(output_path, package_bytes)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "ok": True,
        "package": str(output_path.resolve()),
        "package_sha256": _sha256(output_path.read_bytes()),
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "trust_level": trust_level,
        "key_id": signature["key_id"],
        "files": len(inventory),
        "subject": subject,
    }


def export_run_package(
    *,
    project: object,
    run_id: str,
    output_path: Path,
    private_key_path: Path,
    actor_id: str | None = None,
) -> dict[str, object]:
    from aria.simple_run import _run_root, read_project_run

    run_root = _run_root(project, run_id)
    manifest = read_project_run(project, run_id)
    execution_bundle = manifest.get("execution_bundle")
    if not isinstance(execution_bundle, dict):
        raise WorkflowError("Run has no verified Evidence Bundle to export")
    bundle_relative = execution_bundle.get("path")
    bundle_sha = execution_bundle.get("sha256")
    if not isinstance(bundle_relative, str) or not isinstance(bundle_sha, str):
        raise WorkflowError("Run Evidence Bundle descriptor is malformed")
    bundle_path = run_root / bundle_relative
    if (
        not bundle_path.is_file()
        or not bundle_path.resolve(strict=True).is_relative_to(run_root.resolve(strict=True))
        or _sha256(bundle_path.read_bytes()) != bundle_sha
    ):
        raise WorkflowError("Run Evidence Bundle is missing or has changed")
    bundle = _read_json(bundle_path.read_bytes(), "Evidence Bundle")
    bundle_git = bundle.get("git")
    if (
        bundle.get("run_id") != run_id
        or bundle.get("ok") is not True
        or not isinstance(bundle_git, dict)
        or not isinstance(bundle_git.get("head"), str)
        or not isinstance(bundle_git.get("working_tree_sha256"), str)
        or not isinstance(bundle_git.get("changed_count"), int)
    ):
        raise WorkflowError("Run Evidence Bundle Git identity is malformed")
    result_path = run_root / "result.json"
    if result_path.is_file():
        result = _read_json(result_path.read_bytes(), "Run result")
        trace = result.get("trace_evidence")
        git_trace = trace.get("git") if isinstance(trace, dict) else None
        final_head = (
            git_trace.get("head_commit") if isinstance(git_trace, dict) else None
        )
        if isinstance(final_head, str) and final_head != bundle_git["head"]:
            raise WorkflowError(
                "Run result implementation head differs from verified Git head"
            )
    subject = {
        "kind": "run",
        "project_id": manifest.get("project"),
        "run_id": manifest.get("run_id"),
        "run_status": manifest.get("status"),
        "source_commit": bundle_git["head"],
        "source_working_tree_sha256": bundle_git["working_tree_sha256"],
        "source_changed_count": bundle_git["changed_count"],
        "feature_contract_sha256": (
            manifest.get("feature_contract_lock", {}).get("sha256")
            if isinstance(manifest.get("feature_contract_lock"), dict)
            else None
        ),
        "execution_contract_sha256": (
            manifest.get("execution_contract", {}).get("sha256")
            if isinstance(manifest.get("execution_contract"), dict)
            else None
        ),
        "evidence_bundle_sha256": bundle_sha,
    }
    return build_signed_package(
        files=_run_files(run_root),
        output_path=output_path,
        private_key_path=private_key_path,
        subject=subject,
        trust_level="signed",
        actor_id=actor_id,
    )


def create_review_attestation(
    *,
    output_path: Path,
    private_key_path: Path,
    reviewer_id: str,
    project_id: str,
    target_commit: str,
    source_package_paths: list[Path],
    integration_package_path: Path,
) -> dict[str, object]:
    if len(source_package_paths) < 2:
        raise ConfigurationError("Review attestation requires at least two sources")
    source_hashes = sorted(
        {_sha256(path.read_bytes()) for path in source_package_paths}
    )
    if len(source_hashes) != len(source_package_paths):
        raise WorkflowError("Review source packages must be distinct")
    approval = {
        "schema_version": 1,
        "kind": "aria-review-approval",
        "decision": "approved",
        "project_id": project_id,
        "target_commit": target_commit,
        "source_evidence_sha256": source_hashes,
        "integration_evidence_sha256": _sha256(
            integration_package_path.read_bytes()
        ),
        "reviewer_id": reviewer_id,
    }
    return build_signed_package(
        files=[("review/approval.json", _canonical_json(approval))],
        output_path=output_path,
        private_key_path=private_key_path,
        subject={
            "kind": "review-approval",
            "decision": "approved",
            "project_id": project_id,
            "target_commit": target_commit,
            "source_evidence_sha256": source_hashes,
            "integration_evidence_sha256": approval[
                "integration_evidence_sha256"
            ],
        },
        trust_level="signed",
        actor_id=reviewer_id,
    )


def _archive_entries(
    path: Path, *, content: bytes | None = None
) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    try:
        compressed_size = len(content) if content is not None else path.stat().st_size
    except OSError as error:
        raise WorkflowError(f"Evidence package is not readable: {path}") from error
    if compressed_size > MAX_PACKAGE_BYTES:
        raise WorkflowError("Compressed evidence package exceeds size limit")
    try:
        archive = zipfile.ZipFile(
            io.BytesIO(content) if content is not None else path,
            "r",
        )
    except (OSError, zipfile.BadZipFile) as error:
        raise WorkflowError(f"Evidence package is not a readable ZIP: {path}") from error
    infos = archive.infolist()
    if len(infos) > MAX_PACKAGE_FILES + 2:
        archive.close()
        raise WorkflowError("Evidence package contains too many files")
    entries: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = _safe_archive_name(info.filename)
        if name != info.filename or name in entries or info.is_dir():
            archive.close()
            raise WorkflowError("Evidence package contains duplicate or unsafe paths")
        if info.file_size > MAX_PACKAGE_FILE_BYTES:
            archive.close()
            raise WorkflowError(f"Evidence package file exceeds size limit: {name}")
        total += info.file_size
        if total > MAX_PACKAGE_BYTES:
            archive.close()
            raise WorkflowError("Evidence package expands beyond size limit")
        entries[name] = info
    return archive, entries


def _read_package_bytes(path: Path, *, label: str = "Evidence package") -> bytes:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_PACKAGE_BYTES + 1)
    except OSError as error:
        raise WorkflowError(f"{label} is not readable: {path}") from error
    if len(content) > MAX_PACKAGE_BYTES:
        raise WorkflowError(f"Compressed {label.lower()} exceeds size limit")
    return content


def verify_package(
    package_path: Path,
    *,
    trust_policy_path: Path | None = None,
    policy_name: str = "default",
    actor_roles: list[object] | None = None,
    approval_count: int | None = None,
) -> dict[str, object]:
    package_bytes = _read_package_bytes(package_path)
    archive, entries = _archive_entries(package_path, content=package_bytes)
    try:
        if "package.json" not in entries or "signature.json" not in entries:
            raise WorkflowError("Evidence package metadata is incomplete")
        manifest_bytes = archive.read(entries["package.json"])
        signature = _read_json(archive.read(entries["signature.json"]), "signature.json")
        manifest = _read_json(manifest_bytes, "package.json")
        if (
            manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION
            or manifest.get("kind") != "aria-evidence-package"
        ):
            raise WorkflowError("Unsupported evidence package schema")
        if signature.get("algorithm") != "Ed25519":
            raise WorkflowError("Unsupported evidence signature algorithm")
        if signature.get("subject_sha256") != _sha256(manifest_bytes):
            raise WorkflowError("Evidence manifest digest mismatch")
        key_id = verify_bytes(
            manifest_bytes,
            public_key_b64=signature.get("public_key"),
            signature_b64=signature.get("signature"),
            key_id=signature.get("key_id"),
        )
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list):
            raise WorkflowError("Evidence package inventory is malformed")
        expected_names = {"package.json", "signature.json"}
        total = 0
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict):
                raise WorkflowError(f"Evidence inventory item {index} is malformed")
            name = _safe_archive_name(str(item.get("path", "")))
            if name in expected_names:
                raise WorkflowError(f"Duplicate evidence inventory path: {name}")
            info = entries.get(name)
            if info is None:
                raise WorkflowError(f"Evidence package file is missing: {name}")
            content = archive.read(info)
            if item.get("size") != len(content) or item.get("sha256") != _sha256(content):
                raise WorkflowError(f"Evidence package file integrity failed: {name}")
            expected_names.add(name)
            total += len(content)
        if set(entries) != expected_names:
            raise WorkflowError("Evidence package contains an unlisted file")
        subject = manifest.get("subject")
        if not isinstance(subject, dict):
            raise WorkflowError("Evidence package subject is malformed")
        if subject.get("kind") == "ci-result":
            for required in ("ci/ci-job.json", "ci/ci-result.json"):
                if required not in entries:
                    raise WorkflowError(f"CI evidence package is missing: {required}")
            job_bytes = archive.read(entries["ci/ci-job.json"])
            result = _read_json(
                archive.read(entries["ci/ci-result.json"]), "ci/ci-result.json"
            )
            if subject.get("job_sha256") != _sha256(job_bytes):
                raise WorkflowError("CI evidence job digest differs from signed subject")
            bindings = {
                "job_id": "job_id",
                "nonce": "nonce",
                "project_id": "project_id",
                "run_id": "run_id",
                "source_commit": "source_commit",
                "feature_contract_sha256": "feature_contract_sha256",
                "execution_contract_sha256": "execution_contract_sha256",
                "purpose": "purpose",
                "source_evidence_sha256": "source_evidence_sha256",
                "required_execution_classes": "required_execution_classes",
                "passed_execution_classes": "passed_execution_classes",
                "missing_execution_classes": "missing_execution_classes",
                "result_ok": "ok",
            }
            if any(
                subject.get(subject_key) != result.get(result_key)
                for subject_key, result_key in bindings.items()
            ):
                raise WorkflowError(
                    "CI result content differs from the signed package subject"
                )
        if subject.get("kind") == "run" and isinstance(
            subject.get("evidence_bundle_sha256"), str
        ):
            bundle = entries.get("evidence/execution/bundle.json")
            if (
                bundle is None
                or _sha256(archive.read(bundle))
                != subject.get("evidence_bundle_sha256")
            ):
                raise WorkflowError(
                    "Run Evidence Bundle differs from the signed package subject"
                )
        if subject.get("kind") == "review-approval":
            approval_info = entries.get("review/approval.json")
            if approval_info is None:
                raise WorkflowError("Review package is missing its approval record")
            approval = _read_json(
                archive.read(approval_info), "review/approval.json"
            )
            bindings = (
                "decision",
                "project_id",
                "target_commit",
                "source_evidence_sha256",
                "integration_evidence_sha256",
            )
            if (
                approval.get("schema_version") != 1
                or approval.get("kind") != "aria-review-approval"
                or any(approval.get(key) != subject.get(key) for key in bindings)
                or approval.get("reviewer_id") != manifest.get("actor_id")
            ):
                raise WorkflowError(
                    "Review approval content differs from the signed subject"
                )
        signed_at_value = signature.get("signed_at")
        if not isinstance(signed_at_value, str):
            raise WorkflowError("Evidence signature timestamp is missing")
        try:
            signed_at = datetime.fromisoformat(signed_at_value).astimezone(UTC)
        except ValueError as error:
            raise WorkflowError("Evidence signature timestamp is invalid") from error
        if manifest.get("created_at") != signed_at_value:
            raise WorkflowError("Evidence signature and manifest timestamps differ")
        trust: dict[str, object] = {
            "evaluated": False,
            "trusted": False,
            "trust_level": manifest.get("trust_level"),
        }
        if trust_policy_path is not None:
            public_key_b64 = signature.get("public_key")
            trust_level = manifest.get("trust_level")
            if not isinstance(public_key_b64, str) or not isinstance(trust_level, str):
                raise WorkflowError("Evidence trust metadata is malformed")
            verdict = evaluate_trust(
                policy=load_trust_policy(trust_policy_path),
                policy_name=policy_name,
                key_id=key_id,
                public_key_b64=public_key_b64,
                actor_id=(
                    manifest.get("actor_id")
                    if isinstance(manifest.get("actor_id"), str)
                    else None
                ),
                trust_level=trust_level,
                signed_at=signed_at,
                assurance_classes=(
                    subject.get("passed_execution_classes")
                    if isinstance(subject.get("passed_execution_classes"), list)
                    else []
                ),
                actor_roles=actor_roles,
                approval_count=approval_count,
            )
            trust = {
                "evaluated": True,
                "trusted": True,
                **verdict,
            }
        return {
            "ok": True,
            "package": str(package_path.resolve()),
            "package_sha256": _sha256(package_bytes),
            "signature_valid": True,
            "key_id": key_id,
            "trust": trust,
            "files": len(raw_files),
            "evidence_bytes": total,
            "subject": subject,
            "actor_id": manifest.get("actor_id"),
        }
    finally:
        archive.close()


def inspect_package(package_path: Path) -> dict[str, object]:
    archive, entries = _archive_entries(package_path)
    try:
        if "package.json" not in entries or "signature.json" not in entries:
            raise WorkflowError("Evidence package metadata is incomplete")
        manifest = _read_json(archive.read(entries["package.json"]), "package.json")
        signature = _read_json(archive.read(entries["signature.json"]), "signature.json")
        raw_files = manifest.get("files")
        return {
            "ok": True,
            "package": str(package_path.resolve()),
            "schema_version": manifest.get("schema_version"),
            "trust_level": manifest.get("trust_level"),
            "created_at": manifest.get("created_at"),
            "key_id": signature.get("key_id"),
            "actor_id": manifest.get("actor_id"),
            "subject": manifest.get("subject"),
            "files": len(raw_files) if isinstance(raw_files, list) else None,
            "signature_checked": False,
        }
    finally:
        archive.close()


def _legacy_bundle(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"Evidence is neither a v2 package nor readable JSON: {path}") from error
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or not isinstance(raw.get("run_id"), str)
        or not isinstance(raw.get("executions"), list)
    ):
        raise WorkflowError("Unsupported legacy Evidence Bundle")
    return raw


def verify_evidence(
    path: Path,
    *,
    trust_policy_path: Path | None = None,
    policy_name: str = "default",
    actor_roles: list[object] | None = None,
    approval_count: int | None = None,
) -> dict[str, object]:
    if zipfile.is_zipfile(path):
        return verify_package(
            path,
            trust_policy_path=trust_policy_path,
            policy_name=policy_name,
            actor_roles=actor_roles,
            approval_count=approval_count,
        )
    bundle = _legacy_bundle(path)
    trust: dict[str, object] = {
        "evaluated": False,
        "trusted": False,
        "trust_level": "local",
    }
    if trust_policy_path is not None:
        policy = load_trust_policy(trust_policy_path)
        policies = policy["policies"]
        assert isinstance(policies, dict)
        selected = policies.get(policy_name)
        if not isinstance(selected, dict):
            raise WorkflowError(f"Unknown trust policy: {policy_name}")
        minimum = selected.get("minimum_trust_level")
        if minimum not in TRUST_LEVELS:
            raise WorkflowError(f"Trust policy {policy_name!r} is malformed")
        if TRUST_LEVELS["local"] < TRUST_LEVELS[str(minimum)]:
            raise WorkflowError(
                "Legacy Evidence Bundle v1 is local-only and cannot satisfy "
                f"{minimum!r} policy"
            )
        context = evaluate_policy_context(
            policy=policy,
            policy_name=policy_name,
            actor_roles=actor_roles or [],
            approval_count=approval_count or 0,
        )
        trust = {
            "evaluated": True,
            "trusted": True,
            "trust_level": "local",
            "minimum_trust_level": minimum,
            "policy": policy_name,
            "policy_context": context,
        }
    return {
        "ok": True,
        "package": str(path.resolve()),
        "package_sha256": _sha256(path.read_bytes()),
        "schema_version": 1,
        "legacy": True,
        "signature_valid": False,
        "trust": trust,
        "subject": {
            "kind": "run",
            "run_id": bundle["run_id"],
            "source_commit": (
                bundle.get("git", {}).get("head")
                if isinstance(bundle.get("git"), dict)
                else None
            ),
            "result_ok": bundle.get("ok"),
        },
        "files": 1,
    }


def inspect_evidence(path: Path) -> dict[str, object]:
    if zipfile.is_zipfile(path):
        return inspect_package(path)
    bundle = _legacy_bundle(path)
    return {
        "ok": True,
        "package": str(path.resolve()),
        "schema_version": 1,
        "legacy": True,
        "trust_level": "local",
        "run_id": bundle["run_id"],
        "executions": len(bundle["executions"]),
        "signature_checked": False,
    }
