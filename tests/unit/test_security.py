"""Unit tests for constant-time API key verification, scope enforcement, and production security."""

import pytest

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security import validate_api_key_scope


def test_missing_api_key_raises_401():
    with pytest.raises(AuthenticationError) as exc_info:
        validate_api_key_scope(None, "ingest")
    assert exc_info.value.status_code == 401


def test_invalid_api_key_raises_401():
    with pytest.raises(AuthenticationError) as exc_info:
        validate_api_key_scope("wrong-invalid-key-xyz", "ingest")
    assert exc_info.value.status_code == 401


def test_valid_ingest_key_accepted_for_ingest():
    settings = get_settings()
    validate_api_key_scope(settings.API_KEY_INGEST, "ingest")


def test_read_key_attempting_ingest_raises_403():
    settings = get_settings()
    with pytest.raises(AuthorizationError) as exc_info:
        validate_api_key_scope(settings.API_KEY_READ, "ingest")
    assert exc_info.value.status_code == 403


def test_ingest_key_attempting_read_raises_403():
    settings = get_settings()
    with pytest.raises(AuthorizationError) as exc_info:
        validate_api_key_scope(settings.API_KEY_INGEST, "read")
    assert exc_info.value.status_code == 403


def test_admin_key_satisfies_all_scopes():
    settings = get_settings()
    validate_api_key_scope(settings.API_KEY_ADMIN, "ingest")
    validate_api_key_scope(settings.API_KEY_ADMIN, "read")
    validate_api_key_scope(settings.API_KEY_ADMIN, "admin")


def test_production_mode_refuses_default_keys():
    """In production mode, default or example keys must be rejected."""
    with pytest.raises(ValueError, match="cannot equal known example/default keys"):
        Settings(
            APP_ENV="production",
            API_KEY_INGEST="aurik-ingest-secret-key-prod-001",
            API_KEY_READ="a-very-long-random-secret-key-002",
            API_KEY_ADMIN="a-very-long-random-secret-key-003",
        )


def test_production_mode_refuses_short_keys():
    """In production mode, keys shorter than 24 characters must be rejected."""
    with pytest.raises(ValueError, match="must be at least 24 characters long"):
        Settings(
            APP_ENV="production",
            API_KEY_INGEST="short-key-123",
            API_KEY_READ="a-very-long-random-secret-key-002",
            API_KEY_ADMIN="a-very-long-random-secret-key-003",
        )


def test_production_mode_refuses_empty_keys():
    """In production mode, empty keys must be rejected."""
    with pytest.raises(ValueError, match="must be set to a non-empty secret"):
        Settings(
            APP_ENV="production",
            API_KEY_INGEST="",
            API_KEY_READ="a-very-long-random-secret-key-002",
            API_KEY_ADMIN="a-very-long-random-secret-key-003",
        )


def test_production_mode_accepts_strong_keys():
    """In production mode, strong keys >= 24 chars are accepted."""
    s = Settings(
        APP_ENV="production",
        API_KEY_INGEST="super-secret-ingest-key-prod-123456789",
        API_KEY_READ="super-secret-read-key-prod-1234567890",
        API_KEY_ADMIN="super-secret-admin-key-prod-123456789",
    )
    assert s.APP_ENV == "production"
    assert s.docs_enabled is False


def test_docs_enabled_property():
    """Docs should be enabled in dev and configurable."""
    dev_settings = Settings(APP_ENV="development")
    assert dev_settings.docs_enabled is True

    prod_override = Settings(
        APP_ENV="production",
        API_KEY_INGEST="super-secret-ingest-key-prod-123456789",
        API_KEY_READ="super-secret-read-key-prod-1234567890",
        API_KEY_ADMIN="super-secret-admin-key-prod-123456789",
        ENABLE_DOCS=True,
    )
    assert prod_override.docs_enabled is True


def test_cors_origins_parsing():
    """CORS origins string should parse into clean list."""
    empty_s = Settings(CORS_ALLOW_ORIGINS="")
    assert empty_s.cors_origins == []

    parsed_s = Settings(CORS_ALLOW_ORIGINS="https://app.aurik.io, https://admin.aurik.io ")
    assert parsed_s.cors_origins == ["https://app.aurik.io", "https://admin.aurik.io"]
