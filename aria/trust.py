from __future__ import annotations

import base64
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from aria.assurance import TEST_CLASSES
from aria.errors import ConfigurationError, WorkflowError
from aria.signing import public_key_id
from aria.team import ACTOR_ROLES

TRUST_LEVELS = {"local": 0, "signed": 1, "ci-signed": 2}
ACTOR_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CLOCK_SKEW = timedelta(minutes=5)


def _timestamp(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ConfigurationError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ConfigurationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def load_trust_policy(path: Path) -> dict[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigurationError(f"Trust policy is unreadable: {path}") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ConfigurationError("Trust policy schema_version must be 1")
    unknown_root = sorted(set(raw) - {"schema_version", "keys", "policies"})
    if unknown_root:
        raise ConfigurationError(
            f"Trust policy contains unknown fields: {unknown_root}"
        )
    keys = raw.get("keys")
    policies = raw.get("policies")
    if not isinstance(keys, list) or not isinstance(policies, dict):
        raise ConfigurationError("Trust policy requires keys and policies")
    indexed: dict[str, dict[str, object]] = {}
    for index, item in enumerate(keys):
        if not isinstance(item, dict):
            raise ConfigurationError(f"Trust key {index} must be a mapping")
        unknown_key_fields = sorted(
            set(item)
            - {
                "id",
                "public_key",
                "actor_id",
                "status",
                "not_before",
                "expires_at",
            }
        )
        if unknown_key_fields:
            raise ConfigurationError(
                f"Trust key {index} contains unknown fields: {unknown_key_fields}"
            )
        key_id = item.get("id")
        public_key = item.get("public_key")
        actor_id = item.get("actor_id")
        status = item.get("status", "trusted")
        if (
            not isinstance(key_id, str)
            or not isinstance(public_key, str)
            or not isinstance(actor_id, str)
            or ACTOR_ID_RE.fullmatch(actor_id) is None
            or status not in {"trusted", "revoked"}
        ):
            raise ConfigurationError(f"Trust key {index} is malformed")
        try:
            public_raw = base64.b64decode(public_key, validate=True)
        except (ValueError, TypeError) as error:
            raise ConfigurationError(
                f"Trust key {key_id!r} public_key is not valid base64"
            ) from error
        if len(public_raw) != 32 or public_key_id(public_raw) != key_id:
            raise ConfigurationError(f"Trust key {key_id!r} identity mismatch")
        if key_id in indexed:
            raise ConfigurationError(f"Duplicate trust key: {key_id}")
        not_before = _timestamp(item.get("not_before"), f"Trust key {key_id}.not_before")
        expires_at = _timestamp(item.get("expires_at"), f"Trust key {key_id}.expires_at")
        if (
            not_before is not None
            and expires_at is not None
            and expires_at <= not_before
        ):
            raise ConfigurationError(
                f"Trust key {key_id!r} expires_at must follow not_before"
            )
        indexed[key_id] = item
    normalized_policies: dict[str, dict[str, object]] = {}
    for name, item in policies.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise ConfigurationError("Trust policies must be named mappings")
        unknown_policy_fields = sorted(
            set(item)
            - {
                "minimum_trust_level",
                "trusted_keys",
                "required_assurance_classes",
                "allowed_actor_roles",
                "required_approvals",
            }
        )
        if unknown_policy_fields:
            raise ConfigurationError(
                f"Trust policy {name!r} contains unknown fields: "
                f"{unknown_policy_fields}"
            )
        minimum = item.get("minimum_trust_level")
        trusted_keys = item.get("trusted_keys", [])
        if minimum not in TRUST_LEVELS or not isinstance(trusted_keys, list):
            raise ConfigurationError(f"Trust policy {name!r} is malformed")
        if not all(isinstance(value, str) for value in trusted_keys):
            raise ConfigurationError(
                f"Trust policy {name!r} trusted_keys must be strings"
            )
        if len(trusted_keys) != len(set(trusted_keys)):
            raise ConfigurationError(f"Trust policy {name!r} contains duplicate keys")
        required_classes = item.get("required_assurance_classes", [])
        allowed_roles = item.get("allowed_actor_roles", [])
        required_approvals = item.get("required_approvals", 0)
        if (
            not isinstance(required_classes, list)
            or not all(
                isinstance(value, str) and value in TEST_CLASSES
                for value in required_classes
            )
            or len(required_classes) != len(set(required_classes))
        ):
            raise ConfigurationError(
                f"Trust policy {name!r} required_assurance_classes is malformed"
            )
        if (
            not isinstance(allowed_roles, list)
            or not all(
                isinstance(value, str) and value in ACTOR_ROLES
                for value in allowed_roles
            )
            or len(allowed_roles) != len(set(allowed_roles))
        ):
            raise ConfigurationError(
                f"Trust policy {name!r} allowed_actor_roles is malformed"
            )
        if (
            isinstance(required_approvals, bool)
            or not isinstance(required_approvals, int)
            or required_approvals < 0
        ):
            raise ConfigurationError(
                f"Trust policy {name!r} required_approvals is malformed"
            )
        unknown = sorted(set(trusted_keys) - set(indexed))
        if unknown:
            raise ConfigurationError(
                f"Trust policy {name!r} references unknown keys: {unknown}"
            )
        normalized_policies[name] = item
    return {
        "schema_version": 1,
        "keys": indexed,
        "policies": normalized_policies,
    }


def evaluate_trust(
    *,
    policy: dict[str, object],
    policy_name: str,
    key_id: str,
    public_key_b64: str,
    actor_id: str | None,
    trust_level: str,
    signed_at: datetime,
    assurance_classes: list[object] | None = None,
    actor_roles: list[object] | None = None,
    approval_count: int | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    policies = policy["policies"]
    keys = policy["keys"]
    assert isinstance(policies, dict) and isinstance(keys, dict)
    selected = policies.get(policy_name)
    if not isinstance(selected, dict):
        raise WorkflowError(f"Unknown trust policy: {policy_name}")
    minimum = selected["minimum_trust_level"]
    if (
        trust_level not in TRUST_LEVELS
        or TRUST_LEVELS[trust_level] < TRUST_LEVELS[str(minimum)]
    ):
        raise WorkflowError(
            f"Evidence trust level {trust_level!r} is below required {minimum!r}"
        )
    key = keys.get(key_id)
    if not isinstance(key, dict):
        raise WorkflowError(f"Evidence key is not trusted: {key_id}")
    trusted_keys = selected.get("trusted_keys", [])
    if key_id not in trusted_keys:
        raise WorkflowError(f"Evidence key is not allowed by policy: {key_id}")
    if key.get("public_key") != public_key_b64:
        raise WorkflowError("Trusted public key does not match evidence key")
    if actor_id is None or key.get("actor_id") != actor_id:
        raise WorkflowError("Evidence actor does not match the trusted key owner")
    if key.get("status", "trusted") != "trusted":
        raise WorkflowError(f"Evidence key is revoked: {key_id}")
    reference = (now or datetime.now(UTC)).astimezone(UTC)
    not_before = _timestamp(key.get("not_before"), f"Trust key {key_id}.not_before")
    expires_at = _timestamp(key.get("expires_at"), f"Trust key {key_id}.expires_at")
    if not_before is not None and signed_at < not_before:
        raise WorkflowError(f"Evidence predates trusted key validity: {key_id}")
    if not_before is not None and reference < not_before:
        raise WorkflowError(f"Evidence key is not yet valid: {key_id}")
    if signed_at > reference + CLOCK_SKEW:
        raise WorkflowError("Evidence signature timestamp is in the future")
    if expires_at is not None and (signed_at >= expires_at or reference >= expires_at):
        raise WorkflowError(f"Evidence key is expired: {key_id}")
    required_classes = set(selected.get("required_assurance_classes", []))
    passed_classes = {
        str(value) for value in (assurance_classes or []) if isinstance(value, str)
    }
    missing_classes = sorted(required_classes - passed_classes)
    if missing_classes:
        raise WorkflowError(
            f"Evidence lacks required assurance classes: {missing_classes}"
        )
    allowed_roles = set(selected.get("allowed_actor_roles", []))
    if allowed_roles:
        if actor_roles is None:
            raise WorkflowError(
                f"Trust policy {policy_name!r} requires actor role context"
            )
        actual_roles = {
            str(value) for value in actor_roles if isinstance(value, str)
        }
        if not allowed_roles.intersection(actual_roles):
            raise WorkflowError(
                f"Actor roles {sorted(actual_roles)} are not allowed by policy "
                f"{policy_name!r}"
            )
    required_approvals = int(selected.get("required_approvals", 0))
    if required_approvals:
        if approval_count is None:
            raise WorkflowError(
                f"Trust policy {policy_name!r} requires approval context"
            )
        if approval_count < required_approvals:
            raise WorkflowError(
                f"Policy {policy_name!r} requires {required_approvals} approvals"
            )
    return {
        "ok": True,
        "policy": policy_name,
        "key_id": key_id,
        "trust_level": trust_level,
        "minimum_trust_level": minimum,
        "required_assurance_classes": sorted(required_classes),
        "allowed_actor_roles": sorted(allowed_roles),
        "approval_count": approval_count,
        "required_approvals": required_approvals,
    }


def evaluate_policy_context(
    *,
    policy: dict[str, object],
    policy_name: str,
    actor_roles: list[object],
    approval_count: int,
) -> dict[str, object]:
    policies = policy["policies"]
    assert isinstance(policies, dict)
    selected = policies.get(policy_name)
    if not isinstance(selected, dict):
        raise WorkflowError(f"Unknown trust policy: {policy_name}")
    allowed_roles = set(selected.get("allowed_actor_roles", []))
    actual_roles = {
        str(value) for value in actor_roles if isinstance(value, str)
    }
    if allowed_roles and not allowed_roles.intersection(actual_roles):
        raise WorkflowError(
            f"Actor roles {sorted(actual_roles)} are not allowed by policy "
            f"{policy_name!r}"
        )
    required_approvals = int(selected.get("required_approvals", 0))
    if approval_count < required_approvals:
        raise WorkflowError(
            f"Policy {policy_name!r} requires {required_approvals} approvals"
        )
    return {
        "ok": True,
        "policy": policy_name,
        "actor_roles": sorted(actual_roles),
        "allowed_actor_roles": sorted(allowed_roles),
        "approval_count": approval_count,
        "required_approvals": required_approvals,
    }
