from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aria.errors import ConfigurationError, WorkflowError
from aria.identity import (
    IDENTITY_ID_RE,
    load_identity,
    public_identity,
    sign_with_identity,
    verify_enrollment_request,
)
from aria.io import atomic_write_bytes, exclusive_lock, json_bytes
from aria.signing import public_key_id, verify_bytes
from aria.team import load_team
from aria.trust import evaluate_trust, load_trust_policy

ACCESS_PERMISSIONS = {
    "project.read",
    "run.create",
    "run.advance",
    "verify.execute",
    "evidence.sign",
    "team.claim",
    "team.manage",
    "backlog.read",
    "backlog.write",
    "backlog.assign",
    "backlog.claim",
    "backlog.close",
    "release.manage",
    "access.manage",
}
ADMIN_PERMISSIONS = sorted(ACCESS_PERMISSIONS)
SCOPE_RE = re.compile(r"[A-Za-z0-9*?._/@:+-]{1,128}")


def _stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _access_relative(project: object) -> str:
    files = getattr(project, "files", None)
    relative = getattr(files, "access", None) if files is not None else None
    return relative if isinstance(relative, str) and relative else "ACCESS.yaml"


def _access_path(project: object) -> Path:
    return Path(project.docs_root) / _access_relative(project)


def _history_path(project: object) -> Path:
    return Path(project.docs_root) / "ACCESS_HISTORY.jsonl"


def access_template(project_id: str) -> bytes:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "project_id": project_id,
            "revision": 0,
            "status": "bootstrap",
            "updated_at": None,
            "devices": [],
            "grants": [],
            "signature": None,
        },
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def _unsigned(policy: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in policy.items() if key != "signature"}


def _policy_bytes(policy: dict[str, object]) -> bytes:
    return json_bytes(_unsigned(policy))


def _read_yaml(path: Path, label: str) -> dict[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"{label} is unreadable: {path}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must be a mapping")
    return raw


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ConfigurationError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ConfigurationError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _validate_scope(values: object, label: str) -> list[str]:
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) and SCOPE_RE.fullmatch(value) for value in values)
        or len(values) != len(set(values))
    ):
        raise ConfigurationError(f"{label} must be a unique non-empty scope list")
    return list(values)


def _validate_policy_shape(
    project: object, raw: dict[str, object]
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    expected = {
        "schema_version",
        "project_id",
        "revision",
        "status",
        "updated_at",
        "devices",
        "grants",
        "signature",
    }
    if (
        set(raw) != expected
        or raw.get("schema_version") != 1
        or raw.get("project_id") != project.project_id
        or isinstance(raw.get("revision"), bool)
        or not isinstance(raw.get("revision"), int)
        or int(raw["revision"]) < 0
        or raw.get("status") not in {"bootstrap", "active"}
        or not isinstance(raw.get("devices"), list)
        or not isinstance(raw.get("grants"), list)
    ):
        raise ConfigurationError("ACCESS.yaml schema or project identity is invalid")
    if raw["status"] == "bootstrap":
        if (
            raw["revision"] != 0
            or raw["updated_at"] is not None
            or raw["devices"]
            or raw["grants"]
            or raw["signature"] is not None
        ):
            raise ConfigurationError("ACCESS.yaml bootstrap state is not empty")
        return {}, {}
    if raw["revision"] < 1:
        raise ConfigurationError("Active ACCESS.yaml requires a positive revision")
    _parse_time(raw.get("updated_at"), "ACCESS.yaml.updated_at")

    actors = load_team(project)
    devices: dict[str, dict[str, object]] = {}
    actor_devices: set[tuple[str, str]] = set()
    for index, row in enumerate(raw["devices"]):
        if not isinstance(row, dict) or set(row) != {
            "actor_id",
            "device_id",
            "key_id",
            "public_key",
            "status",
            "enrolled_at",
        }:
            raise ConfigurationError(f"Access device {index} schema is invalid")
        actor_id = row.get("actor_id")
        device_id = row.get("device_id")
        key_id = row.get("key_id")
        public_key = row.get("public_key")
        if (
            not isinstance(actor_id, str)
            or actor_id not in actors
            or not isinstance(device_id, str)
            or IDENTITY_ID_RE.fullmatch(device_id) is None
            or not isinstance(key_id, str)
            or not isinstance(public_key, str)
            or row.get("status") not in {"active", "revoked"}
        ):
            raise ConfigurationError(f"Access device {index} identity is invalid")
        try:
            public_raw = base64.b64decode(public_key, validate=True)
        except (ValueError, TypeError) as error:
            raise ConfigurationError(
                f"Access device {index} public key is invalid"
            ) from error
        if len(public_raw) != 32 or public_key_id(public_raw) != key_id:
            raise ConfigurationError(f"Access device {index} key binding is invalid")
        _parse_time(row.get("enrolled_at"), f"Access device {index}.enrolled_at")
        pair = (actor_id, device_id)
        if key_id in devices or pair in actor_devices:
            raise ConfigurationError("ACCESS.yaml contains duplicate device identity")
        devices[key_id] = row
        actor_devices.add(pair)

    grants: dict[str, dict[str, object]] = {}
    for index, row in enumerate(raw["grants"]):
        if not isinstance(row, dict) or set(row) != {
            "actor_id",
            "permissions",
            "versions",
            "branches",
        }:
            raise ConfigurationError(f"Access grant {index} schema is invalid")
        actor_id = row.get("actor_id")
        permissions = row.get("permissions")
        if (
            not isinstance(actor_id, str)
            or actor_id not in actors
            or actor_id in grants
            or not isinstance(permissions, list)
            or not permissions
            or not all(
                isinstance(permission, str) and permission in ACCESS_PERMISSIONS
                for permission in permissions
            )
            or len(permissions) != len(set(permissions))
        ):
            raise ConfigurationError(f"Access grant {index} is invalid")
        _validate_scope(row.get("versions"), f"Access grant {index}.versions")
        _validate_scope(row.get("branches"), f"Access grant {index}.branches")
        grants[actor_id] = row
    return devices, grants


def load_access_policy(project: object) -> dict[str, object]:
    path = _access_path(project)
    raw = _read_yaml(path, "ACCESS.yaml")
    devices, grants = _validate_policy_shape(project, raw)
    if raw["status"] == "bootstrap":
        return {**raw, "_devices": devices, "_grants": grants}
    signature = raw.get("signature")
    if not isinstance(signature, dict) or set(signature) != {
        "algorithm",
        "key_id",
        "public_key",
        "signature",
        "actor_id",
        "device_id",
    }:
        raise ConfigurationError("ACCESS.yaml signature schema is invalid")
    if signature.get("algorithm") != "Ed25519":
        raise ConfigurationError("ACCESS.yaml requires an Ed25519 signature")
    key_id = verify_bytes(
        _policy_bytes(raw),
        public_key_b64=signature.get("public_key"),
        signature_b64=signature.get("signature"),
        key_id=signature.get("key_id"),
    )
    signer = devices.get(key_id)
    if (
        not isinstance(signer, dict)
        or signer.get("actor_id") != signature.get("actor_id")
        or signer.get("device_id") != signature.get("device_id")
        or signer.get("status") != "active"
        or signer.get("public_key") != signature.get("public_key")
    ):
        raise WorkflowError("ACCESS.yaml signer is not an active policy device")
    grant = grants.get(str(signature.get("actor_id")))
    if not isinstance(grant, dict) or "access.manage" not in grant["permissions"]:
        raise WorkflowError("ACCESS.yaml signer lacks access.manage")
    trust_relative = getattr(getattr(project, "files", None), "trust", None)
    if not isinstance(trust_relative, str) or not trust_relative:
        raise ConfigurationError("Project has no TRUST.yaml pointer for access policy")
    trust = load_trust_policy(Path(project.docs_root) / trust_relative)
    actors = load_team(project)
    actor = actors.get(str(signature.get("actor_id")))
    if not isinstance(actor, dict):
        raise WorkflowError("ACCESS.yaml signer is absent from ARIA_TEAM.yaml")
    evaluate_trust(
        policy=trust,
        policy_name="access",
        key_id=key_id,
        public_key_b64=str(signature["public_key"]),
        actor_id=str(signature["actor_id"]),
        trust_level="signed",
        signed_at=_parse_time(raw["updated_at"], "ACCESS.yaml.updated_at"),
        actor_roles=list(actor["roles"]),
        approval_count=0,
    )
    return {**raw, "_devices": devices, "_grants": grants}


def _render(policy: dict[str, object]) -> bytes:
    portable = {
        key: value for key, value in policy.items() if not str(key).startswith("_")
    }
    return yaml.safe_dump(
        portable, allow_unicode=True, sort_keys=False
    ).encode("utf-8")


def _sign_policy(
    policy: dict[str, object], identity: dict[str, object]
) -> dict[str, object]:
    unsigned = {**_unsigned(policy), "signature": None}
    signature = sign_with_identity(identity, _policy_bytes(unsigned))
    unsigned["signature"] = signature
    return unsigned


def _trust_raw(project: object) -> tuple[Path, dict[str, object]]:
    relative = getattr(getattr(project, "files", None), "trust", None)
    if not isinstance(relative, str) or not relative:
        raise ConfigurationError("Project has no TRUST.yaml pointer")
    path = Path(project.docs_root) / relative
    return path, _read_yaml(path, "TRUST.yaml")


def _ensure_access_trust(
    project: object,
    *,
    identity: dict[str, object],
    signed_at: str,
) -> tuple[Path, bytes, bytes]:
    path, raw = _trust_raw(project)
    before = path.read_bytes()
    if (
        raw.get("schema_version") != 1
        or not isinstance(raw.get("keys"), list)
        or not isinstance(raw.get("policies"), dict)
    ):
        raise ConfigurationError("TRUST.yaml cannot anchor access bootstrap")
    key_id = str(identity["key_id"])
    matching = [
        row
        for row in raw["keys"]
        if isinstance(row, dict) and row.get("id") == key_id
    ]
    entry = {
        "id": key_id,
        "public_key": identity["public_key"],
        "actor_id": identity["actor_id"],
        "status": "trusted",
        "not_before": signed_at,
    }
    if matching and matching[0] != entry:
        raise WorkflowError("TRUST.yaml already contains a different bootstrap key")
    if not matching:
        raw["keys"].append(entry)
    raw["policies"]["access"] = {
        "minimum_trust_level": "signed",
        "trusted_keys": [key_id],
        "required_assurance_classes": [],
        "allowed_actor_roles": ["maintainer", "release-manager"],
        "required_approvals": 0,
    }
    after = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8")
    return path, before, after


def _event_bytes(event: dict[str, object]) -> bytes:
    return json_bytes(
        {
            key: value
            for key, value in event.items()
            if key not in {"event_sha256", "signature"}
        }
    )


def _append_event(
    project: object,
    *,
    event_type: str,
    policy: dict[str, object],
    identity: dict[str, object],
) -> dict[str, object]:
    path = _history_path(project)
    rows: list[dict[str, object]] = []
    if path.is_file():
        try:
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkflowError("ACCESS_HISTORY.jsonl is unreadable") from error
    previous = rows[-1].get("event_sha256") if rows else None
    event: dict[str, object] = {
        "schema_version": 1,
        "sequence": len(rows) + 1,
        "timestamp": policy["updated_at"],
        "type": event_type,
        "project_id": project.project_id,
        "actor_id": identity["actor_id"],
        "device_id": identity["device_id"],
        "key_id": identity["key_id"],
        "policy_revision": policy["revision"],
        "policy_sha256": hashlib.sha256(_policy_bytes(policy)).hexdigest(),
        "previous_event_sha256": previous,
    }
    event["event_sha256"] = hashlib.sha256(_event_bytes(event)).hexdigest()
    event["signature"] = sign_with_identity(
        identity,
        json_bytes({key: value for key, value in event.items() if key != "signature"}),
    )
    rows.append(event)
    content = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    ).encode("utf-8")
    atomic_write_bytes(path, content)
    return event


def bootstrap_access(
    project: object,
    *,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    lock = Path(project.runtime_root) / "access" / "policy.lock"
    with exclusive_lock(lock):
        current = load_access_policy(project)
        if current["status"] != "bootstrap":
            raise WorkflowError("Project access policy is already active")
        identity = load_identity(
            Path(project.registry_path).parent,
            actor_id=actor_id,
            device_id=device_id,
        )
        actors = load_team(project)
        actor = actors.get(str(identity["actor_id"]))
        if not isinstance(actor, dict) or not {
            "maintainer",
            "release-manager",
        }.intersection(set(actor["roles"])):
            raise WorkflowError(
                "Only an existing maintainer or release-manager may bootstrap access"
            )
        timestamp = _stamp()
        trust_path, trust_before, trust_after = _ensure_access_trust(
            project, identity=identity, signed_at=timestamp
        )
        public = public_identity(identity)
        policy: dict[str, object] = {
            "schema_version": 1,
            "project_id": project.project_id,
            "revision": 1,
            "status": "active",
            "updated_at": timestamp,
            "devices": [
                {
                    "actor_id": public["actor_id"],
                    "device_id": public["device_id"],
                    "key_id": public["key_id"],
                    "public_key": public["public_key"],
                    "status": "active",
                    "enrolled_at": timestamp,
                }
            ],
            "grants": [
                {
                    "actor_id": public["actor_id"],
                    "permissions": ADMIN_PERMISSIONS,
                    "versions": ["*"],
                    "branches": ["*"],
                }
            ],
            "signature": None,
        }
        signed = _sign_policy(policy, identity)
        access_path = _access_path(project)
        access_before = access_path.read_bytes()
        try:
            atomic_write_bytes(trust_path, trust_after)
            load_trust_policy(trust_path)
            atomic_write_bytes(access_path, _render(signed))
            loaded = load_access_policy(project)
            event = _append_event(
                project,
                event_type="access_bootstrapped",
                policy=loaded,
                identity=identity,
            )
        except BaseException:
            atomic_write_bytes(trust_path, trust_before)
            atomic_write_bytes(access_path, access_before)
            raise
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": loaded["revision"],
            "actor_id": identity["actor_id"],
            "device_id": identity["device_id"],
            "key_id": identity["key_id"],
            "event_sha256": event["event_sha256"],
        }


def _matches_scope(patterns: list[object], value: str | None) -> bool:
    candidate = value if value is not None else "*"
    return any(
        isinstance(pattern, str) and fnmatch.fnmatchcase(candidate, pattern)
        for pattern in patterns
    )


def authorize_access(
    project: object,
    *,
    permission: str,
    actor_id: str | None = None,
    device_id: str | None = None,
    version: str | None = None,
    branch: str | None = None,
) -> dict[str, object]:
    if permission not in ACCESS_PERMISSIONS:
        raise ConfigurationError(f"Unknown ARIA access permission: {permission}")
    policy = load_access_policy(project)
    if policy["status"] != "active":
        raise WorkflowError("Project access bootstrap is incomplete")
    identity = load_identity(
        Path(project.registry_path).parent,
        actor_id=actor_id,
        device_id=device_id,
    )
    device = policy["_devices"].get(str(identity["key_id"]))
    if (
        not isinstance(device, dict)
        or device.get("actor_id") != identity.get("actor_id")
        or device.get("device_id") != identity.get("device_id")
        or device.get("status") != "active"
        or device.get("public_key") != identity.get("public_key")
    ):
        raise WorkflowError("Local ARIA identity is not an active project device")
    grant = policy["_grants"].get(str(identity["actor_id"]))
    if (
        not isinstance(grant, dict)
        or permission not in grant["permissions"]
        or not _matches_scope(grant["versions"], version)
        or not _matches_scope(grant["branches"], branch)
    ):
        raise WorkflowError(
            f"Actor {identity['actor_id']!r} is not allowed {permission!r} "
            f"for version={version!r}, branch={branch!r}"
        )
    return {
        "ok": True,
        "permission": permission,
        "actor_id": identity["actor_id"],
        "device_id": identity["device_id"],
        "key_id": identity["key_id"],
        "policy_revision": policy["revision"],
        "identity": identity,
        "policy": policy,
    }


def _load_request(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Enrollment request is unreadable: {path}") from error
    return verify_enrollment_request(payload)


def grant_access(
    project: object,
    *,
    request_path: Path,
    permissions: list[str],
    versions: list[str],
    branches: list[str],
    expected_revision: int,
    admin_actor_id: str | None = None,
    admin_device_id: str | None = None,
) -> dict[str, object]:
    unknown = sorted(set(permissions) - ACCESS_PERMISSIONS)
    if not permissions or unknown or len(permissions) != len(set(permissions)):
        raise ConfigurationError(f"Invalid access permissions: unknown={unknown}")
    _validate_scope(versions, "versions")
    _validate_scope(branches, "branches")
    lock = Path(project.runtime_root) / "access" / "policy.lock"
    with exclusive_lock(lock):
        authorization = authorize_access(
            project,
            permission="access.manage",
            actor_id=admin_actor_id,
            device_id=admin_device_id,
        )
        current = authorization["policy"]
        verify_access_audit(project)
        if current["revision"] != expected_revision:
            raise WorkflowError(
                f"Stale access revision: expected {expected_revision}, "
                f"actual {current['revision']}"
            )
        request = _load_request(request_path)
        actors = load_team(project)
        target = str(request["actor_id"])
        if target not in actors:
            raise WorkflowError(
                f"Enrollment actor is absent from ARIA_TEAM.yaml: {target}"
            )
        pair = (request["actor_id"], request["device_id"])
        for device in current["devices"]:
            if (
                device["key_id"] == request["key_id"]
                or (device["actor_id"], device["device_id"]) == pair
            ):
                raise WorkflowError("Enrollment device already exists in access policy")
        timestamp = _stamp()
        devices = [
            *current["devices"],
            {
                "actor_id": request["actor_id"],
                "device_id": request["device_id"],
                "key_id": request["key_id"],
                "public_key": request["public_key"],
                "status": "active",
                "enrolled_at": timestamp,
            },
        ]
        grants = [
            row for row in current["grants"] if row["actor_id"] != request["actor_id"]
        ]
        grants.append(
            {
                "actor_id": request["actor_id"],
                "permissions": permissions,
                "versions": versions,
                "branches": branches,
            }
        )
        updated = _sign_policy(
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "revision": expected_revision + 1,
                "status": "active",
                "updated_at": timestamp,
                "devices": devices,
                "grants": grants,
                "signature": None,
            },
            authorization["identity"],
        )
        access_path = _access_path(project)
        history_path = _history_path(project)
        access_before = access_path.read_bytes()
        history_before = (
            history_path.read_bytes() if history_path.is_file() else None
        )
        try:
            atomic_write_bytes(access_path, _render(updated))
            loaded = load_access_policy(project)
            event = _append_event(
                project,
                event_type="access_granted",
                policy=loaded,
                identity=authorization["identity"],
            )
            verify_access_audit(project)
        except BaseException:
            atomic_write_bytes(access_path, access_before)
            if history_before is None:
                if history_path.exists():
                    history_path.unlink()
            else:
                atomic_write_bytes(history_path, history_before)
            raise
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": loaded["revision"],
            "actor_id": request["actor_id"],
            "device_id": request["device_id"],
            "permissions": permissions,
            "versions": versions,
            "branches": branches,
            "event_sha256": event["event_sha256"],
        }


def revoke_access(
    project: object,
    *,
    actor_id: str,
    device_id: str | None,
    expected_revision: int,
    admin_actor_id: str | None = None,
    admin_device_id: str | None = None,
) -> dict[str, object]:
    lock = Path(project.runtime_root) / "access" / "policy.lock"
    with exclusive_lock(lock):
        authorization = authorize_access(
            project,
            permission="access.manage",
            actor_id=admin_actor_id,
            device_id=admin_device_id,
        )
        current = authorization["policy"]
        verify_access_audit(project)
        if current["revision"] != expected_revision:
            raise WorkflowError(
                f"Stale access revision: expected {expected_revision}, "
                f"actual {current['revision']}"
            )
        changed = 0
        devices: list[dict[str, object]] = []
        for row in current["devices"]:
            if row["actor_id"] == actor_id and (
                device_id is None or row["device_id"] == device_id
            ):
                if row["key_id"] == authorization["key_id"]:
                    raise WorkflowError("Access administrator cannot revoke the signing device")
                if row["status"] != "revoked":
                    row = {**row, "status": "revoked"}
                    changed += 1
            devices.append(row)
        if not changed:
            raise WorkflowError("No active access device matched the revoke request")
        timestamp = _stamp()
        updated = _sign_policy(
            {
                "schema_version": 1,
                "project_id": project.project_id,
                "revision": expected_revision + 1,
                "status": "active",
                "updated_at": timestamp,
                "devices": devices,
                "grants": current["grants"],
                "signature": None,
            },
            authorization["identity"],
        )
        access_path = _access_path(project)
        history_path = _history_path(project)
        access_before = access_path.read_bytes()
        history_before = (
            history_path.read_bytes() if history_path.is_file() else None
        )
        try:
            atomic_write_bytes(access_path, _render(updated))
            loaded = load_access_policy(project)
            event = _append_event(
                project,
                event_type="access_revoked",
                policy=loaded,
                identity=authorization["identity"],
            )
            verify_access_audit(project)
        except BaseException:
            atomic_write_bytes(access_path, access_before)
            if history_before is None:
                if history_path.exists():
                    history_path.unlink()
            else:
                atomic_write_bytes(history_path, history_before)
            raise
        return {
            "ok": True,
            "project_id": project.project_id,
            "revision": loaded["revision"],
            "actor_id": actor_id,
            "device_id": device_id,
            "revoked_devices": changed,
            "event_sha256": event["event_sha256"],
        }


def access_status(project: object) -> dict[str, object]:
    policy = load_access_policy(project)
    return {
        "ok": True,
        "project_id": project.project_id,
        "status": policy["status"],
        "revision": policy["revision"],
        "updated_at": policy["updated_at"],
        "devices": policy["devices"],
        "grants": policy["grants"],
        "signature": policy["signature"],
    }


def verify_access_audit(project: object) -> dict[str, object]:
    policy = load_access_policy(project)
    path = _history_path(project)
    if not path.is_file():
        if policy["status"] == "bootstrap":
            return {"ok": True, "events": 0, "head_sha256": None}
        raise WorkflowError("Active access policy has no ACCESS_HISTORY.jsonl")
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError("ACCESS_HISTORY.jsonl is unreadable") from error
    previous = None
    devices = policy["_devices"]
    for index, event in enumerate(rows, start=1):
        if (
            not isinstance(event, dict)
            or event.get("schema_version") != 1
            or event.get("sequence") != index
            or event.get("project_id") != project.project_id
            or event.get("previous_event_sha256") != previous
            or not isinstance(event.get("signature"), dict)
        ):
            raise WorkflowError(f"Access audit event {index} identity is invalid")
        actual = hashlib.sha256(_event_bytes(event)).hexdigest()
        if event.get("event_sha256") != actual:
            raise WorkflowError(f"Access audit event {index} hash is invalid")
        device = devices.get(str(event.get("key_id")))
        if (
            not isinstance(device, dict)
            or device.get("actor_id") != event.get("actor_id")
            or device.get("device_id") != event.get("device_id")
        ):
            raise WorkflowError(f"Access audit event {index} device is unknown")
        signature = event["signature"]
        verify_bytes(
            json_bytes(
                {key: value for key, value in event.items() if key != "signature"}
            ),
            public_key_b64=device.get("public_key"),
            signature_b64=signature.get("signature"),
            key_id=signature.get("key_id"),
        )
        previous = actual
    if rows and rows[-1].get("policy_revision") != policy["revision"]:
        raise WorkflowError("Access audit head does not match policy revision")
    return {"ok": True, "events": len(rows), "head_sha256": previous}
