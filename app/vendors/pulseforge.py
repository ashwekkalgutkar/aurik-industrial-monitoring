"""PulseForge vendor payload normalization adapter."""

from datetime import datetime
from typing import Any

from app.core.clock import Clock
from app.vendors.base import (
    NormalizationResult,
    NormalizedSignalData,
    sanitize_float,
)
from app.vendors.enums import (
    FLAG_INVALID_SEVERITY,
    FLAG_INVALID_VALUE,
    FLAG_UNMAPPED_ALERT_CODE,
    PULSEFORGE_SEVERITY_MAP,
    CanonicalEventType,
    Dimension,
    SignalKind,
)
from app.vendors.resolver import MachineCatalogEntry, resolve_entities


def normalize_pulseforge_record(
    record: dict[str, Any],
    envelope: dict[str, Any],
    catalog: dict[str, MachineCatalogEntry],
    clock: Clock,
) -> NormalizationResult:
    """Normalize a single PulseForge event into a canonical NormalizedSignalData."""
    # 1. Critical Record Identifier Check
    event_id = record.get("event_id")
    if not event_id or not str(event_id).strip():
        return NormalizationResult(
            success=False,
            rejection_reasons=["Missing or empty event_id."],
        )
    event_id_str = str(event_id).strip()

    # 2. Entity Resolution
    raw_machine_id = record.get("machine_id")
    raw_line_id = record.get("line_id")
    envelope_plant_id = envelope.get("plant_id")

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

    # 3. Timestamp Validation (Strict ISO-8601 UTC)
    raw_event_time = record.get("event_time")
    if not raw_event_time or not isinstance(raw_event_time, str):
        return NormalizationResult(
            success=False,
            rejection_reasons=["Missing or invalid event_time; must be an ISO-8601 string."],
            resolved_machine_id=res.resolved_machine_id,
        )

    time_str = raw_event_time.strip()
    try:
        if not time_str.endswith("Z") and "+00:00" not in time_str:
            raise ValueError("PulseForge event_time must specify UTC timezone (Z or +00:00).")
        event_time_dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    except Exception as exc:
        return NormalizationResult(
            success=False,
            rejection_reasons=[f"Unparsable event_time '{time_str}': {exc}"],
            resolved_machine_id=res.resolved_machine_id,
        )

    # 4. Event Type & Dimension Mapping
    raw_event_type = str(record.get("event_type", "")).strip().upper()
    signal_kind: SignalKind = SignalKind.ALERT
    canonical_type: CanonicalEventType
    dimension: Dimension

    if raw_event_type == "HIGH_VIBRATION":
        canonical_type = CanonicalEventType.VIBRATION_HIGH
        dimension = Dimension.VIBRATION
    elif raw_event_type == "TEMP_SPIKE":
        canonical_type = CanonicalEventType.TEMPERATURE_HIGH
        dimension = Dimension.TEMPERATURE
    elif raw_event_type == "POWER_FLUCTUATION":
        canonical_type = CanonicalEventType.POWER_ANOMALY
        dimension = Dimension.POWER
    elif raw_event_type == "SENSOR_HEALTH_DROP":
        canonical_type = CanonicalEventType.SENSOR_DEGRADED
        dimension = Dimension.SENSOR
    elif raw_event_type == "RECOVERY_SIGNAL":
        canonical_type = CanonicalEventType.NOMINAL
        dimension = Dimension.NONE
        signal_kind = SignalKind.TELEMETRY
    else:
        canonical_type = CanonicalEventType.UNKNOWN_EVENT
        dimension = Dimension.NONE
        flags.append(FLAG_UNMAPPED_ALERT_CODE)

    # 5. Severity Mapping (0-4)
    severity: int | None
    if raw_event_type == "RECOVERY_SIGNAL":
        severity = 0
    else:
        raw_sev = str(record.get("severity", "")).strip().lower()
        if raw_sev in PULSEFORGE_SEVERITY_MAP:
            severity = PULSEFORGE_SEVERITY_MAP[raw_sev]
        else:
            severity = None
            flags.append(FLAG_INVALID_SEVERITY)

    # 6. Physical Measurements & Sanity Checks
    vib_val, vib_invalid = sanitize_float(record.get("vibration_mm_s"), min_val=0.0)
    if vib_invalid:
        flags.append(FLAG_INVALID_VALUE)

    temp_val, temp_invalid = sanitize_float(
        record.get("temperature_c"), min_val=-50.0, max_val=250.0, sentinels={-999.0}
    )
    if temp_invalid:
        flags.append(FLAG_INVALID_VALUE)

    health_val, health_invalid = sanitize_float(
        record.get("sensor_health"), min_val=0.0, max_val=1.0
    )
    if health_invalid:
        flags.append(FLAG_INVALID_VALUE)

    conf_val, conf_invalid = sanitize_float(
        record.get("vendor_confidence"), min_val=0.0, max_val=1.0
    )
    if conf_invalid:
        flags.append(FLAG_INVALID_VALUE)

    machine_state = record.get("machine_state")
    state_str = str(machine_state).strip().lower() if machine_state is not None else None

    # Construct Normalized Signal
    signal = NormalizedSignalData(
        source_vendor="PulseForge",
        source_record_id=event_id_str,
        machine_id=res.resolved_machine_id,
        plant_id=res.resolved_plant_id,
        line_id=res.resolved_line_id,
        signal_kind=signal_kind,
        canonical_event_type=canonical_type,
        dimension=dimension,
        severity=severity,
        event_time=event_time_dt,
        ingested_at=clock.now(),
        vibration_mm_s=vib_val,
        vibration_g_raw=None,
        temperature_c=temp_val,
        power_kw=None,
        machine_state=state_str,
        sensor_quality=health_val,
        vendor_confidence=conf_val,
        details={
            "raw_event_type": raw_event_type,
            "raw_severity": record.get("severity"),
            "batch_generated_at": envelope.get("batch_generated_at"),
        },
        data_quality_flags=sorted(set(flags)),
    )

    return NormalizationResult(
        success=True,
        signal=signal,
        resolved_machine_id=res.resolved_machine_id,
    )
