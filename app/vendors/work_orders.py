"""Internal CMMS Work Orders normalization adapter."""

from datetime import datetime
from typing import Any

from app.core.clock import Clock
from app.vendors.base import (
    NormalizationResult,
    NormalizedSignalData,
)
from app.vendors.enums import (
    CanonicalEventType,
    Dimension,
    SignalKind,
)
from app.vendors.resolver import MachineCatalogEntry, resolve_entities


def normalize_work_order_record(
    record: dict[str, Any],
    envelope: dict[str, Any],
    catalog: dict[str, MachineCatalogEntry],
    clock: Clock,
) -> NormalizationResult:
    """Normalize an internal CMMS work order into NormalizedSignalData."""
    # 1. Critical Identifier Check
    wo_id = record.get("work_order_id")
    if not wo_id or not str(wo_id).strip():
        return NormalizationResult(
            success=False,
            rejection_reasons=["Missing or empty work_order_id."],
        )
    wo_id_str = str(wo_id).strip()

    # 2. Entity Resolution
    raw_machine_id = record.get("machine_id")
    raw_line_id = record.get("line_id")
    envelope_plant_id = record.get("plant_id") or envelope.get("plant_id")

    res = resolve_entities(raw_machine_id, envelope_plant_id, raw_line_id, catalog)
    if not res.is_valid:
        return NormalizationResult(
            success=False,
            rejection_reasons=res.rejection_reasons,
            resolved_machine_id=res.resolved_machine_id,
        )

    assert res.resolved_machine_id is not None
    assert res.resolved_plant_id is not None
    assert res.resolved_line_id is not None

    flags: list[str] = list(res.flags)

    # 3. Timestamp Validation (opened_at)
    raw_opened_at = record.get("opened_at")
    if not raw_opened_at or not isinstance(raw_opened_at, str):
        return NormalizationResult(
            success=False,
            rejection_reasons=["Missing opened_at timestamp; must be an ISO-8601 string."],
            resolved_machine_id=res.resolved_machine_id,
        )

    time_str = raw_opened_at.strip()
    try:
        if not time_str.endswith("Z") and "+00:00" not in time_str:
            raise ValueError("Work order opened_at must specify UTC timezone (Z or +00:00).")
        opened_at_dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    except Exception as exc:
        return NormalizationResult(
            success=False,
            rejection_reasons=[f"Unparsable opened_at '{time_str}': {exc}"],
            resolved_machine_id=res.resolved_machine_id,
        )

    # Optional closed_at
    closed_at_dt: datetime | None = None
    raw_closed_at = record.get("closed_at")
    if raw_closed_at and isinstance(raw_closed_at, str) and raw_closed_at.strip():
        try:
            c_str = raw_closed_at.strip()
            closed_at_dt = datetime.fromisoformat(c_str.replace("Z", "+00:00"))
        except Exception:
            closed_at_dt = None

    status_str = str(record.get("status", "open")).strip().lower()
    post_check = (
        str(record.get("post_maintenance_check", "")).strip().lower()
        if record.get("post_maintenance_check")
        else None
    )

    # Severity: Open work orders contribute attention 1; closed contribute 0
    severity = 1 if status_str == "open" else 0

    signal = NormalizedSignalData(
        source_vendor="CMMS",
        source_record_id=wo_id_str,
        machine_id=res.resolved_machine_id,
        plant_id=res.resolved_plant_id,
        line_id=res.resolved_line_id,
        signal_kind=SignalKind.WORK_ORDER,
        canonical_event_type=CanonicalEventType.WORK_ORDER,
        dimension=Dimension.MAINTENANCE,
        severity=severity,
        event_time=opened_at_dt,
        ingested_at=clock.now(),
        vibration_mm_s=None,
        vibration_g_raw=None,
        temperature_c=None,
        power_kw=None,
        machine_state=None,
        sensor_quality=None,
        vendor_confidence=None,
        details={
            "opened_at": opened_at_dt.isoformat(),
            "closed_at": closed_at_dt.isoformat() if closed_at_dt else None,
            "status": status_str,
            "maintenance_type": record.get("maintenance_type"),
            "trigger_source": record.get("trigger_source"),
            "downtime_minutes": record.get("downtime_minutes"),
            "post_maintenance_check": post_check,
            "notes": record.get("notes"),
        },
        data_quality_flags=sorted(set(flags)),
    )

    return NormalizationResult(
        success=True,
        signal=signal,
        resolved_machine_id=res.resolved_machine_id,
    )
