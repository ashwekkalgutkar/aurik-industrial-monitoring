"""Constant-time API key authentication and role-based scope enforcement."""

import hmac
from collections.abc import Callable
from typing import Literal

from fastapi import Header

from app.core.config import get_settings
from app.core.errors import AuthenticationError, AuthorizationError

ScopeType = Literal["ingest", "read", "admin"]


def validate_api_key_scope(
    provided_key: str | None,
    required_scope: ScopeType,
) -> None:
    """Validate incoming API key against configured scoped keys using constant-time comparison.

    Rules:
    - Missing header -> HTTP 401 UNAUTHENTICATED
    - Invalid/unrecognized key -> HTTP 401 UNAUTHENTICATED
    - Valid key but wrong scope (e.g. read key on ingest endpoint) -> HTTP 403 FORBIDDEN
    - Admin key grants access to all scopes.
    """
    if not provided_key or not provided_key.strip():
        raise AuthenticationError("Missing X-API-Key header.")

    settings = get_settings()
    key_clean = provided_key.strip()

    is_admin = hmac.compare_digest(key_clean, settings.API_KEY_ADMIN)
    if is_admin:
        return  # Admin key satisfies all scopes

    is_ingest = hmac.compare_digest(key_clean, settings.API_KEY_INGEST)
    is_read = hmac.compare_digest(key_clean, settings.API_KEY_READ)

    if required_scope == "ingest":
        if is_ingest:
            return
        if is_read:
            raise AuthorizationError("Read key cannot access ingestion endpoints.")
        raise AuthenticationError("Invalid API key.")

    if required_scope == "read":
        if is_read:
            return
        if is_ingest:
            raise AuthorizationError("Ingest key cannot access read endpoints.")
        raise AuthenticationError("Invalid API key.")

    if required_scope == "admin":
        if is_ingest or is_read:
            raise AuthorizationError("Administrative privileges required.")
        raise AuthenticationError("Invalid API key.")

    raise AuthenticationError("Invalid API key.")


def get_api_key_validator(required_scope: ScopeType) -> Callable[[str | None], None]:
    """Dependency provider generating an API key validator for a specific scope."""

    def _validator(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
        validate_api_key_scope(x_api_key, required_scope)

    return _validator
