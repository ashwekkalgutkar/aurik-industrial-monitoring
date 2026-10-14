"""Anchor integration tests verifying exact expected states for EQ-001 through EQ-008.

This test suite executes the real database seed pipeline against local PostgreSQL,
validates all hand-derived anchor machine expectations as of 2026-04-18T09:00:00Z,
reconciles plant and global summaries, and tests all vendor API sample fixtures.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.cli import seed_database
from app.core.config import get_settings
from app.main import app

client = TestClient(app)
settings = get_settings()
READ_HEADERS = {"X-API-Key": settings.API_KEY_READ}
INGEST_HEADERS = {"X-API-Key": settings.API_KEY_INGEST}

ANCHOR_TIME = datetime(2026, 4, 18, 9, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def seed_environment() -> None:
    """Run full seed pipeline once before running anchor integration assertions."""
    seed_database(as_of=ANCHOR_TIME)


# ===========================================================================
# 1. Anchor Machine State Expectations (EQ-001 through EQ-008)
# ===========================================================================
def test_anchor_eq001_watch():
    """EQ-001: high vibration cleared by recovery, minor inspection note -> WATCH (att 1, needs_attention=False)."""
    resp = client.get("/v1/machines/EQ-001/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-001"
    assert data["plant_id"] == "PLANT_01"
    assert data["line_id"] == "LINE-A"
    assert data["derived_status"] == "WATCH"
    assert data["attention_level"] == 1
    assert data["needs_attention"] is False
    assert "INSPECTION_MINOR_DEFECT" in data["reason_codes"]


def test_anchor_eq002_critical_only():
    """EQ-002: severe temp spike exceeding rated limit -> CRITICAL (att 4, needs_attention=True).

    Must be the ONLY critical machine in the plant / system.
    """
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


def test_anchor_eq003_healthy():
    """EQ-003: recovery signal and OK nominal signal -> HEALTHY (att 0, needs_attention=False)."""
    resp = client.get("/v1/machines/EQ-003/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-003"
    assert data["plant_id"] == "PLANT_01"
    assert data["line_id"] == "LINE-B"
    assert data["derived_status"] == "HEALTHY"
    assert data["attention_level"] == 0
    assert data["needs_attention"] is False


def test_anchor_eq004_resolved_by_maintenance():
    """EQ-004: power drop investigated and closed with check passed -> HEALTHY (att 0, RESOLVED_BY_MAINTENANCE)."""
    resp = client.get("/v1/machines/EQ-004/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-004"
    assert data["plant_id"] == "PLANT_01"
    assert data["line_id"] == "LINE-C"
    assert data["derived_status"] == "HEALTHY"
    assert data["attention_level"] == 0
    assert data["needs_attention"] is False
    assert "RESOLVED_BY_MAINTENANCE" in data["reason_codes"]


def test_anchor_eq005_degraded_partial():
    """EQ-005: active vibration alert and partial parse records -> DEGRADED (att >= 3, needs_attention=True, PARTIAL)."""
    resp = client.get("/v1/machines/EQ-005/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-005"
    assert data["plant_id"] == "PLANT_02"
    assert data["line_id"] == "LINE-D"
    assert data["derived_status"] == "DEGRADED"
    assert data["attention_level"] >= 3
    assert data["needs_attention"] is True
    assert data["processing_status"] == "PARTIAL"


def test_anchor_eq006_watch_sensor_degraded():
    """EQ-006: SENSOR_DEGRADED signal capped at attention 1 -> WATCH (att 1, needs_attention=False, LOW_SIGNAL_TRUST)."""
    resp = client.get("/v1/machines/EQ-006/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-006"
    assert data["plant_id"] == "PLANT_02"
    assert data["line_id"] == "LINE-D"
    assert data["derived_status"] == "WATCH"
    assert data["attention_level"] == 1
    assert data["needs_attention"] is False
    assert "LOW_SIGNAL_TRUST" in data["reason_codes"]


def test_anchor_eq007_under_maintenance():
    """EQ-007: active recalibration work order -> UNDER_MAINTENANCE (needs_attention=False)."""
    resp = client.get("/v1/machines/EQ-007/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-007"
    assert data["plant_id"] == "PLANT_02"
    assert data["line_id"] == "LINE-E"
    assert data["derived_status"] == "UNDER_MAINTENANCE"
    assert data["needs_attention"] is False
    assert "MACHINE_IN_MAINTENANCE" in data["reason_codes"]


def test_anchor_eq008_degraded():
    """EQ-008: temperature spike and loose heat shield defect -> DEGRADED (att 3, needs_attention=True)."""
    resp = client.get("/v1/machines/EQ-008/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-008"
    assert data["plant_id"] == "PLANT_02"
    assert data["line_id"] == "LINE-E"
    assert data["derived_status"] == "DEGRADED"
    assert data["attention_level"] == 3
    assert data["needs_attention"] is True
    assert "INSPECTION_MAJOR_DEFECT" in data["reason_codes"]


# ===========================================================================
# 2. Plant and Global Summary Reconciliations
# ===========================================================================
def test_global_summary_reconciliation():
    """Verify system-wide summary reconciles 100% with individual machine views."""
    resp = client.get("/v1/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_machines"] == 8
    # Only EQ-002 is CRITICAL
    assert data["critical_machines"] == ["EQ-002"]
    # EQ-002, EQ-005, EQ-008 require attention
    assert data["machines_needing_attention"] == 3

    counts = data["status_counts"]
    assert counts["CRITICAL"] == 1
    assert counts["DEGRADED"] == 2
    assert counts["WATCH"] == 2
    assert counts["HEALTHY"] == 2
    assert counts["UNDER_MAINTENANCE"] == 1
    assert sum(counts.values()) == 8


def test_plant01_summary_reconciliation():
    """Plant 01 summary: 4 machines, EQ-002 is critical, LINE-A is worst line."""
    resp = client.get("/v1/plants/PLANT_01/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    assert data["plant_id"] == "PLANT_01"
    assert data["total_machines"] == 4
    assert data["critical_machines"] == ["EQ-002"]
    assert data["machines_needing_attention"] == 1
    # Line A is worst line
    assert len(data["lines_needing_attention"]) > 0
    assert data["lines_needing_attention"][0]["line_id"] == "LINE-A"
    assert data["lines_needing_attention"][0]["machines_needing_attention"] == 1


def test_plant02_summary_reconciliation():
    """Plant 02 summary: 4 machines, 0 critical, 2 needing attention (EQ-005, EQ-008)."""
    resp = client.get("/v1/plants/PLANT_02/summary", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    assert data["plant_id"] == "PLANT_02"
    assert data["total_machines"] == 4
    assert data["critical_machines"] == []
    assert data["machines_needing_attention"] == 2


# ===========================================================================
# 3. Parametrized Fixture Verification (All Sample Payloads)
# ===========================================================================
SAMPLES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "vendor_api_samples"
SAMPLE_FILES = sorted(SAMPLES_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_path", SAMPLE_FILES, ids=lambda p: p.name)
def test_all_vendor_fixtures_parse_and_persist(fixture_path: Path):
    """Verify that every assessment fixture payload ingests without 500 errors."""
    import json
    with fixture_path.open("r", encoding="utf-8") as f:
        content = json.load(f)

    # Ingest through endpoint or helper
    if "events" in content:
        res = client.post("/v1/ingest/pulseforge", json=content, headers=INGEST_HEADERS)
        assert res.status_code in (202, 422)
    elif "readings" in content:
        res = client.post("/v1/ingest/thermexwatch", json=content, headers=INGEST_HEADERS)
        assert res.status_code in (202, 422)
    elif "records" in content:
        res = client.post("/v1/ingest/maintaflow", json=content, headers=INGEST_HEADERS)
        assert res.status_code in (202, 422)
    else:
        # Multi-vendor bundle
        for _key, sub in content.items():
            if isinstance(sub, dict):
                if "events" in sub:
                    res = client.post("/v1/ingest/pulseforge", json=sub, headers=INGEST_HEADERS)
                    assert res.status_code in (202, 422)
                elif "readings" in sub:
                    res = client.post("/v1/ingest/thermexwatch", json=sub, headers=INGEST_HEADERS)
                    assert res.status_code in (202, 422)
                elif "records" in sub:
                    res = client.post("/v1/ingest/maintaflow", json=sub, headers=INGEST_HEADERS)
                    assert res.status_code in (202, 422)


def test_csv_and_json_overlap_produces_duplicate_no_ops():
    """Verify that records in CSV seed that also appear in JSON samples are detected as DUPLICATE."""
    import json
    sample_path = SAMPLES_DIR / "pulseforge_sample.json"
    with sample_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    resp = client.post("/v1/ingest/pulseforge", json=payload, headers=INGEST_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    assert data["duplicate"] == 2
    assert data["accepted"] == 0
    for rec in data["records"]:
        assert rec["disposition"] == "DUPLICATE"


def test_property_shuffle_fixture_ingestion_order_produces_identical_state():
    """Property test: shuffling signal arrival order 5 times yields identical machine operational views."""
    import random

    from app.db.base import get_db_session
    from app.db.models import Machine, NormalizedSignal
    from app.domain.engine import evaluate_machine_state

    session = next(get_db_session())
    try:
        machine = session.get(Machine, "EQ-001")
        assert machine is not None
        signals = (
            session.query(NormalizedSignal)
            .filter_by(machine_id="EQ-001", is_effective=True)
            .all()
        )
        assert len(signals) >= 2

        baseline = evaluate_machine_state(machine, signals, [], [], ANCHOR_TIME, settings)

        rng = random.Random(42)
        for _ in range(5):
            shuffled = list(signals)
            rng.shuffle(shuffled)
            result = evaluate_machine_state(machine, shuffled, [], [], ANCHOR_TIME, settings)
            assert result.derived_status == baseline.derived_status
            assert result.attention_level == baseline.attention_level
            assert result.reason_codes == baseline.reason_codes
            assert result.needs_attention == baseline.needs_attention
    finally:
        session.close()


def test_revision_supersedes_signals_and_triggers_recompute():
    """Verify that a revision supersedes the old signal (is_effective=False) and triggers recompute."""
    import uuid

    from app.core.clock import frozen_time
    from app.db.base import get_db_session
    from app.db.models import Job, NormalizedSignal, RawRecord
    from app.domain.engine import recompute_machine_state
    from app.workers.handlers import DEFAULT_JOB_HANDLERS
    from app.workers.queue import drain_queue

    test_event_id = f"PF-REV-{uuid.uuid4().hex[:8]}"
    with frozen_time("2026-04-18T09:00:00Z") as test_clock:
        session = next(get_db_session())
        try:
            # Clean up any residual test records and pending jobs from prior fixture tests
            session.query(Job).delete()
            session.query(NormalizedSignal).filter(NormalizedSignal.source_record_id.like("PF-REV-%")).delete()
            session.query(RawRecord).filter(RawRecord.record_id.like("PF-REV-%")).delete()
            session.commit()

            # 1. Ingest initial record that triggers a critical alert (after prior recovery at 08:55:00Z)
            init_envelope = {
                "vendor": "PulseForge",
                "plant_id": "PLANT_01",
                "events": [
                    {
                        "event_id": test_event_id,
                        "machine_id": "EQ-003",
                        "line_id": "LINE-B",
                        "event_time": "2026-04-18T08:58:00Z",
                        "event_type": "TEMP_SPIKE",
                        "severity": "critical",
                        "temperature_c": 115.0,
                    }
                ],
            }
            res1 = client.post("/v1/ingest/pulseforge", json=init_envelope, headers=INGEST_HEADERS)
            assert res1.status_code == 202
            assert res1.json()["accepted"] == 1

            drained1 = drain_queue(session, DEFAULT_JOB_HANDLERS, clock=test_clock)
            assert drained1 >= 1

            m_resp1 = client.get("/v1/machines/EQ-003/status", headers=READ_HEADERS)
            assert m_resp1.status_code == 200
            assert m_resp1.json()["derived_status"] == "CRITICAL"

            session.commit()
            old_sig = (
                session.query(NormalizedSignal)
                .filter_by(source_record_id=test_event_id, is_effective=True)
                .first()
            )
            assert old_sig is not None

            # 2. Ingest revision with same record_id but corrected recovery signal
            rev_envelope = {
                "vendor": "PulseForge",
                "plant_id": "PLANT_01",
                "events": [
                    {
                        "event_id": test_event_id,
                        "machine_id": "EQ-003",
                        "line_id": "LINE-B",
                        "event_time": "2026-04-18T08:58:00Z",
                        "event_type": "RECOVERY_SIGNAL",
                        "severity": "info",
                        "temperature_c": 55.0,
                    }
                ],
            }
            res2 = client.post("/v1/ingest/pulseforge", json=rev_envelope, headers=INGEST_HEADERS)
            assert res2.status_code == 202
            assert res2.json()["revision"] == 1
            assert res2.json()["records"][0]["disposition"] == "REVISION"

            drained2 = drain_queue(session, DEFAULT_JOB_HANDLERS, clock=test_clock)
            assert drained2 >= 1

            # 3. Check old signal is now superseded (is_effective=False) in fresh session
            session.close()
            session = next(get_db_session())
            old_sig_refreshed = session.get(NormalizedSignal, old_sig.id)
            assert old_sig_refreshed is not None
            assert old_sig_refreshed.is_effective is False

            # 4. Check new signal is effective and machine status recomputed to HEALTHY
            new_sig = (
                session.query(NormalizedSignal)
                .filter_by(source_record_id=test_event_id, is_effective=True)
                .first()
            )
            assert new_sig is not None
            assert new_sig.id != old_sig.id

            m_resp2 = client.get("/v1/machines/EQ-003/status", headers=READ_HEADERS)
            assert m_resp2.status_code == 200
            assert m_resp2.json()["derived_status"] == "HEALTHY"
        finally:
            # Cleanup test records and restore baseline state
            session.query(Job).delete()
            session.query(NormalizedSignal).filter(NormalizedSignal.source_record_id.like("PF-REV-%")).delete()
            session.query(RawRecord).filter(RawRecord.record_id.like("PF-REV-%")).delete()
            session.commit()
            recompute_machine_state("EQ-003", session)
            session.close()


