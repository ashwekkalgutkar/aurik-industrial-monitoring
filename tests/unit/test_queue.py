"""Unit and integration tests for PostgreSQL jobs queue, worker, retry backoff, and Layer 2 deduplication."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock
from app.db.base import get_db_session
from app.db.models import Job, NormalizedSignal, RawRecord
from app.db.reference_loader import load_reference_data
from app.services.ingest_service import IngestionService
from app.workers.handlers import DEFAULT_JOB_HANDLERS
from app.workers.queue import PostgresJobQueue, drain_queue


@pytest.fixture(autouse=True)
def clean_db():
    session = next(get_db_session())
    try:
        session.query(Job).delete()
        session.query(NormalizedSignal).delete()
        session.query(RawRecord).delete()
        load_reference_data(session)
        session.commit()
    finally:
        session.close()
    yield


def test_job_claim_and_completion():
    session = next(get_db_session())
    clock = FrozenClock("2026-04-18T08:00:00Z")
    queue = PostgresJobQueue(session, clock)

    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        type="TEST_JOB",
        payload={"foo": "bar"},
        dedupe_key="TEST:1",
        status="PENDING",
        attempts=0,
        max_attempts=3,
        run_after=clock.now(),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    session.add(job)
    session.commit()

    # Claim job
    claimed = queue.claim_job(lease_seconds=30)
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status == "RUNNING"
    assert claimed.attempts == 1
    assert claimed.locked_until == clock.now() + timedelta(seconds=30)

    # Complete job
    queue.complete_job(job_id)
    reloaded = session.get(Job, job_id)
    assert reloaded is not None
    assert reloaded.status == "SUCCEEDED"
    assert reloaded.locked_until is None


def test_job_retry_exponential_backoff_and_dead_letter():
    session = next(get_db_session())
    clock = FrozenClock("2026-04-18T08:00:00Z")
    queue = PostgresJobQueue(session, clock)

    job_id = str(uuid.uuid4())
    job = Job(
        id=job_id,
        type="TEST_JOB",
        payload={},
        status="PENDING",
        attempts=0,
        max_attempts=2,  # Max 2 attempts
        run_after=clock.now(),
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    session.add(job)
    session.commit()

    # Claim attempt 1
    c1 = queue.claim_job(lease_seconds=30)
    assert c1 is not None

    # Fail attempt 1 (retryable)
    queue.fail_job(job_id, "Database connection timeout", is_retryable=True)
    j1 = session.get(Job, job_id)
    assert j1 is not None
    assert j1.status == "FAILED_RETRYABLE"
    assert j1.attempts == 1
    assert j1.run_after > clock.now()  # Backoff delay scheduled
    assert j1.last_error == "Database connection timeout"

    # Advance clock past backoff time
    clock.set_time(j1.run_after + timedelta(seconds=1))

    # Claim attempt 2
    c2 = queue.claim_job(lease_seconds=30)
    assert c2 is not None
    assert c2.attempts == 2

    # Fail attempt 2 (exceeds max_attempts)
    queue.fail_job(job_id, "Persistent timeout", is_retryable=True)
    j2 = session.get(Job, job_id)
    assert j2 is not None
    assert j2.status == "DEAD"
    assert j2.last_error == "Persistent timeout"

    # Requeue via admin function
    success = queue.requeue_job(job_id)
    assert success is True
    requeued = session.get(Job, job_id)
    assert requeued is not None
    assert requeued.status == "PENDING"
    assert requeued.attempts == 0
    assert requeued.last_error is None


def test_lease_reclaim_crash_safety():
    """Verify that a job left in RUNNING with an expired lease is reclaimed by another worker."""
    session = next(get_db_session())
    clock = FrozenClock("2026-04-18T08:00:00Z")
    queue = PostgresJobQueue(session, clock)

    job_id = str(uuid.uuid4())
    # Worker died at 07:55:00 leaving locked_until at 07:56:00
    job = Job(
        id=job_id,
        type="TEST_JOB",
        payload={},
        status="RUNNING",
        attempts=1,
        max_attempts=3,
        run_after=datetime(2026, 4, 18, 7, 50, tzinfo=UTC),
        locked_until=datetime(2026, 4, 18, 7, 56, tzinfo=UTC),
        created_at=datetime(2026, 4, 18, 7, 50, tzinfo=UTC),
        updated_at=datetime(2026, 4, 18, 7, 50, tzinfo=UTC),
    )
    session.add(job)
    session.commit()

    # New worker running at 08:00:00 claims it
    reclaimed = queue.claim_job(lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed.id == job_id
    assert reclaimed.status == "RUNNING"
    assert reclaimed.attempts == 2  # Re-attempted
    assert reclaimed.locked_until == clock.now() + timedelta(seconds=30)


def test_layer_2_semantic_dedupe_tw_8801_r():
    """Verify that TW-8801-R with distinct ID but identical measurements is marked DUPLICATE_CONTENT with duplicate_of set."""
    session = next(get_db_session())
    clock = FrozenClock("2026-04-18T08:25:00Z")
    service = IngestionService(session, clock)

    envelope = {
        "source": "ThermexWatch",
        "site_code": "PLANT_01",
        "response_time_epoch_ms": 1776500500000,
        "readings": [
            {
                "readingId": "TW-8801",
                "assetCode": "EQ-001",
                "productionLine": "A",
                "timestampMs": 1776499152000,
                "alertCode": "VIB_WARN",
                "level": 4,
                "vibration_g": 0.81,
                "temperature_f": 181.2,
                "power_kw": 37.8,
                "is_active": True,
                "signal_quality": "GOOD",
            },
            {
                "readingId": "TW-8801-R",  # Different ID, identical content
                "assetCode": "EQ-001",
                "productionLine": "A",
                "timestampMs": 1776499152000,
                "alertCode": "VIB_WARN",
                "level": 4,
                "vibration_g": 0.81,
                "temperature_f": 181.2,
                "power_kw": 37.8,
                "is_active": True,
                "signal_quality": "GOOD",
            },
        ],
    }

    resp = service.ingest_payload(
        vendor="ThermexWatch",
        envelope=envelope,
        record_array_key="readings",
        record_id_key="readingId",
        machine_id_key="assetCode",
        required_envelope_keys=["source", "site_code", "readings"],
    )
    assert resp.accepted == 2

    # Drain queue to execute NORMALIZE_PAYLOAD
    executed = drain_queue(session, DEFAULT_JOB_HANDLERS, clock=clock)
    assert executed >= 2

    # Inspect TW-8801
    rec1 = session.query(RawRecord).filter_by(record_id="TW-8801").first()
    assert rec1 is not None
    assert rec1.processing_state == "PROCESSED"

    sig1 = session.query(NormalizedSignal).filter_by(source_record_id="TW-8801").first()
    assert sig1 is not None
    assert sig1.is_effective is True
    assert sig1.duplicate_of is None

    # Inspect TW-8801-R
    rec2 = session.query(RawRecord).filter_by(record_id="TW-8801-R").first()
    assert rec2 is not None
    assert rec2.processing_state == "DUPLICATE_CONTENT"

    sig2 = session.query(NormalizedSignal).filter_by(source_record_id="TW-8801-R").first()
    assert sig2 is not None
    assert sig2.is_effective is False  # Excluded from engine
    assert sig2.duplicate_of == sig1.id


def test_permanent_validation_errors_no_retries():
    """Verify that records with unparsable timestamps fail permanently straight to REJECTED_INVALID with 0 retries."""
    session = next(get_db_session())
    clock = FrozenClock("2026-04-18T08:50:00Z")
    service = IngestionService(session, clock)

    envelope = {
        "vendor": "PulseForge",
        "plant_id": "PLANT_02",
        "events": [
            {
                "event_id": "PF-1502",
                "machine_id": "EQ-005",
                "line_id": None,
                "event_time": "bad-time",
                "event_type": "TEMP_SPIKE",
                "severity": "critical",
            }
        ],
    }

    resp = service.ingest_payload(
        vendor="PulseForge",
        envelope=envelope,
        record_array_key="events",
        record_id_key="event_id",
        machine_id_key="machine_id",
        required_envelope_keys=["vendor", "plant_id", "events"],
    )
    assert resp.accepted == 1

    # Drain worker queue
    executed = drain_queue(session, DEFAULT_JOB_HANDLERS, clock=clock)
    assert executed >= 1

    raw_rec = session.query(RawRecord).filter_by(record_id="PF-1502").first()
    assert raw_rec is not None
    assert raw_rec.processing_state == "REJECTED_INVALID"
    assert raw_rec.resolved_machine_id == "EQ-005"
    assert any("event_time" in r.lower() for r in raw_rec.rejection_reasons)

    # Verify job completed SUCCEEDED (it processed the validation rejection, didn't retry or fail to DEAD)
    job = (
        session.query(Job)
        .filter(Job.type == "NORMALIZE_PAYLOAD", Job.dedupe_key.like("%PF-1502%"))
        .first()
    )
    assert job is not None
    assert job.status == "SUCCEEDED"
    assert job.attempts == 1
