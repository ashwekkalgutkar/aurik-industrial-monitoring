"""Unit tests for constant-time API key verification and scope enforcement."""

import pytest

from app.core.config import get_settings
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
