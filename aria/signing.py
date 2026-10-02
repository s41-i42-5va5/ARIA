from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from aria.errors import ConfigurationError, WorkflowError
from aria.io import atomic_write_bytes


def _ed25519() -> tuple[object, object, object, object]:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as error:
        raise ConfigurationError(
            "Ed25519 support requires the ARIA cryptography dependency"
        ) from error
    return serialization, Ed25519PrivateKey, Ed25519PublicKey, InvalidSignature


def public_key_id(public_key_bytes: bytes) -> str:
    return f"ed25519:{hashlib.sha256(public_key_bytes).hexdigest()[:32]}"


def generate_private_key() -> object:
    _serialization, private_type, _public_type, _invalid = _ed25519()
    return private_type.generate()


def private_key_bytes(private_key: object) -> bytes:
    serialization, private_type, _public_type, _invalid = _ed25519()
    if not isinstance(private_key, private_type):
        raise ConfigurationError("Signing key must be an Ed25519 private key")
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_private_key_bytes(content: bytes) -> object:
    serialization, private_type, _public_type, _invalid = _ed25519()
    try:
        key = serialization.load_pem_private_key(content, password=None)
    except (ValueError, TypeError) as error:
        raise ConfigurationError("Private signing key bytes are unreadable") from error
    if not isinstance(key, private_type):
        raise ConfigurationError("Signing key must be an Ed25519 private key")
    return key


def generate_keypair(private_path: Path, public_path: Path) -> dict[str, object]:
    serialization, _private_type, _public_type, _invalid = _ed25519()
    if private_path.resolve(strict=False) == public_path.resolve(strict=False):
        raise ConfigurationError("Private and public key paths must be different")
    if private_path.exists() or public_path.exists():
        raise ConfigurationError("Refusing to overwrite an existing signing key")
    private_key = generate_private_key()
    public_key = private_key.public_key()
    private_pem = private_key_bytes(private_key)
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    atomic_write_bytes(private_path, private_pem)
    atomic_write_bytes(public_path, public_pem)
    return {
        "ok": True,
        "algorithm": "Ed25519",
        "key_id": public_key_id(public_raw),
        "private_key": str(private_path.resolve()),
        "public_key": str(public_path.resolve()),
    }


def load_private_key(path: Path) -> object:
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(f"Private signing key is unreadable: {path}") from error
    return load_private_key_bytes(content)


def public_key_raw(private_key: object) -> bytes:
    serialization, private_type, _public_type, _invalid = _ed25519()
    if not isinstance(private_key, private_type):
        raise ConfigurationError("Signing key must be an Ed25519 private key")
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def sign_bytes(content: bytes, private_key_path: Path) -> dict[str, str]:
    key = load_private_key(private_key_path)
    return sign_bytes_with_key(content, key)


def sign_bytes_with_key(content: bytes, private_key: object) -> dict[str, str]:
    key = private_key
    public_raw = public_key_raw(key)
    return {
        "algorithm": "Ed25519",
        "key_id": public_key_id(public_raw),
        "public_key": base64.b64encode(public_raw).decode("ascii"),
        "signature": base64.b64encode(key.sign(content)).decode("ascii"),
    }


def verify_bytes(
    content: bytes,
    *,
    public_key_b64: object,
    signature_b64: object,
    key_id: object,
) -> str:
    _serialization, _private_type, public_type, invalid_signature = _ed25519()
    if not all(
        isinstance(value, str) and value
        for value in (public_key_b64, signature_b64, key_id)
    ):
        raise WorkflowError("Signature metadata is incomplete")
    try:
        public_raw = base64.b64decode(public_key_b64, validate=True)
        signature = base64.b64decode(signature_b64, validate=True)
        public_key = public_type.from_public_bytes(public_raw)
    except (ValueError, TypeError) as error:
        raise WorkflowError("Signature metadata is not valid Ed25519 data") from error
    actual_key_id = public_key_id(public_raw)
    if key_id != actual_key_id:
        raise WorkflowError("Signature key id does not match the public key")
    try:
        public_key.verify(signature, content)
    except invalid_signature as error:
        raise WorkflowError("Evidence signature is invalid") from error
    return actual_key_id
