"""ThermexWatch vendor payload normalization adapter."""

from datetime import UTC, datetime
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
    THERMEXWATCH_QUALITY_MAP,
    CanonicalEventType,
    Dimension,
    SignalKind,
    fahrenheit_to_celsius,
)
from app.vendors.resolver import MachineCatalogEntry, resolve_entities


def normalize_thermexwatch_record(
    record: dict[str, Any],
    envelope: dict[str, Any],
    catalog: dict[str, MachineCatalogEntry],
    clock: Clock,
) -> NormalizationResult:
    """Normalize a single ThermexWatch reading into a canonical NormalizedSignalData."""
    # 1. Critical Record Identifier Check
    reading_id = record.get("readingId")
    if not reading_id or not str(reading_id).strip():
        return NormalizationResult(
            success=False,
            rejection_reasons=["Missing or empty readingId."],
        )
    reading_id_str = str(reading_id).strip()

    # 2. Entity Resolution
    raw_machine_id = record.get("assetCode")
    raw_line_id = record.get("productionLine")
    envelope_site_code = envelope.get("site_code")

    res = resolve_entities(raw_machine_id, envelope_site_code, raw_line_id, catalog)
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

    # 3. Timestamp Validation (Epoch Milliseconds)
    raw_timestamp = record.get("timestampMs")
    if raw_timestamp is None:
        return NormalizationResult(
            success=False,
            rejection_reasons=["Missing timestampMs."],
            resolved_machine_id=res.resolved_machine_id,
        )

    try:
        ts_ms = int(raw_timestamp)
        if ts_ms <= 0:
            raise ValueError("Timestamp milliseconds must be positive.")
        event_time_dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
    except Exception as exc:
        return NormalizationResult(
            success=False,
            rejection_reasons=[f"Unparsable timestampMs '{raw_timestamp}': {exc}"],
            resolved_machine_id=res.resolved_machine_id,
        )

    # 4. Alert Code & Dimension Mapping
    raw_alert_code = str(record.get("alertCode", "")).strip().upper()
    signal_kind: SignalKind = SignalKind.ALERT
    canonical_type: CanonicalEventType
    dimension: Dimension

    if raw_alert_code == "VIB_WARN":
        canonical_type = CanonicalEventType.VIBRATION_HIGH
        dimension = Dimension.VIBRATION
    elif raw_alert_code == "TEMP_WARN":
        canonical_type = CanonicalEventType.TEMPERATURE_HIGH
        dimension = Dimension.TEMPERATURE
    elif raw_alert_code == "TEMP_CRIT":
        canonical_type = CanonicalEventType.TEMPERATURE_CRITICAL
        dimension = Dimension.TEMPERATURE
    elif raw_alert_code == "POWER_DROP":
        canonical_type = CanonicalEventType.POWER_ANOMALY
        dimension = Dimension.POWER
    elif raw_alert_code == "OK":
        canonical_type = CanonicalEventType.NOMINAL
        dimension = Dimension.NONE
        signal_kind = SignalKind.TELEMETRY
    else:
        canonical_type = CanonicalEventType.UNKNOWN_EVENT
        dimension = Dimension.NONE
        flags.append(FLAG_UNMAPPED_ALERT_CODE)

    # 5. Severity Mapping (Level 1-5 -> Severity 0-4)
    raw_level = record.get("level")
    severity: int | None
    try:
        level_int = int(raw_level)  # type: ignore
        if 1 <= level_int <= 5:
            severity = level_int - 1
        else:
            severity = None
            flags.append(FLAG_INVALID_SEVERITY)
    except (ValueError, TypeError):
        severity = None
        flags.append(FLAG_INVALID_SEVERITY)

    # 6. Physical Measurements & Conversions
    # Vibration in Gs: store vibration_g_raw; vibration_mm_s remains NULL
    vib_g_val, vib_g_invalid = sanitize_float(record.get("vibration_g"), min_val=0.0)
    if vib_g_invalid:
        flags.append(FLAG_INVALID_VALUE)

    # Temperature in Fahrenheit -> convert to Celsius
    temp_f_val, temp_f_invalid = sanitize_float(
        record.get("temperature_f"), min_val=-60.0, max_val=500.0, sentinels={-999.0}
    )
    temp_c_val: float | None = None
    if temp_f_invalid:
        flags.append(FLAG_INVALID_VALUE)
    elif temp_f_val is not None:
        temp_c_val = fahrenheit_to_celsius(temp_f_val)

    # Power in kW
    power_val, power_invalid = sanitize_float(record.get("power_kw"), min_val=0.0)
    if power_invalid:
        flags.append(FLAG_INVALID_VALUE)

    # Signal Quality -> Sensor Quality
    raw_quality = str(record.get("signal_quality", "")).strip().upper()
    sensor_quality: float | None = THERMEXWATCH_QUALITY_MAP.get(raw_quality)
    if sensor_quality is None and raw_quality:
        flags.append(FLAG_INVALID_VALUE)

    # Machine State from is_active
    # False means machine idle (not alert cleared); True means running
    is_active_raw = record.get("is_active")
    machine_state = "running"
    if is_active_raw is False or str(is_active_raw).strip().lower() == "false":
        machine_state = "idle"

    signal = NormalizedSignalData(
        source_vendor="ThermexWatch",
        source_record_id=reading_id_str,
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
        vibration_g_raw=vib_g_val,
        temperature_c=temp_c_val,
        power_kw=power_val,
        machine_state=machine_state,
        sensor_quality=sensor_quality,
        vendor_confidence=None,
        details={
            "raw_alert_code": raw_alert_code,
            "raw_level": raw_level,
            "temperature_f": temp_f_val,
            "is_active": is_active_raw,
            "signal_quality": raw_quality,
            "response_time_epoch_ms": envelope.get("response_time_epoch_ms"),
        },
        data_quality_flags=sorted(set(flags)),
    )

    return NormalizationResult(
        success=True,
        signal=signal,
        resolved_machine_id=res.resolved_machine_id,
    )
