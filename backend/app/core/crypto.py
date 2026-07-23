"""Small encryption helper for persisted OAuth secrets."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core import secrets
from app.core.errors import AppError, ErrorCode


def _get_fernet(provider: str) -> Fernet:
    if provider == "jira":
        key = secrets.get_jira_oauth_encryption_key()
        missing_error = ErrorCode.JIRA_OAUTH_NOT_AVAILABLE
        missing_detail = "JIRA_OAUTH_ENCRYPTION_KEY is not configured"
        invalid_detail = "JIRA_OAUTH_ENCRYPTION_KEY is invalid"
        decrypt_detail = "Stored Jira OAuth secret could not be decrypted"
    elif provider == "github":
        key = secrets.get_github_oauth_encryption_key()
        missing_error = ErrorCode.REPO_PROVIDER_NOT_AVAILABLE
        missing_detail = "GITHUB_OAUTH_ENCRYPTION_KEY is not configured"
        invalid_detail = "GITHUB_OAUTH_ENCRYPTION_KEY is invalid"
        decrypt_detail = "Stored GitHub OAuth secret could not be decrypted"
    else:  # pragma: no cover - defensive guard
        raise AppError(ErrorCode.INTERNAL, internal_detail=f"Unknown crypto provider: {provider}")

    if not key:
        raise AppError(
            missing_error,
            internal_detail=missing_detail,
        )
    try:
        return Fernet(key.encode("utf-8"))
    except ValueError as exc:  # pragma: no cover - defensive config guard
        raise AppError(
            ErrorCode.INTERNAL,
            internal_detail=invalid_detail,
        ) from exc


def encrypt_secret(value: str, *, provider: str = "jira") -> str:
    """Encrypt a secret for at-rest storage."""
    return _get_fernet(provider).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str, *, provider: str = "jira") -> str:
    """Decrypt a stored secret and register the plaintext with the redactor."""
    try:
        decrypted = _get_fernet(provider).decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        detail = (
            "Stored GitHub OAuth secret could not be decrypted"
            if provider == "github"
            else "Stored Jira OAuth secret could not be decrypted"
        )
        raise AppError(
            ErrorCode.INTERNAL,
            internal_detail=detail,
        ) from exc
    secrets.register_secret(decrypted)
    return decrypted
