"""Unit tests for the pure deterministic status engine rules, thresholds, and property invariants."""

import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from app.core.config import Settings
from app.db.models import Machine, NormalizedSignal
from app.domain.engine import ReasonCode, evaluate_machine_state
from app.vendors.enums import CanonicalEventType, Dimension, SignalKind

DEFAULT_SETTINGS = Settings(
    ALERT_TTL_MINUTES=120,
    FRESHNESS_MINUTES=30,
    VENDOR_CONFLICT_WINDOW_MINUTES=5,
    TEMP_NEAR_LIMIT_RATIO=0.90,
    VIB_NEAR_LIMIT_RATIO=0.85,
    SENSOR_QUALITY_MIN=0.50,
    POWER_DEVIATION_PCT=20.0,
)

AS_OF = datetime(2026, 4, 18, 9, 0, 0, tzinfo=UTC)


def make_mock_machine(
    machine_id: str = "EQ-001",
    criticality: str = "medium",
    status: str = "active",
    rated_max_temp: float = 90.0,
    rated_max_vib: float = 8.5,
    baseline_power: float = 30.0,
) -> Machine:
    """Helper creating a detached Machine ORM instance."""
    return Machine(
        machine_id=machine_id,
        plant_id="PLANT_01",
        line_id="LINE-A",
        machine_type="Conveyor",
        criticality=criticality,
        installed_date=date(2022, 1, 1),
        rated_max_temp_c=rated_max_temp,
        rated_max_vibration_mm_s=rated_max_vib,
        baseline_power_kw=baseline_power,
        asset_status=status,
    )


def make_mock_signal(
    vendor: str,
    record_id: str,
    event_time: datetime,
    event_type: CanonicalEventType,
    dimension: Dimension,
    severity: int | None = 2,
    machine_id: str = "EQ-001",
    vibration_mm_s: float | None = None,
    vibration_g_raw: float | None = None,
    temperature_c: float | None = None,
    power_kw: float | None = None,
    machine_state: str = "running",
    sensor_quality: float | None = 0.9,
    signal_kind: SignalKind = SignalKind.ALERT,
    details: dict[str, Any] | None = None,
    is_effective: bool = True,
) -> NormalizedSignal:
    """Helper creating a detached NormalizedSignal ORM instance."""
    return NormalizedSignal(
        id=f"sig-{record_id}",
        raw_record_id=f"raw-{record_id}",
        source_vendor=vendor,
        source_record_id=record_id,
        machine_id=machine_id,
        plant_id="PLANT_01",
        line_id="LINE-A",
        signal_kind=signal_kind,
        canonical_event_type=event_type,
        dimension=dimension,
        severity=severity,
        event_time=event_time,
        ingested_at=event_time + timedelta(seconds=10),
        vibration_mm_s=vibration_mm_s,
        vibration_g_raw=vibration_g_raw,
        temperature_c=temperature_c,
        power_kw=power_kw,
        machine_state=machine_state,
        sensor_quality=sensor_quality,
        details=details or {},
        data_quality_flags=[],
        is_effective=is_effective,
    )


# ===========================================================================
# 1. Recovery vs Alert Ordering Tests (Rule 1)
# ===========================================================================
def test_newer_recovery_clears_older_alert():
    machine = make_mock_machine()
    alert = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-1",
        event_time=datetime(2026, 4, 18, 8, 0, tzinfo=UTC),
        event_type=CanonicalEventType.VIBRATION_HIGH,
        dimension=Dimension.VIBRATION,
        severity=3,
        vibration_mm_s=9.5,
    )
    recovery = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-2",
        event_time=datetime(2026, 4, 18, 8, 10, tzinfo=UTC),
        event_type=CanonicalEventType.NOMINAL,
        dimension=Dimension.NONE,
        severity=0,
        signal_kind=SignalKind.TELEMETRY,
    )

    res = evaluate_machine_state(machine, [alert, recovery], [], [], AS_OF, DEFAULT_SETTINGS)
    assert res.derived_status == "HEALTHY"
    assert res.attention_level == 0
    assert res.needs_attention is False
    assert ReasonCode.RECOVERING in res.reason_codes
    assert ReasonCode.VIBRATION_HIGH not in res.reason_codes


def test_older_recovery_never_clears_newer_alert():
    machine = make_mock_machine()
    recovery = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-1",
        event_time=datetime(2026, 4, 18, 7, 50, tzinfo=UTC),
        event_type=CanonicalEventType.NOMINAL,
        dimension=Dimension.NONE,
        severity=0,
        signal_kind=SignalKind.TELEMETRY,
    )
    alert = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-2",
        event_time=datetime(2026, 4, 18, 8, 20, tzinfo=UTC),
        event_type=CanonicalEventType.VIBRATION_HIGH,
        dimension=Dimension.VIBRATION,
        severity=3,
        vibration_mm_s=9.5,  # Above rated 8.5
    )

    res = evaluate_machine_state(machine, [recovery, alert], [], [], AS_OF, DEFAULT_SETTINGS)
    assert res.derived_status == "DEGRADED"
    assert res.attention_level == 3
    assert res.needs_attention is True
    assert ReasonCode.VIBRATION_ABOVE_RATED in res.reason_codes


# ===========================================================================
# 2. Cross-Vendor Conflict Window (Rule 7)
# ===========================================================================
def test_cross_vendor_conflict_within_window():
    machine = make_mock_machine()
    # Alert from ThermexWatch at 08:00
    tw_alert = make_mock_signal(
        vendor="ThermexWatch",
        record_id="TW-1",
        event_time=datetime(2026, 4, 18, 8, 0, tzinfo=UTC),
        event_type=CanonicalEventType.TEMPERATURE_HIGH,
        dimension=Dimension.TEMPERATURE,
        severity=2,
    )
    # Nominal from PulseForge at 08:03 (diff = 3 mins <= 5 min window)
    pf_nominal = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-1",
        event_time=datetime(2026, 4, 18, 8, 3, tzinfo=UTC),
        event_type=CanonicalEventType.NOMINAL,
        dimension=Dimension.NONE,
        severity=0,
        signal_kind=SignalKind.TELEMETRY,
    )

    res = evaluate_machine_state(machine, [tw_alert, pf_nominal], [], [], AS_OF, DEFAULT_SETTINGS)
    assert ReasonCode.VENDOR_CONFLICT in res.reason_codes
    assert ReasonCode.RECOVERING in res.reason_codes


def test_cross_vendor_conflict_outside_window():
    machine = make_mock_machine()
    # Alert from ThermexWatch at 08:00
    tw_alert = make_mock_signal(
        vendor="ThermexWatch",
        record_id="TW-1",
        event_time=datetime(2026, 4, 18, 8, 0, tzinfo=UTC),
        event_type=CanonicalEventType.TEMPERATURE_HIGH,
        dimension=Dimension.TEMPERATURE,
        severity=2,
    )
    # Nominal from PulseForge at 08:12 (diff = 12 mins > 5 min window)
    pf_nominal = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-1",
        event_time=datetime(2026, 4, 18, 8, 12, tzinfo=UTC),
        event_type=CanonicalEventType.NOMINAL,
        dimension=Dimension.NONE,
        severity=0,
        signal_kind=SignalKind.TELEMETRY,
    )

    res = evaluate_machine_state(machine, [tw_alert, pf_nominal], [], [], AS_OF, DEFAULT_SETTINGS)
    # Outside window -> NO VENDOR_CONFLICT, later nominal clears alert cleanly
    assert ReasonCode.VENDOR_CONFLICT not in res.reason_codes
    assert ReasonCode.RECOVERING in res.reason_codes


# ===========================================================================
# 3. Maintenance Resolution (Rule 6)
# ===========================================================================
def test_maintenance_resolution_suppresses_earlier_alerts():
    machine = make_mock_machine()
    vib_alert = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-1",
        event_time=datetime(2026, 4, 18, 7, 45, tzinfo=UTC),
        event_type=CanonicalEventType.VIBRATION_HIGH,
        dimension=Dimension.VIBRATION,
        severity=2,
    )
    # Closed work order with passed check at 08:10
    wo = make_mock_signal(
        vendor="CMMS",
        record_id="WO-1002",
        event_time=datetime(2026, 4, 18, 7, 30, tzinfo=UTC),
        event_type=CanonicalEventType.WORK_ORDER,
        dimension=Dimension.MAINTENANCE,
        severity=0,
        signal_kind=SignalKind.WORK_ORDER,
        details={
            "status": "closed",
            "post_maintenance_check": "passed",
            "closed_at": "2026-04-18T08:10:00Z",
        },
    )

    res = evaluate_machine_state(machine, [vib_alert, wo], [], [], AS_OF, DEFAULT_SETTINGS)
    assert ReasonCode.RESOLVED_BY_MAINTENANCE in res.reason_codes
    assert ReasonCode.VIBRATION_HIGH not in res.reason_codes
    assert res.derived_status == "HEALTHY"
    assert res.attention_level == 0


# ===========================================================================
# 4. Under Maintenance Override (Rule 9)
# ===========================================================================
def test_under_maintenance_asset_status_override():
    # EQ-007 has asset_status="maintenance"
    machine = make_mock_machine(status="maintenance")
    # Telemetry signal during maintenance
    sig = make_mock_signal(
        vendor="ThermexWatch",
        record_id="TW-OK",
        event_time=datetime(2026, 4, 18, 8, 30, tzinfo=UTC),
        event_type=CanonicalEventType.NOMINAL,
        dimension=Dimension.NONE,
        severity=0,
        signal_kind=SignalKind.TELEMETRY,
    )

    res = evaluate_machine_state(machine, [sig], [], [], AS_OF, DEFAULT_SETTINGS)
    assert res.derived_status == "UNDER_MAINTENANCE"
    assert res.needs_attention is False
    assert ReasonCode.MACHINE_IN_MAINTENANCE in res.reason_codes


# ===========================================================================
# 5. UNKNOWN & NO_RECENT_DATA Override (Rule 9)
# ===========================================================================
def test_unknown_status_when_signals_outside_ttl():
    machine = make_mock_machine()
    # Signal from 5 hours ago (TTL is 2 hours)
    old_sig = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-OLD",
        event_time=AS_OF - timedelta(hours=5),
        event_type=CanonicalEventType.VIBRATION_HIGH,
        dimension=Dimension.VIBRATION,
        severity=2,
    )

    res = evaluate_machine_state(machine, [old_sig], [], [], AS_OF, DEFAULT_SETTINGS)
    assert res.derived_status == "UNKNOWN"
    assert ReasonCode.NO_RECENT_DATA in res.reason_codes


# ===========================================================================
# 6. Criticality Bump Limits (Rule 8)
# ===========================================================================
@pytest.mark.parametrize(
    ("criticality", "base_sev", "expected_att"),
    [
        ("high", 2, 3),  # 2 bumped to 3
        ("high", 3, 4),  # 3 bumped to 4
        ("high", 4, 4),  # 4 capped at 4
        ("high", 1, 1),  # 1 stays 1 (never created from < 2)
        ("medium", 2, 2),  # Medium criticality not bumped
        ("low", 2, 2),  # Low criticality not bumped
    ],
)
def test_criticality_bump_behavior(criticality, base_sev, expected_att):
    machine = make_mock_machine(criticality=criticality, rated_max_vib=15.0)
    sig = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-CRIT",
        event_time=datetime(2026, 4, 18, 8, 30, tzinfo=UTC),
        event_type=CanonicalEventType.VIBRATION_HIGH,
        dimension=Dimension.VIBRATION,
        severity=base_sev,
        vibration_mm_s=5.0,  # Below near-limit, keeps base severity
    )

    res = evaluate_machine_state(machine, [sig], [], [], AS_OF, DEFAULT_SETTINGS)
    assert res.attention_level == expected_att


# ===========================================================================
# 7. Idle Power De-escalation (Rule 2)
# ===========================================================================
def test_idle_power_anomaly_de_escalated_to_1():
    machine = make_mock_machine(baseline_power=30.0)
    sig = make_mock_signal(
        vendor="ThermexWatch",
        record_id="TW-PWR",
        event_time=datetime(2026, 4, 18, 8, 30, tzinfo=UTC),
        event_type=CanonicalEventType.POWER_ANOMALY,
        dimension=Dimension.POWER,
        severity=2,
        power_kw=18.0,
        machine_state="idle",  # Idle state!
    )

    res = evaluate_machine_state(machine, [sig], [], [], AS_OF, DEFAULT_SETTINGS)
    assert res.attention_level == 1
    assert res.derived_status == "WATCH"


# ===========================================================================
# 8. Sensor Quality Trust & User SENSOR_DEGRADED Directive
# ===========================================================================
def test_sensor_quality_below_threshold_adds_low_signal_trust():
    machine = make_mock_machine()
    sig = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-SENSOR",
        event_time=datetime(2026, 4, 18, 8, 30, tzinfo=UTC),
        event_type=CanonicalEventType.SENSOR_DEGRADED,
        dimension=Dimension.SENSOR,
        severity=2,
        sensor_quality=0.45,  # < SENSOR_QUALITY_MIN 0.50
    )

    res = evaluate_machine_state(machine, [sig], [], [], AS_OF, DEFAULT_SETTINGS)
    assert ReasonCode.LOW_SIGNAL_TRUST in res.reason_codes
    assert res.attention_level == 1
    assert res.derived_status == "WATCH"
    assert res.needs_attention is False


def test_sensor_degraded_never_exceeds_attention_1(monkeypatch):
    """USER DIRECTIVE: SENSOR_DEGRADED signals (dimension SENSOR) never contribute

    more than attention 1, regardless of vendor severity. They only produce
    LOW_SIGNAL_TRUST.
    """
    machine = make_mock_machine(criticality="high")
    sig = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-SENSOR-HIGH",
        event_time=datetime(2026, 4, 18, 8, 30, tzinfo=UTC),
        event_type=CanonicalEventType.SENSOR_DEGRADED,
        dimension=Dimension.SENSOR,
        severity=4,  # Critical vendor severity
        sensor_quality=0.35,
    )

    res = evaluate_machine_state(machine, [sig], [], [], AS_OF, DEFAULT_SETTINGS)
    assert res.attention_level == 1
    assert res.derived_status == "WATCH"
    assert res.needs_attention is False
    assert ReasonCode.LOW_SIGNAL_TRUST in res.reason_codes


def test_eq006_stays_watch_attention_1():
    """EQ-006 Resolution Rule Test:

    SENSOR_DEGRADED signals (dimension SENSOR) never contribute more than attention 1,
    regardless of their vendor severity. They only produce LOW_SIGNAL_TRUST, as in engine rule 4.
    EQ-006 stays WATCH, attention 1, needs_attention false.
    """
    eq006 = make_mock_machine(machine_id="EQ-006", criticality="high")
    sig = make_mock_signal(
        vendor="PulseForge",
        record_id="PF-EQ006-01",
        event_time=datetime(2026, 4, 18, 8, 45, tzinfo=UTC),
        event_type=CanonicalEventType.SENSOR_DEGRADED,
        dimension=Dimension.SENSOR,
        severity=3,
        sensor_quality=0.32,
    )
    res = evaluate_machine_state(eq006, [sig], [], [], AS_OF, DEFAULT_SETTINGS)
    assert res.derived_status == "WATCH"
    assert res.attention_level == 1
    assert res.needs_attention is False
    assert res.reason_codes == ["LOW_SIGNAL_TRUST"]


# ===========================================================================
# 9. Shuffle Ordering Property Test
# ===========================================================================
def test_shuffle_signal_order_produces_identical_machine_view():
    """Property test: shuffling arrival/input order of signals must produce identical output."""
    machine = make_mock_machine(criticality="high")
    base_time = datetime(2026, 4, 18, 7, 30, tzinfo=UTC)

    sig1 = make_mock_signal(
        "PulseForge",
        "S1",
        base_time,
        CanonicalEventType.VIBRATION_HIGH,
        Dimension.VIBRATION,
        2,
        vibration_mm_s=6.0,
    )
    sig2 = make_mock_signal(
        "ThermexWatch",
        "S2",
        base_time + timedelta(minutes=10),
        CanonicalEventType.TEMPERATURE_HIGH,
        Dimension.TEMPERATURE,
        3,
        temperature_c=86.0,
    )
    sig3 = make_mock_signal(
        "PulseForge",
        "S3",
        base_time + timedelta(minutes=25),
        CanonicalEventType.NOMINAL,
        Dimension.NONE,
        0,
        signal_kind=SignalKind.TELEMETRY,
    )
    sig4 = make_mock_signal(
        "MaintaFlow",
        "S4",
        base_time + timedelta(minutes=35),
        CanonicalEventType.INSPECTION,
        Dimension.MAINTENANCE,
        1,
        signal_kind=SignalKind.INSPECTION,
        details={"inspection_result": "minor_defect_found"},
    )
    sig5 = make_mock_signal(
        "CMMS",
        "S5",
        base_time + timedelta(minutes=45),
        CanonicalEventType.WORK_ORDER,
        Dimension.MAINTENANCE,
        1,
        signal_kind=SignalKind.WORK_ORDER,
        details={"status": "open", "maintenance_type": "inspection"},
    )

    raw_signals = [sig1, sig2, sig3, sig4, sig5]
    baseline_result = evaluate_machine_state(
        machine, list(raw_signals), [], [], AS_OF, DEFAULT_SETTINGS
    )

    for seed in range(10):
        shuffled = list(raw_signals)
        random.Random(seed).shuffle(shuffled)
        shuffled_result = evaluate_machine_state(machine, shuffled, [], [], AS_OF, DEFAULT_SETTINGS)

        assert shuffled_result.derived_status == baseline_result.derived_status
        assert shuffled_result.attention_level == baseline_result.attention_level
        assert shuffled_result.needs_attention == baseline_result.needs_attention
        assert shuffled_result.reason_codes == baseline_result.reason_codes
        assert (
            shuffled_result.latest_relevant_event_time == baseline_result.latest_relevant_event_time
        )
        assert shuffled_result.processing_status == baseline_result.processing_status
