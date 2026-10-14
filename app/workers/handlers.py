"""Asynchronous worker job handlers for normalization and machine recomputation."""

import math
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import get_clock
from app.core.config import get_settings
from app.db.models import Job, Machine, NormalizedSignal, RawRecord
from app.vendors.base import NormalizationResult, NormalizedSignalData
from app.vendors.maintaflow import normalize_maintaflow_record
from app.vendors.pulseforge import normalize_pulseforge_record
from app.vendors.resolver import MachineCatalogEntry
from app.vendors.thermexwatch import normalize_thermexwatch_record
from app.vendors.work_orders import normalize_work_order_record
from app.workers.queue import PostgresJobQueue


def _measurements_match(sig1: NormalizedSignalData, sig2: NormalizedSignal) -> bool:
    """Check if physical measurements of two signals match within precision tolerances."""

    def _floats_eq(f1: float | None, f2: float | None) -> bool:
        if f1 is None and f2 is None:
            return True
        if f1 is None or f2 is None:
            return False
        return math.isclose(f1, f2, rel_tol=1e-3, abs_tol=1e-3)

    return (
        _floats_eq(sig1.vibration_mm_s, sig2.vibration_mm_s)
        and _floats_eq(sig1.vibration_g_raw, sig2.vibration_g_raw)
        and _floats_eq(sig1.temperature_c, sig2.temperature_c)
        and _floats_eq(sig1.power_kw, sig2.power_kw)
        and sig1.machine_state == sig2.machine_state
    )


def handle_normalize_payload(job: Job, session: Session) -> None:
    """Normalize a raw record payload, apply Layer 2 deduplication, and enqueue machine recompute."""
    clock = get_clock()
    settings = get_settings()

    raw_record_id = job.payload.get("raw_record_id")
    if not raw_record_id:
        return

    raw_rec = session.get(RawRecord, raw_record_id)
    if not raw_rec or raw_rec.processing_state in ("SUPERSEDED", "PROCESSED"):
        return

    # Build authoritative machine catalog from DB
    machines = session.query(Machine).all()
    catalog = {
        m.machine_id: MachineCatalogEntry(
            machine_id=m.machine_id,
            plant_id=m.plant_id,
            line_id=m.line_id,
        )
        for m in machines
    }

    # Dispatch to vendor adapter
    result: NormalizationResult
    vendor = raw_rec.vendor

    if vendor == "PulseForge":
        result = normalize_pulseforge_record(raw_rec.payload, raw_rec.envelope_meta, catalog, clock)
    elif vendor == "ThermexWatch":
        result = normalize_thermexwatch_record(
            raw_rec.payload, raw_rec.envelope_meta, catalog, clock
        )
    elif vendor == "MaintaFlow":
        result = normalize_maintaflow_record(
            raw_rec.payload,
            raw_rec.envelope_meta,
            catalog,
            clock,
            assumed_tz_name=settings.DEFAULT_ASSUMED_TIMEZONE,
        )
    elif vendor == "CMMS":
        result = normalize_work_order_record(raw_rec.payload, raw_rec.envelope_meta, catalog, clock)
    else:
        raw_rec.processing_state = "REJECTED_INVALID"
        raw_rec.rejection_reasons = [f"Unsupported vendor '{vendor}'."]
        session.commit()
        return

    # Handle Validation Failure (Permanent Error: no retries, raw kept)
    if not result.success:
        raw_rec.processing_state = "REJECTED_INVALID"
        raw_rec.rejection_reasons = result.rejection_reasons
        raw_rec.resolved_machine_id = result.resolved_machine_id
        session.commit()

        # If a machine was resolvable, trigger recompute so its processing_status becomes PARTIAL
        if result.resolved_machine_id:
            queue = PostgresJobQueue(session, clock)
            queue.enqueue_recompute(result.resolved_machine_id)
        return

    sig_data = result.signal
    assert sig_data is not None
    raw_rec.resolved_machine_id = sig_data.machine_id

    # Layer 2 Semantic Deduplication (detect content duplicates like TW-8801-R)
    candidates = (
        session.query(NormalizedSignal)
        .filter(
            NormalizedSignal.source_vendor == sig_data.source_vendor,
            NormalizedSignal.machine_id == sig_data.machine_id,
            NormalizedSignal.event_time == sig_data.event_time,
            NormalizedSignal.canonical_event_type == sig_data.canonical_event_type,
        )
        .all()
    )

    duplicate_match = next((c for c in candidates if _measurements_match(sig_data, c)), None)

    if duplicate_match:
        # Content duplicate: earliest-ingested wins, mark is_effective=False
        raw_rec.processing_state = "DUPLICATE_CONTENT"
        signal_orm = NormalizedSignal(
            id=str(uuid.uuid4()),
            raw_record_id=raw_rec.id,
            source_vendor=sig_data.source_vendor,
            source_record_id=sig_data.source_record_id,
            machine_id=sig_data.machine_id,
            plant_id=sig_data.plant_id,
            line_id=sig_data.line_id,
            signal_kind=sig_data.signal_kind,
            canonical_event_type=sig_data.canonical_event_type,
            dimension=sig_data.dimension,
            severity=sig_data.severity,
            event_time=sig_data.event_time,
            ingested_at=sig_data.ingested_at,
            vibration_mm_s=sig_data.vibration_mm_s,
            vibration_g_raw=sig_data.vibration_g_raw,
            temperature_c=sig_data.temperature_c,
            power_kw=sig_data.power_kw,
            machine_state=sig_data.machine_state,
            sensor_quality=sig_data.sensor_quality,
            vendor_confidence=sig_data.vendor_confidence,
            details=sig_data.details,
            data_quality_flags=sig_data.data_quality_flags,
            duplicate_of=duplicate_match.id,
            is_effective=False,  # Exclude from engine computation
        )
        session.add(signal_orm)
        session.commit()
    else:
        # Effective unique signal
        raw_rec.processing_state = "PROCESSED"
        signal_orm = NormalizedSignal(
            id=str(uuid.uuid4()),
            raw_record_id=raw_rec.id,
            source_vendor=sig_data.source_vendor,
            source_record_id=sig_data.source_record_id,
            machine_id=sig_data.machine_id,
            plant_id=sig_data.plant_id,
            line_id=sig_data.line_id,
            signal_kind=sig_data.signal_kind,
            canonical_event_type=sig_data.canonical_event_type,
            dimension=sig_data.dimension,
            severity=sig_data.severity,
            event_time=sig_data.event_time,
            ingested_at=sig_data.ingested_at,
            vibration_mm_s=sig_data.vibration_mm_s,
            vibration_g_raw=sig_data.vibration_g_raw,
            temperature_c=sig_data.temperature_c,
            power_kw=sig_data.power_kw,
            machine_state=sig_data.machine_state,
            sensor_quality=sig_data.sensor_quality,
            vendor_confidence=sig_data.vendor_confidence,
            details=sig_data.details,
            data_quality_flags=sig_data.data_quality_flags,
            duplicate_of=None,
            is_effective=True,
        )
        session.add(signal_orm)
        session.commit()

        # Enqueue machine status recomputation
        queue = PostgresJobQueue(session, clock)
        queue.enqueue_recompute(sig_data.machine_id)


def handle_recompute_machine(job: Job, session: Session) -> None:
    """Recompute machine operational state using the deterministic pure engine."""
    machine_id = job.payload.get("machine_id")
    if not machine_id:
        return

    # In Phase 6, we import and invoke the pure engine here
    from app.domain.engine import recompute_machine_state

    recompute_machine_state(machine_id, session)


DEFAULT_JOB_HANDLERS: dict[str, Any] = {
    "NORMALIZE_PAYLOAD": handle_normalize_payload,
    "RECOMPUTE_MACHINE": handle_recompute_machine,
}
