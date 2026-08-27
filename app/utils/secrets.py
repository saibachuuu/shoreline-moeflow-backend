"""Small application-level helpers for values that must not be stored plainly."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


ENCRYPTED_PREFIX = "fernet:"


def _fernet() -> Fernet:
    configured = str(
        current_app.config.get("ARCHIVE_API_KEY_ENCRYPTION_KEY", "") or ""
    ).strip()
    if configured:
        key = configured.encode("ascii")
    else:
        # Keep the default deployable without introducing a second mandatory
        # secret.  Deployments that need independent key rotation can provide
        # ARCHIVE_API_KEY_ENCRYPTION_KEY as a Fernet key.
        secret = str(current_app.config.get("SECRET_KEY", "") or "")
        if not secret:
            raise RuntimeError("SECRET_KEY is required to protect archive API keys")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    """Encrypt a secret, preserving already-encrypted values."""

    value = str(value or "")
    if not value:
        return ""
    if value.startswith(ENCRYPTED_PREFIX):
        return value
    return ENCRYPTED_PREFIX + _fernet().encrypt(value.encode()).decode("ascii")


def decrypt_secret(value: str) -> str:
    """Decrypt a value, accepting legacy plaintext during migration."""

    value = str(value or "")
    if not value.startswith(ENCRYPTED_PREFIX):
        return value
    payload = value[len(ENCRYPTED_PREFIX) :]
    try:
        return _fernet().decrypt(payload.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise ValueError("无法解密归档 API key") from exc


def secret_tail(value: str, length: int = 4) -> str:
    """Return only the tail used by the settings UI."""

    try:
        plaintext = decrypt_secret(value)
    except ValueError:
        return ""
    return plaintext[-length:] if plaintext else ""
