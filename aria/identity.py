from __future__ import annotations

import base64
import ctypes
import getpass
import json
import os
import re
import socket
from datetime import UTC, datetime
from pathlib import Path

from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_json, exclusive_lock, json_bytes
from aria.signing import (
    generate_private_key,
    load_private_key_bytes,
    private_key_bytes,
    public_key_id,
    public_key_raw,
    sign_bytes_with_key,
    verify_bytes,
)

IDENTITY_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_device_id(value: str | None) -> str:
    if value is None:
        value = socket.gethostname().lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    if IDENTITY_ID_RE.fullmatch(normalized) is None:
        raise ConfigurationError(f"Invalid ARIA device id: {value!r}")
    return normalized


def _identity_path(runtime_root: Path, actor_id: str, device_id: str) -> Path:
    return runtime_root / "identities" / f"{actor_id}--{device_id}.json"


def _dpapi_protect(content: bytes) -> bytes:
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    buffer = (ctypes.c_ubyte * len(content)).from_buffer_copy(content)
    source = DataBlob(len(content), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    protected = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "ARIA device identity",
        None,
        None,
        None,
        0x1,
        ctypes.byref(protected),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(protected.data, protected.size)
    finally:
        kernel32.LocalFree(protected.data)


def _dpapi_unprotect(content: bytes) -> bytes:
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    buffer = (ctypes.c_ubyte * len(content)).from_buffer_copy(content)
    source = DataBlob(len(content), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    clear = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(clear),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(clear.data, clear.size)
    finally:
        kernel32.LocalFree(clear.data)


def _protect_private_key(content: bytes) -> tuple[str, bytes]:
    if os.name == "nt":
        try:
            return "windows-dpapi-current-user", _dpapi_protect(content)
        except OSError as error:
            raise ConfigurationError(
                "Windows DPAPI could not protect the ARIA device identity"
            ) from error
    return "local-file-0600", content


def _unprotect_private_key(protection: object, content: bytes) -> bytes:
    if protection == "windows-dpapi-current-user":
        if os.name != "nt":
            raise ConfigurationError(
                "Windows-protected ARIA identity cannot be opened on this OS"
            )
        try:
            return _dpapi_unprotect(content)
        except OSError as error:
            raise ConfigurationError(
                "Windows DPAPI could not unlock the ARIA device identity"
            ) from error
    if protection == "local-file-0600":
        return content
    raise ConfigurationError(f"Unsupported ARIA identity protection: {protection!r}")


def _request_content(request: dict[str, object]) -> bytes:
    unsigned = {key: value for key, value in request.items() if key != "proof"}
    return json_bytes(unsigned)


def enroll_identity(
    runtime_root: Path,
    *,
    actor_id: str,
    device_id: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    request_path: Path | None = None,
) -> dict[str, object]:
    if IDENTITY_ID_RE.fullmatch(actor_id) is None:
        raise ConfigurationError(f"Invalid ARIA actor id: {actor_id!r}")
    device = _normalize_device_id(device_id)
    if email is not None and EMAIL_RE.fullmatch(email) is None:
        raise ConfigurationError(f"Invalid contact email: {email!r}")
    root = runtime_root.resolve(strict=False)
    path = _identity_path(root, actor_id, device)
    lock = root / "identities" / ".identity.lock"
    with exclusive_lock(lock):
        if path.exists():
            raise ConfigurationError(
                f"Refusing to overwrite ARIA identity: {actor_id}/{device}"
            )
        private_key = generate_private_key()
        public_raw = public_key_raw(private_key)
        key_id = public_key_id(public_raw)
        protection, protected = _protect_private_key(private_key_bytes(private_key))
        created_at = _stamp()
        stored = {
            "schema_version": 1,
            "kind": "aria-device-identity",
            "actor_id": actor_id,
            "device_id": device,
            "display_name": display_name or actor_id,
            "contact_email": email,
            "email_verified": False,
            "hostname": socket.gethostname(),
            "os_user": getpass.getuser(),
            "key_id": key_id,
            "public_key": base64.b64encode(public_raw).decode("ascii"),
            "protection": protection,
            "protected_private_key": base64.b64encode(protected).decode("ascii"),
            "created_at": created_at,
            "status": "active",
        }
        atomic_write_json(path, stored)
        try:
            path.chmod(0o600)
        except OSError as error:
            raise ConfigurationError(
                f"Cannot restrict ARIA identity file permissions: {path}"
            ) from error

        request: dict[str, object] = {
            "schema_version": 1,
            "kind": "aria-device-enrollment",
            "actor_id": actor_id,
            "device_id": device,
            "display_name": display_name or actor_id,
            "contact_email": email,
            "email_verified": False,
            "key_id": key_id,
            "public_key": base64.b64encode(public_raw).decode("ascii"),
            "created_at": created_at,
        }
        request["proof"] = sign_bytes_with_key(_request_content(request), private_key)
        if request_path is not None:
            if request_path.exists():
                raise ConfigurationError(
                    f"Refusing to overwrite enrollment request: {request_path}"
                )
            atomic_write_json(request_path, request)
        return {
            "ok": True,
            "actor_id": actor_id,
            "device_id": device,
            "key_id": key_id,
            "protection": protection,
            "identity_path": str(path),
            "request_path": str(request_path.resolve()) if request_path else None,
            "enrollment_request": request,
        }


def _load_identity_file(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"ARIA identity is unreadable: {path}") from error
    required = {
        "schema_version",
        "kind",
        "actor_id",
        "device_id",
        "key_id",
        "public_key",
        "protection",
        "protected_private_key",
        "status",
    }
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("kind") != "aria-device-identity"
        or set(raw) - {
            *required,
            "display_name",
            "contact_email",
            "email_verified",
            "hostname",
            "os_user",
            "created_at",
        }
        or not required.issubset(raw)
        or IDENTITY_ID_RE.fullmatch(str(raw.get("actor_id"))) is None
        or IDENTITY_ID_RE.fullmatch(str(raw.get("device_id"))) is None
        or raw.get("status") not in {"active", "revoked"}
    ):
        raise ConfigurationError(f"ARIA identity schema is invalid: {path}")
    return raw


def load_identity(
    runtime_root: Path,
    *,
    actor_id: str | None = None,
    device_id: str | None = None,
) -> dict[str, object]:
    root = runtime_root.resolve(strict=False) / "identities"
    if actor_id is not None and IDENTITY_ID_RE.fullmatch(actor_id) is None:
        raise ConfigurationError(f"Invalid ARIA actor id: {actor_id!r}")
    if device_id is not None:
        device_id = _normalize_device_id(device_id)
    candidates: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        raw = _load_identity_file(path)
        if actor_id is not None and raw.get("actor_id") != actor_id:
            continue
        if device_id is not None and raw.get("device_id") != device_id:
            continue
        if raw.get("status") == "active":
            candidates.append((path, raw))
    if not candidates:
        raise WorkflowError("No active local ARIA identity matches the request")
    if len(candidates) != 1:
        labels = [
            f"{raw['actor_id']}/{raw['device_id']}" for _path, raw in candidates
        ]
        raise WorkflowError(
            f"Multiple local ARIA identities match; select actor/device: {labels}"
        )
    path, raw = candidates[0]
    try:
        protected = base64.b64decode(
            str(raw["protected_private_key"]), validate=True
        )
        public = base64.b64decode(str(raw["public_key"]), validate=True)
    except (ValueError, TypeError) as error:
        raise ConfigurationError(f"ARIA identity key material is invalid: {path}") from error
    private_pem = _unprotect_private_key(raw["protection"], protected)
    private_key = load_private_key_bytes(private_pem)
    actual_public = public_key_raw(private_key)
    if actual_public != public or public_key_id(public) != raw["key_id"]:
        raise WorkflowError("ARIA identity public/private key binding is invalid")
    return {**raw, "_path": path, "_private_key": private_key}


def public_identity(identity: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in identity.items()
        if key
        in {
            "actor_id",
            "device_id",
            "display_name",
            "contact_email",
            "email_verified",
            "key_id",
            "public_key",
            "protection",
            "created_at",
            "status",
        }
    }


def identity_status(
    runtime_root: Path,
    *,
    actor_id: str | None = None,
) -> dict[str, object]:
    root = runtime_root.resolve(strict=False) / "identities"
    rows: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        raw = _load_identity_file(path)
        if actor_id is None or raw.get("actor_id") == actor_id:
            rows.append(public_identity(raw))
    return {"ok": True, "identities": rows, "count": len(rows)}


def sign_with_identity(
    identity: dict[str, object], content: bytes
) -> dict[str, object]:
    private_key = identity.get("_private_key")
    if private_key is None:
        raise ConfigurationError("Loaded ARIA identity has no private key")
    signature = sign_bytes_with_key(content, private_key)
    return {
        **signature,
        "actor_id": identity["actor_id"],
        "device_id": identity["device_id"],
    }


def verify_enrollment_request(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ConfigurationError("Enrollment request must be a mapping")
    allowed = {
        "schema_version",
        "kind",
        "actor_id",
        "device_id",
        "display_name",
        "contact_email",
        "email_verified",
        "key_id",
        "public_key",
        "created_at",
        "proof",
    }
    if (
        set(payload) != allowed
        or payload.get("schema_version") != 1
        or payload.get("kind") != "aria-device-enrollment"
        or IDENTITY_ID_RE.fullmatch(str(payload.get("actor_id"))) is None
        or IDENTITY_ID_RE.fullmatch(str(payload.get("device_id"))) is None
        or payload.get("email_verified") is not False
        or not isinstance(payload.get("proof"), dict)
    ):
        raise ConfigurationError("Enrollment request schema is invalid")
    proof = payload["proof"]
    assert isinstance(proof, dict)
    if proof.get("algorithm") != "Ed25519":
        raise ConfigurationError("Enrollment request requires Ed25519 proof")
    verified = verify_bytes(
        _request_content(payload),
        public_key_b64=payload.get("public_key"),
        signature_b64=proof.get("signature"),
        key_id=proof.get("key_id"),
    )
    if verified != payload.get("key_id") or proof.get("public_key") != payload.get(
        "public_key"
    ):
        raise WorkflowError("Enrollment proof does not match the requested device key")
    return dict(payload)
