"""Integration tests for Ingestion API, raw-first persistence, and Layer 1 idempotency."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.base import get_db_session
from app.db.models import Job, RawRecord
from app.main import app

client = TestClient(app)
settings = get_settings()
INGEST_HEADERS = {"X-API-Key": settings.API_KEY_INGEST}


@pytest.fixture(autouse=True)
def clean_db():
    """Clean dynamic tables between tests while retaining reference records."""
    session = next(get_db_session())
    try:
        session.query(Job).delete()
        session.query(RawRecord).delete()
        session.commit()
    finally:
        session.close()
    yield


def test_auth_headers_required():
    payload = {
        "vendor": "PulseForge",
        "plant_id": "PLANT_01",
        "events": [],
    }
    # No header -> 401
    resp = client.post("/v1/ingest/pulseforge", json=payload)
    assert resp.status_code == 401

    # Read key on ingest -> 403
    resp = client.post(
        "/v1/ingest/pulseforge",
        json=payload,
        headers={"X-API-Key": settings.API_KEY_READ},
    )
    assert resp.status_code == 403


def test_missing_envelope_key_raises_422():
    # Missing required key 'plant_id'
    bad_envelope = {
        "vendor": "PulseForge",
        "events": [],
    }
    resp = client.post("/v1/ingest/pulseforge", json=bad_envelope, headers=INGEST_HEADERS)
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"]["code"] == "VALIDATION_ERROR"
    assert "plant_id" in data["error"]["message"]


def test_unsupported_schema_version_rejected():
    envelope = {
        "vendor": "PulseForge",
        "plant_id": "PLANT_01",
        "schema_version": "v999.0",
        "events": [],
    }
    resp = client.post("/v1/ingest/pulseforge", json=envelope, headers=INGEST_HEADERS)
    assert resp.status_code == 422
    assert "Unsupported schema_version" in resp.json()["error"]["message"]


def test_pulseforge_ingest_accepted_and_duplicate():
    envelope = {
        "vendor": "PulseForge",
        "plant_id": "PLANT_01",
        "batch_generated_at": "2026-04-18T08:05:00Z",
        "events": [
            {
                "event_id": "PF-TEST-001",
                "machine_id": "EQ-001",
                "line_id": "LINE-A",
                "event_time": "2026-04-18T07:59:12Z",
                "event_type": "HIGH_VIBRATION",
                "severity": "high",
                "vibration_mm_s": 11.8,
                "temperature_c": 83.2,
                "machine_state": "running",
                "sensor_health": 0.91,
                "vendor_confidence": 0.87,
            }
        ],
    }

    # First ingest -> ACCEPTED
    resp1 = client.post("/v1/ingest/pulseforge", json=envelope, headers=INGEST_HEADERS)
    assert resp1.status_code == 202
    data1 = resp1.json()
    assert data1["accepted"] == 1
    assert data1["duplicate"] == 0
    assert data1["records"][0]["disposition"] == "ACCEPTED"

    # Re-ingest identical record -> DUPLICATE no-op
    resp2 = client.post("/v1/ingest/pulseforge", json=envelope, headers=INGEST_HEADERS)
    assert resp2.status_code == 202
    data2 = resp2.json()
    assert data2["accepted"] == 0
    assert data2["duplicate"] == 1
    assert data2["records"][0]["disposition"] == "DUPLICATE"


def test_revision_supersedes_prior_record():
    initial_envelope = {
        "vendor": "PulseForge",
        "plant_id": "PLANT_01",
        "events": [
            {
                "event_id": "PF-REV-1",
                "machine_id": "EQ-001",
                "line_id": "LINE-A",
                "event_time": "2026-04-18T07:59:12Z",
                "event_type": "HIGH_VIBRATION",
                "severity": "medium",
                "temperature_c": 75.0,
            }
        ],
    }
    resp1 = client.post("/v1/ingest/pulseforge", json=initial_envelope, headers=INGEST_HEADERS)
    assert resp1.json()["accepted"] == 1

    updated_envelope = {
        "vendor": "PulseForge",
        "plant_id": "PLANT_01",
        "events": [
            {
                "event_id": "PF-REV-1",
                "machine_id": "EQ-001",
                "line_id": "LINE-A",
                "event_time": "2026-04-18T07:59:12Z",
                "event_type": "HIGH_VIBRATION",
                "severity": "critical",  # Updated value!
                "temperature_c": 95.0,
            }
        ],
    }
    resp2 = client.post("/v1/ingest/pulseforge", json=updated_envelope, headers=INGEST_HEADERS)
    assert resp2.status_code == 202
    data2 = resp2.json()
    assert data2["revision"] == 1
    assert data2["records"][0]["disposition"] == "REVISION"


def test_intra_batch_duplicate_handling():
    """Verify that identical record appearing twice in the same batch yields ACCEPTED then DUPLICATE."""
    envelope = {
        "vendor": "PulseForge",
        "plant_id": "PLANT_01",
        "events": [
            {
                "event_id": "PF-INTRA",
                "machine_id": "EQ-001",
                "line_id": "LINE-A",
                "event_time": "2026-04-18T07:59:12Z",
                "event_type": "HIGH_VIBRATION",
                "severity": "high",
            },
            {
                "event_id": "PF-INTRA",
                "machine_id": "EQ-001",
                "line_id": "LINE-A",
                "event_time": "2026-04-18T07:59:12Z",
                "event_type": "HIGH_VIBRATION",
                "severity": "high",
            },
        ],
    }
    resp = client.post("/v1/ingest/pulseforge", json=envelope, headers=INGEST_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    assert data["accepted"] == 1
    assert data["duplicate"] == 1
    assert data["records"][0]["disposition"] == "ACCEPTED"
    assert data["records"][1]["disposition"] == "DUPLICATE"


def test_structurally_rejected_record_does_not_block_valid_sibling():
    envelope = {
        "vendor": "PulseForge",
        "plant_id": "PLANT_01",
        "events": [
            {
                # Missing event_id -> structural rejection
                "machine_id": "EQ-001",
                "event_time": "2026-04-18T07:59:12Z",
            },
            {
                # Valid sibling
                "event_id": "PF-VALID-SIBLING",
                "machine_id": "EQ-001",
                "line_id": "LINE-A",
                "event_time": "2026-04-18T07:59:12Z",
                "event_type": "HIGH_VIBRATION",
                "severity": "low",
            },
        ],
    }
    resp = client.post("/v1/ingest/pulseforge", json=envelope, headers=INGEST_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    assert data["total"] == 2
    assert data["rejected"] == 1
    assert data["accepted"] == 1
    assert data["records"][0]["disposition"] == "REJECTED"
    assert data["records"][1]["disposition"] == "ACCEPTED"
