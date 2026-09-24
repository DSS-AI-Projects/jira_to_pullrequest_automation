"""Small encryption helper for persisted OAuth secrets."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from app.core import secrets
from app.core.errors import AppError, ErrorCode


@dataclass(frozen=True)
class _ProviderCryptoConfig:
    get_key: Callable[[], str | None]
    missing_error: ErrorCode
    missing_detail: str
    invalid_detail: str
    label: str  # human-readable, for decrypt-failure log details ("Jira", "GitHub", ...)


# One entry per provider whose tokens this app persists at rest. Adding a new
# provider means adding a new entry here (plus its own `..._OAUTH_ENCRYPTION_KEY`
# getter in secrets.py) — never reusing another provider's key.
_PROVIDERS: dict[str, _ProviderCryptoConfig] = {
    "jira": _ProviderCryptoConfig(
        get_key=secrets.get_jira_oauth_encryption_key,
        missing_error=ErrorCode.JIRA_OAUTH_NOT_AVAILABLE,
        missing_detail="JIRA_OAUTH_ENCRYPTION_KEY is not configured",
        invalid_detail="JIRA_OAUTH_ENCRYPTION_KEY is invalid",
        label="Jira",
    ),
    "github": _ProviderCryptoConfig(
        get_key=secrets.get_github_oauth_encryption_key,
        missing_error=ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
        missing_detail="GITHUB_OAUTH_ENCRYPTION_KEY is not configured",
        invalid_detail="GITHUB_OAUTH_ENCRYPTION_KEY is invalid",
        label="GitHub",
    ),
    "gitlab": _ProviderCryptoConfig(
        get_key=secrets.get_gitlab_oauth_encryption_key,
        missing_error=ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
        missing_detail="GITLAB_OAUTH_ENCRYPTION_KEY is not configured",
        invalid_detail="GITLAB_OAUTH_ENCRYPTION_KEY is invalid",
        label="GitLab",
    ),
    "bitbucket": _ProviderCryptoConfig(
        get_key=secrets.get_bitbucket_oauth_encryption_key,
        missing_error=ErrorCode.REPO_PROVIDER_NOT_AVAILABLE,
        missing_detail="BITBUCKET_OAUTH_ENCRYPTION_KEY is not configured",
        invalid_detail="BITBUCKET_OAUTH_ENCRYPTION_KEY is invalid",
        label="Bitbucket",
    ),
}


def _get_fernet(provider: str) -> Fernet:
    config = _PROVIDERS.get(provider)
    if config is None:  # pragma: no cover - defensive guard
        raise AppError(ErrorCode.INTERNAL, internal_detail=f"Unknown crypto provider: {provider}")

    key = config.get_key()
    if not key:
        raise AppError(config.missing_error, internal_detail=config.missing_detail)
    try:
        return Fernet(key.encode("utf-8"))
    except ValueError as exc:  # pragma: no cover - defensive config guard
        raise AppError(ErrorCode.INTERNAL, internal_detail=config.invalid_detail) from exc


def encrypt_secret(value: str, *, provider: str) -> str:
    """Encrypt a secret for at-rest storage.

    `provider` is required and deliberately has no default: a real bug
    shipped from `provider` defaulting to "jira" (before this signature
    change) — `complete_github_authorization()` forgot to pass
    `provider="github"` and silently encrypted GitHub tokens under the Jira
    key, which `list_github_repositories()`'s explicit `provider="github"`
    decrypt could then never read back (see CLAUDE.md's GitHub OAuth note).
    Making the argument required turns "forgot the provider for a new
    integration" into an immediate TypeError at the call site instead of a
    silent wrong-key bug discovered only when decryption fails, weeks later.
    """
    return _get_fernet(provider).encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str, *, provider: str) -> str:
    """Decrypt a stored secret and register the plaintext with the redactor."""
    config = _PROVIDERS.get(provider)
    try:
        decrypted = _get_fernet(provider).decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        label = config.label if config is not None else provider
        raise AppError(
            ErrorCode.INTERNAL,
            internal_detail=f"Stored {label} OAuth secret could not be decrypted",
        ) from exc
    secrets.register_secret(decrypted)
    return decrypted
