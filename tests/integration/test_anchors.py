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
from app.domain.engine import ReasonCode
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
    # Derivation:
    # All vibration alerts (PF-1001, PF-1101, TW-8801, PF-1301) are older than recovery signal
    # PF-1302 at 08:32:05Z, which clears them -> RECOVERING (att 0).
    # Latest inspection MF-501 at 08:14:05 recorded minor defect -> INSPECTION_MINOR_DEFECT (att 1).
    # Max attention = 1 -> WATCH, needs_attention=False.
    # Latest relevant event time is 08:32:05Z (recovery PF-1302).
    # Latest signal PF-1302 (< 30 min from 09:00:00Z) -> FRESH.
    resp = client.get("/v1/machines/EQ-001/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-001"
    assert data["plant_id"] == "PLANT_01"
    assert data["line_id"] == "LINE-A"
    assert data["derived_status"] == "WATCH"
    assert data["attention_level"] == 1
    assert data["needs_attention"] is False
    assert data["processing_status"] == "FRESH"
    assert set(data["reason_codes"]) == {"INSPECTION_MINOR_DEFECT", "RECOVERING"}
    assert data["latest_relevant_event_time"].startswith("2026-04-18T08:32:05")


def test_anchor_eq002_critical_only():
    # Derivation:
    # PF-1002 temp 96.4C exceeds rated 90.0C (att 3).
    # TW-8803 TEMP_CRIT at 07:58:40Z escalates attention to 4 -> CRITICAL, needs_attention=True.
    # Contributing reasons include TEMPERATURE_ABOVE_RATED, MAINT_OVERDUE (MF-503, MF-901 overdue),
    # and OPEN_WORK_ORDER (WO-1001 open cooling_system).
    # PF-1202 malformed record (bad timestamp format) resolved to EQ-002 within TTL -> PARTIAL.
    # Latest relevant event time is 08:49:00Z (MF-901 overdue update).
    resp = client.get("/v1/machines/EQ-002/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-002"
    assert data["plant_id"] == "PLANT_01"
    assert data["line_id"] == "LINE-A"
    assert data["derived_status"] == "CRITICAL"
    assert data["attention_level"] == 4
    assert data["needs_attention"] is True
    assert data["processing_status"] == "PARTIAL"
    assert {"TEMPERATURE_ABOVE_RATED", "MAINT_OVERDUE", "OPEN_WORK_ORDER"}.issubset(set(data["reason_codes"]))
    assert data["latest_relevant_event_time"].startswith("2026-04-18T08:49:00")


def test_anchor_eq003_healthy():
    # Derivation:
    # Only NOMINAL telemetry received: PF-1003 (07:56:40Z), TW-8802 (07:56:50Z), PF-1102 (08:17:01Z).
    # Telemetry on NOMINAL signals never raises attention. Attention = 0 -> HEALTHY, needs_attention=False.
    # Reason codes array is empty.
    # Latest signal PF-1102 at 08:17:01Z is older than 30 min from 09:00:00Z -> STALE.
    resp = client.get("/v1/machines/EQ-003/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-003"
    assert data["plant_id"] == "PLANT_01"
    assert data["line_id"] == "LINE-B"
    assert data["derived_status"] == "HEALTHY"
    assert data["attention_level"] == 0
    assert data["needs_attention"] is False
    assert data["processing_status"] == "STALE"
    assert data["reason_codes"] == []
    assert data["latest_relevant_event_time"].startswith("2026-04-18T08:17:01")


def test_anchor_eq004_resolved_by_maintenance():
    # Derivation:
    # Closed work order WO-1002 (post_maintenance_check=passed, closed at 08:10:00Z) and completed
    # maintenance MF-701 at 08:40:00Z resolve and suppress earlier power drop alerts (PF-1004, TW-8804)
    # and operator notes (MF-502, MF-700) -> RESOLVED_BY_MAINTENANCE (att 0).
    # Attention = 0 -> HEALTHY, needs_attention=False.
    # Latest signal MF-701 at 08:40:00Z is fresh (< 30 min) -> FRESH.
    resp = client.get("/v1/machines/EQ-004/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-004"
    assert data["plant_id"] == "PLANT_01"
    assert data["line_id"] == "LINE-C"
    assert data["derived_status"] == "HEALTHY"
    assert data["attention_level"] == 0
    assert data["needs_attention"] is False
    assert data["processing_status"] == "FRESH"
    assert set(data["reason_codes"]) == {"RESOLVED_BY_MAINTENANCE"}
    assert data["latest_relevant_event_time"].startswith("2026-04-18T08:40:00")


def test_anchor_eq005_degraded_partial():
    # Derivation:
    # Rated max vibration is 8.0 mm/s.
    # PF-1005 measured 8.4 mm/s, PF-1501 measured 8.8 mm/s -> both exceed rated 8.0 mm/s (att 3).
    # Reasons include VIBRATION_ABOVE_RATED, INSPECTION_MINOR_DEFECT (MF-504, MF-601 observation),
    # and MAINT_DUE_SOON. Medium asset criticality -> no criticality bump.
    # Max attention = 3 -> DEGRADED, needs_attention=True.
    # PF-1502 rejected for invalid event_time ('bad-time') and resolved to EQ-005 within TTL -> PARTIAL.
    # Latest relevant event time is 08:47:12Z (PF-1501).
    resp = client.get("/v1/machines/EQ-005/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-005"
    assert data["plant_id"] == "PLANT_02"
    assert data["line_id"] == "LINE-D"
    assert data["derived_status"] == "DEGRADED"
    assert data["attention_level"] == 3
    assert data["needs_attention"] is True
    assert data["processing_status"] == "PARTIAL"
    assert {"VIBRATION_ABOVE_RATED", "INSPECTION_MINOR_DEFECT", "MAINT_DUE_SOON"}.issubset(set(data["reason_codes"]))
    assert data["latest_relevant_event_time"].startswith("2026-04-18T08:47:12")


def test_anchor_eq006_watch_sensor_degraded():
    # Derivation:
    # PF-1006 SENSOR_HEALTH_DROP has sensor_health=0.49 < 0.50 minimum quality threshold.
    # Per engine rule 4, sensor degraded signals never exceed attention 1 and produce LOW_SIGNAL_TRUST.
    # Nominal signal TW-9902 at 08:15:25Z does not clear sensor quality issues.
    # Attention = 1 -> WATCH, needs_attention=False.
    # Latest signal TW-9902 at 08:15:25Z is older than 30 min from 09:00:00Z -> STALE.
    # Latest relevant event time is 07:50:10Z (PF-1006).
    resp = client.get("/v1/machines/EQ-006/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-006"
    assert data["plant_id"] == "PLANT_02"
    assert data["line_id"] == "LINE-D"
    assert data["derived_status"] == "WATCH"
    assert data["attention_level"] == 1
    assert data["needs_attention"] is False
    assert data["processing_status"] == "STALE"
    assert set(data["reason_codes"]) == {"LOW_SIGNAL_TRUST"}
    assert data["latest_relevant_event_time"].startswith("2026-04-18T07:50:10")


def test_anchor_eq007_under_maintenance():
    # Derivation:
    # Machine marked with asset_status='maintenance' in asset reference and has open scheduled
    # recalibration work order WO-1004 (06:50:00Z). MF-506 calibration_pending at 06:55:00Z.
    # TW-8807 at 07:46:40Z has alertCode OK with is_active=False (idle telemetry, not an alarm).
    # Attention = 1 with maintenance override -> UNDER_MAINTENANCE, needs_attention=False.
    # Full reasons: {CALIBRATION_PENDING, OPEN_WORK_ORDER, MACHINE_IN_MAINTENANCE}.
    # Latest signal TW-8807 at 07:46:40Z is older than 30 min -> STALE.
    # Latest relevant event time is 06:55:00Z (MF-506).
    resp = client.get("/v1/machines/EQ-007/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-007"
    assert data["plant_id"] == "PLANT_02"
    assert data["line_id"] == "LINE-E"
    assert data["derived_status"] == "UNDER_MAINTENANCE"
    assert data["attention_level"] == 1
    assert data["needs_attention"] is False
    assert data["processing_status"] == "STALE"
    assert set(data["reason_codes"]) == {"CALIBRATION_PENDING", "OPEN_WORK_ORDER", "MACHINE_IN_MAINTENANCE"}
    assert data["latest_relevant_event_time"].startswith("2026-04-18T06:55:00")


def test_anchor_eq008_degraded():
    # Derivation:
    # Latest inspection MF-801 at 08:41:00Z found major defect (loose heat shield) -> att 3 -> DEGRADED.
    # Open work order WO-1003 for heat_shield (att 1).
    # TW-9912 alert code UNKNOWN_ALERT at 08:17:00Z contributes UNMAPPED_ALERT_CODE (att 1, never cleared by NOMINAL).
    # TW-9921 TEMP_CRIT at 08:29:10Z is cleared by PF-1401 RECOVERY_SIGNAL at 08:39:10Z -> RECOVERING.
    # Time diff (10 min) > 5 min conflict window, so zero VENDOR_CONFLICT.
    # Full reason codes exactly: {INSPECTION_MAJOR_DEFECT, UNMAPPED_ALERT_CODE, OPEN_WORK_ORDER, RECOVERING}.
    # Latest signal MF-801 at 08:41:00Z (< 30 min) -> FRESH.
    resp = client.get("/v1/machines/EQ-008/status", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "EQ-008"
    assert data["plant_id"] == "PLANT_02"
    assert data["line_id"] == "LINE-E"
    assert data["derived_status"] == "DEGRADED"
    assert data["attention_level"] == 3
    assert data["needs_attention"] is True
    assert data["processing_status"] == "FRESH"
    assert set(data["reason_codes"]) == {"INSPECTION_MAJOR_DEFECT", "UNMAPPED_ALERT_CODE", "OPEN_WORK_ORDER", "RECOVERING"}
    assert data["latest_relevant_event_time"].startswith("2026-04-18T08:41:00")


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


def test_closed_reason_code_vocabulary_enforced():
    """Verify that every machine operational view only emits reason codes in the closed enum."""
    allowed_codes = {rc.value for rc in ReasonCode}
    prohibited_codes = {
        "HIGH_CRITICALITY_ASSET",
        "TEMP_ABOVE_RATED_LIMIT",
        "VIBRATION_APPROACHING_LIMIT",
        "MALFORMED_PAYLOAD_SKIPPED",
        "NOMINAL_OPERATION",
    }

    resp = client.get("/v1/machines/status?page_size=50", headers=READ_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 8

    all_observed_codes: set[str] = set()
    for item in data["items"]:
        codes = set(item["reason_codes"])
        # All emitted codes must be in the closed enum
        assert codes.issubset(
            allowed_codes
        ), f"Machine {item['machine_id']} emitted invalid codes: {codes - allowed_codes}"
        # None of the prohibited codes can be present
        assert codes.isdisjoint(
            prohibited_codes
        ), f"Machine {item['machine_id']} emitted prohibited codes: {codes & prohibited_codes}"
        all_observed_codes.update(codes)

    assert len(all_observed_codes) > 0


def test_time_travel_as_of_semantics():
    """Verify as_of recomputes purely in memory, echoes as_of, and does not alter database."""
    from app.db.base import get_db_session
    from app.db.models import MachineStateView

    session = next(get_db_session())
    try:
        db_view_before = session.get(MachineStateView, "EQ-001")
        assert db_view_before is not None
        version_before = db_view_before.state_version

        # 1. As of 08:00:00Z (before recovery at 08:32:05Z, active vibration alerts)
        resp_08 = client.get(
            "/v1/machines/EQ-001/status?as_of=2026-04-18T08:00:00Z",
            headers=READ_HEADERS,
        )
        assert resp_08.status_code == 200
        data_08 = resp_08.json()
        assert data_08["as_of"] == "2026-04-18T08:00:00Z"
        assert data_08["derived_status"] == "CRITICAL"

        # 2. As of 09:00:00Z (after recovery at 08:32:05Z)
        resp_09 = client.get(
            "/v1/machines/EQ-001/status?as_of=2026-04-18T09:00:00Z",
            headers=READ_HEADERS,
        )
        assert resp_09.status_code == 200
        data_09 = resp_09.json()
        assert data_09["as_of"] == "2026-04-18T09:00:00Z"
        assert data_09["derived_status"] == "WATCH"

        # 3. Verify they gave different results
        assert data_08["derived_status"] != data_09["derived_status"]

        # 4. Verify no write to database
        session.expire_all()
        db_view_after = session.get(MachineStateView, "EQ-001")
        assert db_view_after is not None
        assert db_view_after.state_version == version_before
    finally:
        session.close()


def test_recompute_increments_version_and_updates_timestamps():
    """Verify that a new ingest produces recompute jobs, increments state_version, and updates timestamps."""
    import uuid

    from app.core.clock import frozen_time
    from app.db.base import get_db_session
    from app.db.models import Job, MachineStateView, NormalizedSignal, RawRecord
    from app.domain.engine import recompute_machine_state
    from app.workers.handlers import DEFAULT_JOB_HANDLERS
    from app.workers.queue import drain_queue

    test_event_id = f"PF-RECOMP-{uuid.uuid4().hex[:8]}"
    test_now = "2026-04-18T09:30:00Z"

    with frozen_time(test_now) as test_clock:
        session = next(get_db_session())
        try:
            # Baseline state_version
            view_before = session.get(MachineStateView, "EQ-003")
            assert view_before is not None
            initial_version = view_before.state_version

            # Ingest new signal using real PulseForge type RECOVERY_SIGNAL
            envelope = {
                "vendor": "PulseForge",
                "plant_id": "PLANT_01",
                "events": [
                    {
                        "event_id": test_event_id,
                        "machine_id": "EQ-003",
                        "line_id": "LINE-B",
                        "event_time": "2026-04-18T09:15:00Z",
                        "event_type": "RECOVERY_SIGNAL",
                        "severity": "info",
                        "temperature_c": 52.0,
                    }
                ],
            }
            res = client.post("/v1/ingest/pulseforge", json=envelope, headers=INGEST_HEADERS)
            assert res.status_code == 202
            assert res.json()["accepted"] == 1

            # Assert NORMALIZE job exists in queue
            job = (
                session.query(Job)
                .filter(Job.type == "NORMALIZE_PAYLOAD", Job.status == "PENDING")
                .first()
            )
            assert job is not None

            # Drain queue (processes NORMALIZE -> produces RECOMPUTE -> processes RECOMPUTE)
            drained = drain_queue(session, DEFAULT_JOB_HANDLERS, clock=test_clock)
            assert drained >= 2

            # Assert state_version incremented and timestamps updated
            session.expire_all()
            view_after = session.get(MachineStateView, "EQ-003")
            assert view_after is not None
            assert view_after.state_version == initial_version + 1
            assert view_after.last_processed_at is not None
            assert view_after.last_processed_at.isoformat().startswith("2026-04-18T09:30:00")
            assert view_after.latest_relevant_event_time is not None
            assert view_after.latest_relevant_event_time.isoformat().startswith("2026-04-18T09:15:00")
        finally:
            session.query(Job).delete()
            session.query(NormalizedSignal).filter(NormalizedSignal.source_record_id.like("PF-RECOMP-%")).delete()
            session.query(RawRecord).filter(RawRecord.record_id.like("PF-RECOMP-%")).delete()
            session.commit()
            recompute_machine_state("EQ-003", session)
            session.close()


