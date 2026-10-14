"""Unit tests for all vendor normalization adapters and canonical mapping rules."""

from datetime import UTC, datetime

import pytest

from app.core.clock import FrozenClock
from app.vendors.base import sanitize_float
from app.vendors.enums import (
    FLAG_ASSUMED_TIMEZONE,
    FLAG_INVALID_SEVERITY,
    FLAG_INVALID_VALUE,
    FLAG_LINE_MISMATCH_RESOLVED,
    FLAG_MISSING_LINE_RESOLVED,
    FLAG_PLANT_MISMATCH_RESOLVED,
    FLAG_UNMAPPED_ALERT_CODE,
    CanonicalEventType,
    Dimension,
    SignalKind,
    fahrenheit_to_celsius,
)
from app.vendors.maintaflow import normalize_maintaflow_record
from app.vendors.pulseforge import normalize_pulseforge_record
from app.vendors.resolver import MachineCatalogEntry, resolve_entities
from app.vendors.thermexwatch import normalize_thermexwatch_record
from app.vendors.work_orders import normalize_work_order_record

MOCK_CATALOG: dict[str, MachineCatalogEntry] = {
    "EQ-001": MachineCatalogEntry("EQ-001", "PLANT_01", "LINE-A"),
    "EQ-002": MachineCatalogEntry("EQ-002", "PLANT_01", "LINE-A"),
    "EQ-004": MachineCatalogEntry("EQ-004", "PLANT_01", "LINE-C"),
    "EQ-005": MachineCatalogEntry("EQ-005", "PLANT_02", "LINE-D"),
    "EQ-006": MachineCatalogEntry("EQ-006", "PLANT_02", "LINE-D"),
    "EQ-007": MachineCatalogEntry("EQ-007", "PLANT_02", "LINE-E"),
    "EQ-008": MachineCatalogEntry("EQ-008", "PLANT_02", "LINE-E"),
}

FIXED_CLOCK = FrozenClock("2026-04-18T09:00:00Z")


# ===========================================================================
# 1. Timestamp Validation Tests
# ===========================================================================
def test_pulseforge_timestamp_strict_iso_acceptance():
    record = {
        "event_id": "PF-TEST",
        "machine_id": "EQ-001",
        "line_id": "LINE-A",
        "event_time": "2026-04-18T07:59:12Z",
        "event_type": "HIGH_VIBRATION",
        "severity": "high",
    }
    res = normalize_pulseforge_record(record, {"plant_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.event_time == datetime(2026, 4, 18, 7, 59, 12, tzinfo=UTC)


@pytest.mark.parametrize("bad_time", ["18-04-2026 08:28:20", "bad-time", "2026/04/18 08:00:00", ""])
def test_pulseforge_timestamp_rejection(bad_time: str):
    record = {
        "event_id": "PF-BAD",
        "machine_id": "EQ-001",
        "line_id": "LINE-A",
        "event_time": bad_time,
        "event_type": "HIGH_VIBRATION",
        "severity": "high",
    }
    res = normalize_pulseforge_record(record, {"plant_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is False
    assert any("event_time" in reason for reason in res.rejection_reasons)


def test_thermexwatch_epoch_ms_acceptance():
    record = {
        "readingId": "TW-TEST",
        "assetCode": "EQ-001",
        "productionLine": "A",
        "timestampMs": 1776499152000,
        "alertCode": "VIB_WARN",
        "level": 4,
    }
    res = normalize_thermexwatch_record(
        record, {"site_code": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK
    )
    assert res.success is True
    assert res.signal is not None
    assert res.signal.event_time == datetime.fromtimestamp(1776499152, tz=UTC)


@pytest.mark.parametrize("bad_ts", ["bad-ts", -100, None])
def test_thermexwatch_timestamp_rejection(bad_ts):
    record = {
        "readingId": "TW-BAD",
        "assetCode": "EQ-001",
        "productionLine": "A",
        "timestampMs": bad_ts,
        "alertCode": "VIB_WARN",
        "level": 4,
    }
    res = normalize_thermexwatch_record(
        record, {"site_code": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK
    )
    assert res.success is False
    assert any("timestampMs" in reason for reason in res.rejection_reasons)


def test_maintaflow_naive_timestamp_and_assumed_timezone():
    record = {
        "record_id": "MF-TEST",
        "machine_ref": "EQ-001",
        "line_ref": "LINE-A",
        "recorded_at": "2026/04/18 08:14:05",
        "record_type": "inspection",
        "inspection_result": "minor_defect_found",
    }
    res = normalize_maintaflow_record(
        record, {"factory_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK, assumed_tz_name="UTC"
    )
    assert res.success is True
    assert res.signal is not None
    assert res.signal.event_time == datetime(2026, 4, 18, 8, 14, 5, tzinfo=UTC)
    assert FLAG_ASSUMED_TIMEZONE in res.signal.data_quality_flags


# ===========================================================================
# 2. Unit Conversions & Raw Vibration Preservation
# ===========================================================================
def test_temperature_fahrenheit_to_celsius():
    assert fahrenheit_to_celsius(212.0) == 100.0
    assert fahrenheit_to_celsius(32.0) == 0.0
    assert fahrenheit_to_celsius(181.2) == 82.89


def test_thermexwatch_raw_vibration_g_preserved_and_mm_s_null():
    record = {
        "readingId": "TW-VIB",
        "assetCode": "EQ-001",
        "productionLine": "A",
        "timestampMs": 1776499152000,
        "alertCode": "VIB_WARN",
        "level": 4,
        "vibration_g": 0.81,
    }
    res = normalize_thermexwatch_record(
        record, {"site_code": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK
    )
    assert res.success is True
    assert res.signal is not None
    assert res.signal.vibration_g_raw == 0.81
    assert res.signal.vibration_mm_s is None


# ===========================================================================
# 3. Enum Taxonomy & Dimension Mapping
# ===========================================================================
@pytest.mark.parametrize(
    ("raw_type", "expected_type", "expected_dim", "expected_kind"),
    [
        (
            "HIGH_VIBRATION",
            CanonicalEventType.VIBRATION_HIGH,
            Dimension.VIBRATION,
            SignalKind.ALERT,
        ),
        (
            "TEMP_SPIKE",
            CanonicalEventType.TEMPERATURE_HIGH,
            Dimension.TEMPERATURE,
            SignalKind.ALERT,
        ),
        ("POWER_FLUCTUATION", CanonicalEventType.POWER_ANOMALY, Dimension.POWER, SignalKind.ALERT),
        (
            "SENSOR_HEALTH_DROP",
            CanonicalEventType.SENSOR_DEGRADED,
            Dimension.SENSOR,
            SignalKind.ALERT,
        ),
        ("RECOVERY_SIGNAL", CanonicalEventType.NOMINAL, Dimension.NONE, SignalKind.TELEMETRY),
        ("UNKNOWN_CODE", CanonicalEventType.UNKNOWN_EVENT, Dimension.NONE, SignalKind.ALERT),
    ],
)
def test_pulseforge_enum_mapping(raw_type, expected_type, expected_dim, expected_kind):
    record = {
        "event_id": "PF-ENUM",
        "machine_id": "EQ-001",
        "line_id": "LINE-A",
        "event_time": "2026-04-18T07:59:12Z",
        "event_type": raw_type,
        "severity": "low",
    }
    res = normalize_pulseforge_record(record, {"plant_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.canonical_event_type == expected_type
    assert res.signal.dimension == expected_dim
    assert res.signal.signal_kind == expected_kind
    if raw_type == "UNKNOWN_CODE":
        assert FLAG_UNMAPPED_ALERT_CODE in res.signal.data_quality_flags


@pytest.mark.parametrize(
    ("raw_alert", "expected_type", "expected_dim", "expected_kind"),
    [
        ("VIB_WARN", CanonicalEventType.VIBRATION_HIGH, Dimension.VIBRATION, SignalKind.ALERT),
        ("TEMP_WARN", CanonicalEventType.TEMPERATURE_HIGH, Dimension.TEMPERATURE, SignalKind.ALERT),
        (
            "TEMP_CRIT",
            CanonicalEventType.TEMPERATURE_CRITICAL,
            Dimension.TEMPERATURE,
            SignalKind.ALERT,
        ),
        ("POWER_DROP", CanonicalEventType.POWER_ANOMALY, Dimension.POWER, SignalKind.ALERT),
        ("OK", CanonicalEventType.NOMINAL, Dimension.NONE, SignalKind.TELEMETRY),
        ("CUSTOM_ALERT", CanonicalEventType.UNKNOWN_EVENT, Dimension.NONE, SignalKind.ALERT),
    ],
)
def test_thermexwatch_enum_mapping(raw_alert, expected_type, expected_dim, expected_kind):
    record = {
        "readingId": "TW-ENUM",
        "assetCode": "EQ-001",
        "productionLine": "A",
        "timestampMs": 1776499152000,
        "alertCode": raw_alert,
        "level": 3,
    }
    res = normalize_thermexwatch_record(
        record, {"site_code": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK
    )
    assert res.success is True
    assert res.signal is not None
    assert res.signal.canonical_event_type == expected_type
    assert res.signal.dimension == expected_dim
    assert res.signal.signal_kind == expected_kind
    if raw_alert == "CUSTOM_ALERT":
        assert FLAG_UNMAPPED_ALERT_CODE in res.signal.data_quality_flags


# ===========================================================================
# 4. Severity Mapping & Unknown Severity
# ===========================================================================
@pytest.mark.parametrize(
    ("raw_sev", "expected_sev"),
    [
        ("low", 1),
        ("medium", 2),
        ("high", 3),
        ("critical", 4),
    ],
)
def test_pulseforge_severity_scale(raw_sev, expected_sev):
    record = {
        "event_id": "PF-SEV",
        "machine_id": "EQ-001",
        "line_id": "LINE-A",
        "event_time": "2026-04-18T07:59:12Z",
        "event_type": "HIGH_VIBRATION",
        "severity": raw_sev,
    }
    res = normalize_pulseforge_record(record, {"plant_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.severity == expected_sev


def test_pulseforge_recovery_signal_severity_forced_to_zero():
    record = {
        "event_id": "PF-REC",
        "machine_id": "EQ-001",
        "line_id": "LINE-A",
        "event_time": "2026-04-18T07:59:12Z",
        "event_type": "RECOVERY_SIGNAL",
        "severity": "low",  # Raw indicates low, but recovery must be forced to 0
    }
    res = normalize_pulseforge_record(record, {"plant_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.severity == 0


def test_pulseforge_invalid_severity_string():
    record = {
        "event_id": "PF-URGENT",
        "machine_id": "EQ-001",
        "line_id": "LINE-A",
        "event_time": "2026-04-18T07:59:12Z",
        "event_type": "HIGH_VIBRATION",
        "severity": "urgent",
    }
    res = normalize_pulseforge_record(record, {"plant_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.severity is None
    assert FLAG_INVALID_SEVERITY in res.signal.data_quality_flags


@pytest.mark.parametrize(("level", "expected_sev"), [(1, 0), (2, 1), (3, 2), (4, 3), (5, 4)])
def test_thermexwatch_level_scale(level, expected_sev):
    record = {
        "readingId": "TW-LVL",
        "assetCode": "EQ-001",
        "productionLine": "A",
        "timestampMs": 1776499152000,
        "alertCode": "TEMP_WARN",
        "level": level,
    }
    res = normalize_thermexwatch_record(
        record, {"site_code": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK
    )
    assert res.success is True
    assert res.signal is not None
    assert res.signal.severity == expected_sev


def test_thermexwatch_invalid_level_string():
    record = {
        "readingId": "TW-BADLVL",
        "assetCode": "EQ-001",
        "productionLine": "A",
        "timestampMs": 1776499152000,
        "alertCode": "TEMP_WARN",
        "level": "four",
    }
    res = normalize_thermexwatch_record(
        record, {"site_code": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK
    )
    assert res.success is True
    assert res.signal is not None
    assert res.signal.severity is None
    assert FLAG_INVALID_SEVERITY in res.signal.data_quality_flags


# ===========================================================================
# 5. MaintaFlow Derived Severity & Confidence
# ===========================================================================
@pytest.mark.parametrize(
    ("rec_type", "insp_result", "maint_status", "note", "expected_sev"),
    [
        ("inspection", "major_defect_found", "not_due", "major crack", 3),
        ("maintenance_update", None, "overdue", "service delayed", 2),
        ("inspection", "minor_defect_found", "not_due", "minor wear", 1),
        ("calibration", "calibration_pending", "not_due", "awaiting check", 1),
        ("inspection", "passed_with_observation", "due_soon", "normal obs", 1),
        ("inspection", None, "completed", "done", 0),
        ("operator_note", None, "unknown", "Regular shift handover", 0),
        ("operator_note", None, "unknown", "Intermittent noise reported during warmup", 1),
        ("operator_note", None, "unknown", "Machine stalled before shift start", 1),
    ],
)
def test_maintaflow_derived_severity(rec_type, insp_result, maint_status, note, expected_sev):
    record = {
        "record_id": "MF-SEV",
        "machine_ref": "EQ-001",
        "line_ref": "LINE-A",
        "recorded_at": "2026/04/18 08:14:05",
        "record_type": rec_type,
        "inspection_result": insp_result,
        "maintenance_status": maint_status,
        "technician_note": note,
    }
    res = normalize_maintaflow_record(record, {"factory_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.severity == expected_sev


@pytest.mark.parametrize(
    ("conf_str", "expected_val"),
    [("low", 0.4), ("medium", 0.7), ("high", 0.9)],
)
def test_maintaflow_confidence_mapping(conf_str, expected_val):
    record = {
        "record_id": "MF-CONF",
        "machine_ref": "EQ-001",
        "line_ref": "LINE-A",
        "recorded_at": "2026/04/18 08:14:05",
        "record_type": "inspection",
        "manual_confidence": conf_str,
    }
    res = normalize_maintaflow_record(record, {"factory_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.vendor_confidence == expected_val


# ===========================================================================
# 6. ThermexWatch is_active Semantics
# ===========================================================================
def test_thermexwatch_is_active_idle_state():
    record = {
        "readingId": "TW-IDLE",
        "assetCode": "EQ-004",
        "productionLine": "C",
        "timestampMs": 1776498750000,
        "alertCode": "POWER_DROP",
        "level": 3,
        "is_active": False,
    }
    res = normalize_thermexwatch_record(
        record, {"site_code": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK
    )
    assert res.success is True
    assert res.signal is not None
    assert res.signal.machine_state == "idle"
    # An active alert is NOT cleared by is_active=False
    assert res.signal.canonical_event_type == CanonicalEventType.POWER_ANOMALY


# ===========================================================================
# 7. Sentinel & Invalid Numerics Sanitization
# ===========================================================================
def test_sanitize_float_sentinels_and_bounds():
    val, invalid = sanitize_float(-999.0, sentinels={-999.0})
    assert val is None
    assert invalid is True

    val, invalid = sanitize_float(-5.0, min_val=0.0)
    assert val is None
    assert invalid is True

    val, invalid = sanitize_float(1.2, min_val=0.0, max_val=1.0)
    assert val is None
    assert invalid is True

    val, invalid = sanitize_float("bad", min_val=0.0)
    assert val is None
    assert invalid is True

    val, invalid = sanitize_float("18.42", min_val=0.0)
    assert val == 18.42
    assert invalid is False


def test_pulseforge_sentinel_temperature_handling():
    record = {
        "event_id": "PF-SENT",
        "machine_id": "EQ-001",
        "line_id": "LINE-A",
        "event_time": "2026-04-18T07:59:12Z",
        "event_type": "HIGH_VIBRATION",
        "temperature_c": -999,
        "severity": "high",
    }
    res = normalize_pulseforge_record(record, {"plant_id": "PLANT_01"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.temperature_c is None
    assert FLAG_INVALID_VALUE in res.signal.data_quality_flags


# ===========================================================================
# 8. Entity Resolution Tests
# ===========================================================================
def test_entity_resolution_authoritative_match():
    res = resolve_entities("EQ-001", "PLANT_01", "LINE-A", MOCK_CATALOG)
    assert res.is_valid is True
    assert res.resolved_machine_id == "EQ-001"
    assert res.resolved_plant_id == "PLANT_01"
    assert res.resolved_line_id == "LINE-A"
    assert len(res.flags) == 0


def test_entity_resolution_short_line_expansion():
    res = resolve_entities("EQ-004", "PLANT_01", "C", MOCK_CATALOG)
    assert res.is_valid is True
    assert res.resolved_machine_id == "EQ-004"
    assert res.resolved_line_id == "LINE-C"
    assert len(res.flags) == 0


def test_entity_resolution_line_mismatch_flag():
    # Machine EQ-008 is actually on LINE-E, but record says "ZZ"
    res = resolve_entities("EQ-008", "PLANT_02", "ZZ", MOCK_CATALOG)
    assert res.is_valid is True
    assert res.resolved_machine_id == "EQ-008"
    assert res.resolved_line_id == "LINE-E"
    assert FLAG_LINE_MISMATCH_RESOLVED in res.flags


def test_entity_resolution_plant_mismatch_flag():
    # Machine EQ-001 is PLANT_01, but envelope says PLANT_02
    res = resolve_entities("EQ-001", "PLANT_02", "LINE-A", MOCK_CATALOG)
    assert res.is_valid is True
    assert res.resolved_plant_id == "PLANT_01"
    assert FLAG_PLANT_MISMATCH_RESOLVED in res.flags


def test_entity_resolution_missing_line_flag():
    res = resolve_entities("EQ-005", "PLANT_02", None, MOCK_CATALOG)
    assert res.is_valid is True
    assert res.resolved_line_id == "LINE-D"
    assert FLAG_MISSING_LINE_RESOLVED in res.flags


def test_entity_resolution_unknown_machine():
    res = resolve_entities("EQ-UNKNOWN", "PLANT_01", "LINE-A", MOCK_CATALOG)
    assert res.is_valid is False
    assert res.resolved_machine_id == "EQ-UNKNOWN"
    assert any("Unknown machine ID" in r for r in res.rejection_reasons)


@pytest.mark.parametrize("empty_val", ["", "   ", None])
def test_entity_resolution_empty_machine(empty_val):
    res = resolve_entities(empty_val, "PLANT_01", "LINE-A", MOCK_CATALOG)
    assert res.is_valid is False
    assert res.resolved_machine_id is None
    assert any("machine identifier" in r.lower() for r in res.rejection_reasons)


# ===========================================================================
# 9. SENSOR_DEGRADED Dimension Rule Test (User Guidance Rule)
# ===========================================================================
def test_sensor_degraded_dimension_is_sensor():
    """Verify SENSOR_HEALTH_DROP maps to dimension SENSOR and produces SENSOR_DEGRADED signal."""
    record = {
        "event_id": "PF-1006",
        "machine_id": "EQ-006",
        "line_id": "LINE-D",
        "event_time": "2026-04-18T07:50:10Z",
        "event_type": "SENSOR_HEALTH_DROP",
        "severity": "medium",
        "sensor_health": 0.49,
    }
    res = normalize_pulseforge_record(record, {"plant_id": "PLANT_02"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.canonical_event_type == CanonicalEventType.SENSOR_DEGRADED
    assert res.signal.dimension == Dimension.SENSOR
    assert res.signal.sensor_quality == 0.49


# ===========================================================================
# 10. CMMS Work Order Normalization Test
# ===========================================================================
def test_work_order_normalization():
    record = {
        "work_order_id": "WO-1001",
        "plant_id": "PLANT_01",
        "machine_id": "EQ-002",
        "line_id": "LINE-A",
        "opened_at": "2026-04-18T08:07:00Z",
        "closed_at": None,
        "maintenance_type": "cooling_system",
        "trigger_source": "sensor_alert",
        "status": "open",
        "downtime_minutes": 0,
        "post_maintenance_check": "pending",
        "notes": "Work order created after temperature spike",
    }
    res = normalize_work_order_record(record, {"source": "CMMS"}, MOCK_CATALOG, FIXED_CLOCK)
    assert res.success is True
    assert res.signal is not None
    assert res.signal.source_vendor == "CMMS"
    assert res.signal.source_record_id == "WO-1001"
    assert res.signal.signal_kind == SignalKind.WORK_ORDER
    assert res.signal.severity == 1
    assert res.signal.event_time == datetime(2026, 4, 18, 8, 7, 0, tzinfo=UTC)
