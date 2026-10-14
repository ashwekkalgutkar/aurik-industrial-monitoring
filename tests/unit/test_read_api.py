"""Unit and integration tests for operational read APIs, summaries, and health endpoints."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.base import get_db_session
from app.db.models import (
    IngestionBatch,
    Job,
    MachineStateView,
    NormalizedSignal,
    RawRecord,
)
from app.main import app
from app.vendors.enums import CanonicalEventType, Dimension, SignalKind

client = TestClient(app)
settings = get_settings()
READ_HEADERS = {"X-API-Key": settings.API_KEY_READ}
ADMIN_HEADERS = {"X-API-Key": settings.API_KEY_ADMIN}
INGEST_HEADERS = {"X-API-Key": settings.API_KEY_INGEST}


@pytest.fixture(autouse=True)
def clean_operational_tables():
    """Wipe operational and audit tables between test runs while keeping reference records."""
    session = next(get_db_session())
    try:
        session.query(MachineStateView).delete()
        session.query(NormalizedSignal).delete()
        session.query(Job).delete()
        session.query(RawRecord).delete()
        session.query(IngestionBatch).delete()
        session.commit()
    finally:
        session.close()
    yield


# ===========================================================================
# 1. Health & Liveness / Readiness
# ===========================================================================
def test_healthz_unauthenticated():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "1.0.0"
    assert "timestamp" in data


def test_readyz_database_connected():
    resp = client.get("/readyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"


# ===========================================================================
# 2. Authentication Scoping on Read Endpoints
# ===========================================================================
def test_read_endpoints_auth_enforcement():
    # 1. Missing header -> 401
    resp = client.get("/v1/machines/status")
    assert resp.status_code == 401

    # 2. Ingest key -> 403 Forbidden
    resp = client.get("/v1/machines/status", headers=INGEST_HEADERS)
    assert resp.status_code == 403
    assert "Ingest key cannot access read endpoints." in resp.json()["error"]["message"]

    # 3. Read key -> 200 OK
    resp = client.get("/v1/machines/status", headers=READ_HEADERS)
    assert resp.status_code == 200

    # 4. Admin key -> 200 OK
    resp = client.get("/v1/machines/status", headers=ADMIN_HEADERS)
    assert resp.status_code == 200


# ===========================================================================
# 3. Machine Status Views (Single & Paginated)
# ===========================================================================
def _seed_sample_views() -> None:
    session = next(get_db_session())
    try:
        now = datetime(2026, 4, 18, 9, 0, tzinfo=UTC)
        v1 = MachineStateView(
            machine_id="EQ-001",
            plant_id="PLANT_01",
            line_id="LINE-A",
            derived_status="WATCH",
            attention_level=1,
            needs_attention=False,
            reason_codes=["VIBRATION_HIGH"],
            explanation=[
                {
                    "reason_code": "VIBRATION_HIGH",
                    "attention_contribution": 1,
                    "rule_applied": "Active vibration alert within TTL.",
                    "observed_value": {"vibration_mm_s": 5.2},
                    "signal_refs": [
                        {
                            "vendor": "PulseForge",
                            "record_id": "PF-01",
                            "raw_record_id": "00000000-0000-0000-0000-000000000001",
                            "role": "alert",
                        }
                    ],
                }
            ],
            latest_relevant_event_time=now,
            last_processed_at=now,
            processing_status="FRESH",
            source_event_refs=[
                {
                    "vendor": "PulseForge",
                    "record_id": "PF-01",
                    "raw_record_id": "00000000-0000-0000-0000-000000000001",
                    "role": "alert",
                }
            ],
            rules_version="1.0.0",
        )
        v2 = MachineStateView(
            machine_id="EQ-002",
            plant_id="PLANT_01",
            line_id="LINE-A",
            derived_status="CRITICAL",
            attention_level=4,
            needs_attention=True,
            reason_codes=["TEMPERATURE_ABOVE_RATED"],
            explanation=[
                {
                    "reason_code": "TEMPERATURE_ABOVE_RATED",
                    "attention_contribution": 4,
                    "rule_applied": "Critical temperature alert within TTL.",
                    "observed_value": {"temperature_c": 96.4},
                    "signal_refs": [
                        {
                            "vendor": "ThermexWatch",
                            "record_id": "TW-02",
                            "raw_record_id": "00000000-0000-0000-0000-000000000002",
                            "role": "alert",
                        }
                    ],
                }
            ],
            latest_relevant_event_time=now,
            last_processed_at=now,
            processing_status="FRESH",
            source_event_refs=[
                {
                    "vendor": "ThermexWatch",
                    "record_id": "TW-02",
                    "raw_record_id": "00000000-0000-0000-0000-000000000002",
                    "role": "alert",
                }
            ],
            rules_version="1.0.0",
        )
        v3 = MachineStateView(
            machine_id="EQ-003",
            plant_id="PLANT_01",
            line_id="LINE-B",
            derived_status="HEALTHY",
            attention_level=0,
            needs_attention=False,
            reason_codes=[],
            explanation=[],
            latest_relevant_event_time=now,
            last_processed_at=now,
            processing_status="FRESH",
            source_event_refs=[],
            rules_version="1.0.0",
        )
        session.add_all([v1, v2, v3])
        session.commit()
    finally:
        session.close()


def test_get_machine_status_unknown_404():
    resp = client.get("/v1/machines/EQ-NONEXISTENT/status", headers=READ_HEADERS)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_get_machine_status_success():
    _seed_sample_views()
    resp = client.get("/v1/machines/EQ-002/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-002"
    assert data["plant_id"] == "PLANT_01"
    assert data["line_id"] == "LINE-A"
    assert data["derived_status"] == "CRITICAL"
    assert data["attention_level"] == 4
    assert data["needs_attention"] is True
    assert "TEMPERATURE_ABOVE_RATED" in data["reason_codes"]
    assert len(data["source_event_refs"]) == 1


def test_list_machines_status_sorting_and_filtering():
    _seed_sample_views()

    # 1. Default sort: attention_level desc (EQ-002 att 4 -> EQ-001 att 1 -> EQ-003 att 0)
    resp = client.get("/v1/machines/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert len(data["items"]) == 3
    assert data["items"][0]["machine_id"] == "EQ-002"
    assert data["items"][1]["machine_id"] == "EQ-001"
    assert data["items"][2]["machine_id"] == "EQ-003"

    # 2. Filter by needs_attention=true
    resp = client.get("/v1/machines/status?needs_attention=true", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["machine_id"] == "EQ-002"

    # 3. Filter by min_attention_level=1
    resp = client.get("/v1/machines/status?min_attention_level=1", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    ids = [item["machine_id"] for item in data["items"]]
    assert ids == ["EQ-002", "EQ-001"]

    # 4. Filter by line_id=LINE-B
    resp = client.get("/v1/machines/status?line_id=LINE-B", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["machine_id"] == "EQ-003"

    # 5. Pagination page=1, page_size=2
    resp = client.get("/v1/machines/status?page=1&page_size=2", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) == 2


# ===========================================================================
# 4. Plant & Line Operational Summaries
# ===========================================================================
def test_plant_summary_404_for_unknown_plant():
    resp = client.get("/v1/plants/UNKNOWN_PLANT/summary", headers=READ_HEADERS)
    assert resp.status_code == 404


def test_plant_summary_reconciles_machines():
    _seed_sample_views()
    resp = client.get("/v1/plants/PLANT_01/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["plant_id"] == "PLANT_01"
    assert data["total_machines"] == 4  # EQ-001, EQ-002, EQ-003, EQ-004 in PLANT_01
    assert data["machines_needing_attention"] == 1  # EQ-002
    assert "EQ-002" in data["critical_machines"]
    assert len(data["lines_needing_attention"]) > 0
    # Worst line should be LINE-A (has EQ-002 requiring attention)
    assert data["lines_needing_attention"][0]["line_id"] == "LINE-A"
    assert data["lines_needing_attention"][0]["machines_needing_attention"] == 1


def test_line_summary_success():
    _seed_sample_views()
    resp = client.get("/v1/lines/LINE-A/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["line_id"] == "LINE-A"
    assert data["total_machines"] == 2  # EQ-001, EQ-002
    assert data["machines_needing_attention"] == 1
    assert len(data["machines"]) == 2
    # Sorted by attention desc: EQ-002 first, then EQ-001
    assert data["machines"][0]["machine_id"] == "EQ-002"
    assert data["machines"][1]["machine_id"] == "EQ-001"


def test_global_summary():
    _seed_sample_views()
    resp = client.get("/v1/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_machines"] == 8
    assert data["machines_needing_attention"] == 1
    assert "EQ-002" in data["critical_machines"]
    assert len(data["plants"]) == 2
    assert len(data["worst_lines"]) > 0
    assert data["worst_lines"][0]["line_id"] == "LINE-A"


# ===========================================================================
# 5. Ingestion Batch & Record Deep Audit Lookups
# ===========================================================================
def test_batch_and_record_audit_endpoints():
    session = next(get_db_session())
    try:
        now = datetime(2026, 4, 18, 8, 30, tzinfo=UTC)
        batch = IngestionBatch(
            id="batch-test-123",
            vendor="PulseForge",
            received_at=now,
            status="COMPLETED",
            counts={"total": 1, "accepted": 1, "duplicate": 0, "rejected": 0, "source_filename": "test.json"},
        )
        record = RawRecord(
            id="raw-rec-456",
            batch_id="batch-test-123",
            vendor="PulseForge",
            record_id="PF-AUDIT-01",
            revision=1,
            payload={"event_id": "PF-AUDIT-01", "machine_id": "EQ-001"},
            payload_sha256="abc123def456",
            received_at=now,
            resolved_machine_id="EQ-001",
            processing_state="PROCESSED",
            rejection_reasons=[],
        )
        sig = NormalizedSignal(
            id="sig-789",
            raw_record_id="raw-rec-456",
            source_vendor="PulseForge",
            source_record_id="PF-AUDIT-01",
            machine_id="EQ-001",
            plant_id="PLANT_01",
            line_id="LINE-A",
            event_time=now,
            ingested_at=now,
            canonical_event_type=CanonicalEventType.VIBRATION_HIGH,
            dimension=Dimension.VIBRATION,
            signal_kind=SignalKind.ALERT,
            severity=2,
            vibration_mm_s=8.5,
            is_effective=True,
            details={},
            data_quality_flags=[],
        )
        session.add_all([batch, record, sig])
        session.commit()
    finally:
        session.close()

    # 1. Batch details
    resp = client.get("/v1/ingestion/batches/batch-test-123", headers=READ_HEADERS)
    assert resp.status_code == 200
    b_data = resp.json()
    assert b_data["batch_id"] == "batch-test-123"
    assert b_data["vendor"] == "PulseForge"
    assert b_data["source_filename"] == "test.json"
    assert len(b_data["records"]) == 1
    assert b_data["records"][0]["record_id"] == "PF-AUDIT-01"

    # Batch 404
    resp = client.get("/v1/ingestion/batches/nonexistent", headers=READ_HEADERS)
    assert resp.status_code == 404

    # 2. Record details
    resp = client.get("/v1/ingestion/records/PulseForge/PF-AUDIT-01", headers=READ_HEADERS)
    assert resp.status_code == 200
    r_data = resp.json()
    assert r_data["record_id"] == "PF-AUDIT-01"
    assert r_data["payload_hash"] == "abc123def456"
    assert len(r_data["signals"]) == 1
    assert r_data["signals"][0]["canonical_event_type"] == "VIBRATION_HIGH"

    # Record 404
    resp = client.get("/v1/ingestion/records/PulseForge/NONEXISTENT", headers=READ_HEADERS)
    assert resp.status_code == 404


# ===========================================================================
# 6. Prometheus Text Metrics
# ===========================================================================
def test_prometheus_metrics_export():
    _seed_sample_views()
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "aurik_queue_jobs_total" in body
    assert "aurik_machine_status_total" in body
    assert 'aurik_machine_status_total{status="CRITICAL"} 1' in body
    assert 'aurik_machine_status_total{status="WATCH"} 1' in body
    assert 'aurik_machine_status_total{status="HEALTHY"} 1' in body


# ===========================================================================
# 7. Time-Travel as_of Machine Status
# ===========================================================================
def test_time_travel_as_of_machine_status():
    session = next(get_db_session())
    try:
        t_alert = datetime(2026, 4, 18, 8, 30, tzinfo=UTC)
        t_recovery = datetime(2026, 4, 18, 8, 45, tzinfo=UTC)

        batch = IngestionBatch(
            id="batch-tt",
            vendor="PulseForge",
            received_at=t_alert,
            status="COMPLETED",
            counts={"total": 2},
        )
        rec1 = RawRecord(
            id="raw-tt-1",
            batch_id="batch-tt",
            vendor="PulseForge",
            record_id="PF-TT-1",
            revision=1,
            payload={},
            payload_sha256="hash1",
            received_at=t_alert,
            resolved_machine_id="EQ-001",
            processing_state="PROCESSED",
            rejection_reasons=[],
        )
        rec2 = RawRecord(
            id="raw-tt-2",
            batch_id="batch-tt",
            vendor="PulseForge",
            record_id="PF-TT-2",
            revision=1,
            payload={},
            payload_sha256="hash2",
            received_at=t_recovery,
            resolved_machine_id="EQ-001",
            processing_state="PROCESSED",
            rejection_reasons=[],
        )
        sig1 = NormalizedSignal(
            id="sig-tt-1",
            raw_record_id="raw-tt-1",
            source_vendor="PulseForge",
            source_record_id="PF-TT-1",
            machine_id="EQ-001",
            plant_id="PLANT_01",
            line_id="LINE-A",
            event_time=t_alert,
            ingested_at=t_alert,
            canonical_event_type=CanonicalEventType.VIBRATION_HIGH,
            dimension=Dimension.VIBRATION,
            signal_kind=SignalKind.ALERT,
            severity=3,
            vibration_mm_s=15.0,  # Exceeds rated limit of 10.0 -> att 3 + crit bump -> att 4
            is_effective=True,
            details={},
            data_quality_flags=[],
        )
        sig2 = NormalizedSignal(
            id="sig-tt-2",
            raw_record_id="raw-tt-2",
            source_vendor="PulseForge",
            source_record_id="PF-TT-2",
            machine_id="EQ-001",
            plant_id="PLANT_01",
            line_id="LINE-A",
            event_time=t_recovery,
            ingested_at=t_recovery,
            canonical_event_type=CanonicalEventType.NOMINAL,
            dimension=Dimension.VIBRATION,
            signal_kind=SignalKind.TELEMETRY,
            severity=0,
            is_effective=True,
            details={},
            data_quality_flags=[],
        )
        session.add_all([batch, rec1, rec2, sig1, sig2])
        session.commit()
    finally:
        session.close()

    # 1. As of 08:35 (after alert, before recovery): EQ-001 is CRITICAL
    resp_alert = client.get(
        "/v1/machines/EQ-001/status?as_of=2026-04-18T08:35:00Z",
        headers=READ_HEADERS,
    )
    assert resp_alert.status_code == 200
    d_alert = resp_alert.json()
    assert d_alert["derived_status"] == "CRITICAL"
    assert d_alert["needs_attention"] is True
    assert "VIBRATION_ABOVE_RATED" in d_alert["reason_codes"]

    # 2. As of 08:50 (after recovery): EQ-001 is HEALTHY
    resp_rec = client.get(
        "/v1/machines/EQ-001/status?as_of=2026-04-18T08:50:00Z",
        headers=READ_HEADERS,
    )
    assert resp_rec.status_code == 200
    d_rec = resp_rec.json()
    assert d_rec["derived_status"] == "HEALTHY"
    assert d_rec["needs_attention"] is False
    assert "RECOVERING" in d_rec["reason_codes"]
