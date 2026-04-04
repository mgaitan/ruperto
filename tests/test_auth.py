"""Tests for dashboard authentication helpers."""

from __future__ import annotations

import re

from ruperto.auth import hash_password, normalize_email, verify_password


def test_password_hashing_and_verification_round_trip():
    """Passwords round-trip through the PBKDF2 helper."""
    encoded = hash_password("super-secret", salt="fixed-salt")

    assert encoded.startswith("pbkdf2_sha256$")
    assert verify_password("super-secret", encoded) is True
    assert verify_password("wrong", encoded) is False


def test_verify_password_rejects_malformed_hashes():
    """Malformed encodings fail closed."""
    assert verify_password("secret", "not-a-valid-hash") is False


def test_verify_password_rejects_unknown_algorithm():
    """Unknown password algorithms fail closed."""
    encoded = hash_password("super-secret", salt="fixed-salt")
    invalid = re.sub("^pbkdf2_sha256", "sha1", encoded)

    assert verify_password("super-secret", invalid) is False


def test_normalize_email_trims_and_lowercases():
    """Email lookup should be case-insensitive and stable."""
    assert normalize_email("  Owner@Example.COM ") == "owner@example.com"
