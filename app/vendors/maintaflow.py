"""MaintaFlow vendor payload normalization adapter."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.clock import Clock
from app.core.config import get_settings
from app.vendors.base import (
    NormalizationResult,
    NormalizedSignalData,
)
from app.vendors.enums import (
    FLAG_ASSUMED_TIMEZONE,
    FLAG_UNMAPPED_ALERT_CODE,
    MAINTAFLOW_CONFIDENCE_MAP,
    CanonicalEventType,
    Dimension,
    SignalKind,
)
from app.vendors.resolver import MachineCatalogEntry, resolve_entities


def normalize_maintaflow_record(
    record: dict[str, Any],
    envelope: dict[str, Any],
    catalog: dict[str, MachineCatalogEntry],
    clock: Clock,
    assumed_tz_name: str = "UTC",
) -> NormalizationResult:
    """Normalize a single MaintaFlow inspection/maintenance record into NormalizedSignalData."""
    settings = get_settings()

    # 1. Critical Record Identifier Check
    record_id = record.get("record_id")
    if not record_id or not str(record_id).strip():
        return NormalizationResult(
            success=False,
            rejection_reasons=["Missing or empty record_id."],
        )
    record_id_str = str(record_id).strip()

    # 2. Entity Resolution
    raw_machine_id = record.get("machine_ref")
    raw_line_id = record.get("line_ref")
    envelope_factory_id = envelope.get("factory_id")

    res = resolve_entities(raw_machine_id, envelope_factory_id, raw_line_id, catalog)
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

    # 3. Timestamp Validation (Format "%Y/%m/%d %H:%M:%S", naive -> localized to assumed TZ)
    raw_recorded_at = record.get("recorded_at")
    if not raw_recorded_at or not isinstance(raw_recorded_at, str):
        return NormalizationResult(
            success=False,
            rejection_reasons=["Missing recorded_at timestamp."],
            resolved_machine_id=res.resolved_machine_id,
        )

    time_str = raw_recorded_at.strip()
    try:
        naive_dt = datetime.strptime(time_str, "%Y/%m/%d %H:%M:%S")
        tz = ZoneInfo(assumed_tz_name)
        event_time_dt = naive_dt.replace(tzinfo=tz).astimezone(UTC)
        flags.append(FLAG_ASSUMED_TIMEZONE)
    except Exception as exc:
        return NormalizationResult(
            success=False,
            rejection_reasons=[f"Unparsable recorded_at '{time_str}': {exc}"],
            resolved_machine_id=res.resolved_machine_id,
        )

    # 4. Record Type & Canonical Taxonomy
    raw_record_type = str(record.get("record_type", "")).strip().lower()
    signal_kind: SignalKind
    canonical_type: CanonicalEventType
    dimension: Dimension = Dimension.MAINTENANCE

    if raw_record_type == "inspection":
        canonical_type = CanonicalEventType.INSPECTION
        signal_kind = SignalKind.INSPECTION
    elif raw_record_type == "maintenance_update":
        canonical_type = CanonicalEventType.MAINTENANCE
        signal_kind = SignalKind.MAINTENANCE
    elif raw_record_type == "operator_note":
        canonical_type = CanonicalEventType.OPERATOR_NOTE
        signal_kind = SignalKind.OPERATOR_NOTE
    elif raw_record_type == "calibration":
        canonical_type = CanonicalEventType.CALIBRATION
        signal_kind = SignalKind.CALIBRATION
    else:
        canonical_type = CanonicalEventType.UNKNOWN_EVENT
        signal_kind = SignalKind.TELEMETRY
        flags.append(FLAG_UNMAPPED_ALERT_CODE)

    # 5. Derived Severity Calculation (Deterministic Domain Rules)
    # major_defect_found=3, overdue=2, minor_defect_found=1, calibration_pending=1, due_soon=1, passed_with_observation=1
    inspection_res = (
        str(record.get("inspection_result", "")).strip().lower()
        if record.get("inspection_result")
        else None
    )
    maint_status = (
        str(record.get("maintenance_status", "")).strip().lower()
        if record.get("maintenance_status")
        else None
    )
    note_text = str(record.get("technician_note", "")).strip().lower()

    severity: int = 0
    matched_concern_keyword: str | None = None

    if inspection_res == "major_defect_found":
        severity = max(severity, 3)
    elif maint_status == "overdue":
        severity = max(severity, 2)
    elif inspection_res in ("minor_defect_found", "passed_with_observation", "calibration_pending"):
        severity = max(severity, 1)
    elif maint_status == "due_soon":
        severity = max(severity, 1)

    # Operator notes keyword matching
    if raw_record_type == "operator_note" and note_text:
        for kw in settings.OPERATOR_NOTE_CONCERN_KEYWORDS:
            if kw in note_text:
                severity = max(severity, 1)
                matched_concern_keyword = kw
                break

    # 6. Confidence Mapping
    raw_confidence = str(record.get("manual_confidence", "")).strip().lower()
    vendor_confidence = MAINTAFLOW_CONFIDENCE_MAP.get(raw_confidence)

    # 7. Days since last service
    days_since: int | None = None
    raw_days = record.get("days_since_last_service")
    if raw_days is not None and str(raw_days).strip() != "":
        try:
            days_since = int(float(str(raw_days).strip()))
        except (ValueError, TypeError):
            days_since = None

    signal = NormalizedSignalData(
        source_vendor="MaintaFlow",
        source_record_id=record_id_str,
        machine_id=res.resolved_machine_id,
        plant_id=res.resolved_plant_id,
        line_id=res.resolved_line_id,
        signal_kind=signal_kind,
        canonical_event_type=canonical_type,
        dimension=dimension,
        severity=severity,
        event_time=event_time_dt,
        ingested_at=clock.now(),
        vibration_mm_s=None,
        vibration_g_raw=None,
        temperature_c=None,
        power_kw=None,
        machine_state=None,
        sensor_quality=None,
        vendor_confidence=vendor_confidence,
        details={
            "raw_record_type": raw_record_type,
            "inspection_result": inspection_res,
            "maintenance_status": maint_status,
            "days_since_last_service": days_since,
            "technician_note": record.get("technician_note"),
            "manual_confidence": raw_confidence,
            "matched_concern_keyword": matched_concern_keyword,
        },
        data_quality_flags=sorted(set(flags)),
    )

    return NormalizationResult(
        success=True,
        signal=signal,
        resolved_machine_id=res.resolved_machine_id,
    )
