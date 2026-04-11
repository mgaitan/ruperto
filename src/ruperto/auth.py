"""Authentication helpers for the staff dashboard."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
PASSWORD_RESET_TOKEN_BYTES = 24
PASSWORD_RESET_TOKEN_TTL = timedelta(hours=2)


def normalize_email(value: str) -> str:
    """Normalize an email address for stable lookup and storage."""
    return value.strip().lower()


def hash_password(password: str, *, salt: str | None = None) -> str:
    """Hash a plain-text password using PBKDF2-SHA256."""
    resolved_salt = salt or secrets.token_hex(SALT_BYTES)
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        resolved_salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    ).hex()
    return f"{PBKDF2_ALGORITHM}${PBKDF2_ITERATIONS}${resolved_salt}${derived_key}"


def verify_password(password: str, encoded_hash: str) -> bool:
    """Verify a plain-text password against the encoded PBKDF2 hash."""
    try:
        algorithm, iterations_text, salt, expected_hash = encoded_hash.split("$", maxsplit=3)
    except ValueError:
        return False
    if algorithm != PBKDF2_ALGORITHM:
        return False
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        int(iterations_text),
    ).hex()
    return hmac.compare_digest(derived_key, expected_hash)


def create_password_reset_token() -> str:
    """Return one random opaque token suitable for password-reset links."""
    return secrets.token_urlsafe(PASSWORD_RESET_TOKEN_BYTES)


def hash_password_reset_token(token: str) -> str:
    """Hash one reset token before persisting it in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
