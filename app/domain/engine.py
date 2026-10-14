"""Pure deterministic derived status engine for industrial equipment.

No database I/O, no wall-clock reads, fully testable, and order-independent.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import get_clock
from app.core.config import Settings, get_settings
from app.db.models import (
    Job,
    Machine,
    MachineStateView,
    NormalizedSignal,
    RawRecord,
)
from app.vendors.enums import CanonicalEventType, Dimension, SignalKind


class ReasonCode(StrEnum):
    """Authoritative vocabulary for operational status explanation reasons."""

    VIBRATION_HIGH = "VIBRATION_HIGH"
    VIBRATION_ABOVE_RATED = "VIBRATION_ABOVE_RATED"
    TEMPERATURE_HIGH = "TEMPERATURE_HIGH"
    TEMPERATURE_ABOVE_RATED = "TEMPERATURE_ABOVE_RATED"
    POWER_ANOMALY = "POWER_ANOMALY"
    LOW_SIGNAL_TRUST = "LOW_SIGNAL_TRUST"
    UNMAPPED_ALERT_CODE = "UNMAPPED_ALERT_CODE"
    INSPECTION_MAJOR_DEFECT = "INSPECTION_MAJOR_DEFECT"
    INSPECTION_MINOR_DEFECT = "INSPECTION_MINOR_DEFECT"
    MAINT_OVERDUE = "MAINT_OVERDUE"
    MAINT_DUE_SOON = "MAINT_DUE_SOON"
    CALIBRATION_PENDING = "CALIBRATION_PENDING"
    OPEN_WORK_ORDER = "OPEN_WORK_ORDER"
    OPERATOR_NOTE_CONCERN = "OPERATOR_NOTE_CONCERN"
    RECOVERING = "RECOVERING"
    RESOLVED_BY_MAINTENANCE = "RESOLVED_BY_MAINTENANCE"
    VENDOR_CONFLICT = "VENDOR_CONFLICT"
    MACHINE_IN_MAINTENANCE = "MACHINE_IN_MAINTENANCE"
    NO_RECENT_DATA = "NO_RECENT_DATA"


@dataclass(slots=True)
class ContributingReason:
    """Detailed trace item explaining a status decision."""

    code: ReasonCode
    attention: int
    rule: str
    observed_value: Any
    signal_refs: list[dict[str, Any]]
    event_time: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": str(self.code),
            "attention_contribution": self.attention,
            "rule": self.rule,
            "observed_value": self.observed_value,
            "signal_refs": self.signal_refs,
        }


@dataclass(slots=True)
class EngineResult:
    """Calculated operational view output from the pure status engine."""

    derived_status: str
    attention_level: int
    needs_attention: bool
    reason_codes: list[str]
    explanation: list[dict[str, Any]]
    latest_relevant_event_time: datetime | None
    processing_status: str
    source_event_refs: list[dict[str, Any]]
    rules_version: str


def evaluate_machine_state(
    machine: Machine,
    effective_signals: list[NormalizedSignal],
    raw_records: list[RawRecord],
    dead_jobs: list[Job],
    as_of: datetime,
    settings: Settings,
) -> EngineResult:
    """Pure, deterministic evaluation of machine status based on chronological signals."""
    ttl_cutoff = as_of - timedelta(minutes=settings.ALERT_TTL_MINUTES)
    freshness_cutoff = as_of - timedelta(minutes=settings.FRESHNESS_MINUTES)
    conflict_window = timedelta(minutes=settings.VENDOR_CONFLICT_WINDOW_MINUTES)

    # Sort all effective signals strictly by event_time, then by ingestion order for ties
    signals = sorted(effective_signals, key=lambda s: (s.event_time, s.ingested_at))

    reasons: list[ContributingReason] = []
    source_event_refs: list[dict[str, Any]] = []

    def _sig_ref(s: NormalizedSignal) -> dict[str, Any]:
        return {
            "vendor": s.source_vendor,
            "record_id": s.source_record_id,
            "raw_record_id": s.raw_record_id,
            "event_time": s.event_time.isoformat(),
        }

    # -------------------------------------------------------------------------
    # 1. Maintenance Resolutions (Rule 6)
    # -------------------------------------------------------------------------
    # A closed work order with post_maintenance_check=passed, or a MaintaFlow
    # maintenance_update with status completed, dated AFTER an alert/concern
    # suppresses that alert, operator concern, and dimension alerts earlier than it.
    resolutions: list[tuple[datetime, NormalizedSignal]] = []
    for s in signals:
        if s.signal_kind == SignalKind.WORK_ORDER:
            check = s.details.get("post_maintenance_check")
            status = s.details.get("status")
            if status == "closed" and check == "passed":
                c_time_str = s.details.get("closed_at")
                c_time = datetime.fromisoformat(c_time_str) if c_time_str else s.event_time
                resolutions.append((c_time, s))
        elif (
            s.source_vendor == "MaintaFlow"
            and s.canonical_event_type == CanonicalEventType.MAINTENANCE
        ):
            if s.details.get("maintenance_status") == "completed":
                resolutions.append((s.event_time, s))

    # -------------------------------------------------------------------------
    # 2. Identify NOMINAL Recovery Signals (Rule 1)
    # -------------------------------------------------------------------------
    recovery_signals = [s for s in signals if s.canonical_event_type == CanonicalEventType.NOMINAL]

    # -------------------------------------------------------------------------
    # 3. Process Dimension Alerts: Vibration, Temperature, Power (Rules 1, 2)
    # -------------------------------------------------------------------------
    # Track active alerts per dimension (VIBRATION, TEMPERATURE, POWER)
    dimension_signals = [
        s
        for s in signals
        if s.dimension in (Dimension.VIBRATION, Dimension.TEMPERATURE, Dimension.POWER)
        and s.canonical_event_type != CanonicalEventType.NOMINAL
        and s.event_time >= ttl_cutoff
    ]

    for s in dimension_signals:
        # Check if suppressed by a later maintenance resolution
        suppressing_res = next((r for r_time, r in resolutions if r_time > s.event_time), None)
        if suppressing_res:
            reasons.append(
                ContributingReason(
                    code=ReasonCode.RESOLVED_BY_MAINTENANCE,
                    attention=0,
                    rule="Alert earlier than completed maintenance was resolved.",
                    observed_value=s.canonical_event_type,
                    signal_refs=[_sig_ref(s), _sig_ref(suppressing_res)],
                    event_time=suppressing_res.event_time,
                )
            )
            source_event_refs.append(_sig_ref(suppressing_res))
            continue

        # Check if cleared by a later NOMINAL signal
        clearing_nom = next((r for r in recovery_signals if r.event_time > s.event_time), None)
        if clearing_nom:
            # Cleared by recovery!
            reasons.append(
                ContributingReason(
                    code=ReasonCode.RECOVERING,
                    attention=0,
                    rule="Alert cleared by newer recovery/nominal signal.",
                    observed_value=f"Cleared by {clearing_nom.source_vendor} nominal signal",
                    signal_refs=[_sig_ref(clearing_nom)],
                    event_time=clearing_nom.event_time,
                )
            )
            source_event_refs.append(_sig_ref(clearing_nom))
            continue

        # Alert is ACTIVE! Calculate attention
        att = s.severity or 1
        code: ReasonCode

        if s.dimension == Dimension.VIBRATION:
            code = ReasonCode.VIBRATION_HIGH
            if (
                s.vibration_mm_s is not None
                and s.vibration_mm_s >= machine.rated_max_vibration_mm_s
            ):
                att = max(att, 3)
                code = ReasonCode.VIBRATION_ABOVE_RATED
            elif (
                s.vibration_mm_s is not None
                and s.vibration_mm_s
                >= machine.rated_max_vibration_mm_s * settings.VIB_NEAR_LIMIT_RATIO
            ):
                att = max(att, 2)
                code = ReasonCode.VIBRATION_HIGH

        elif s.dimension == Dimension.TEMPERATURE:
            code = ReasonCode.TEMPERATURE_HIGH
            if s.canonical_event_type == CanonicalEventType.TEMPERATURE_CRITICAL:
                att = 4
                code = ReasonCode.TEMPERATURE_ABOVE_RATED
            elif s.temperature_c is not None and s.temperature_c >= machine.rated_max_temp_c:
                att = max(att, 3)
                code = ReasonCode.TEMPERATURE_ABOVE_RATED
            elif (
                s.temperature_c is not None
                and s.temperature_c >= machine.rated_max_temp_c * settings.TEMP_NEAR_LIMIT_RATIO
            ):
                att = max(att, 2)
                code = ReasonCode.TEMPERATURE_HIGH

        elif s.dimension == Dimension.POWER:
            code = ReasonCode.POWER_ANOMALY
            att = 2
            if s.machine_state == "idle":
                # Idle power anomaly de-escalated to attention 1
                att = 1
            elif (
                s.power_kw is not None
                and machine.baseline_power_kw > 0
                and s.machine_state == "running"
            ):
                dev_pct = (
                    abs(s.power_kw - machine.baseline_power_kw) / machine.baseline_power_kw * 100
                )
                if dev_pct > settings.POWER_DEVIATION_PCT and (s.severity or 0) >= 3:
                    att = 3

        reasons.append(
            ContributingReason(
                code=code,
                attention=att,
                rule=f"Active {s.dimension} alert within TTL.",
                observed_value={
                    "vibration_mm_s": s.vibration_mm_s,
                    "vibration_g": s.vibration_g_raw,
                    "temperature_c": s.temperature_c,
                    "power_kw": s.power_kw,
                },
                signal_refs=[_sig_ref(s)],
                event_time=s.event_time,
            )
        )
        source_event_refs.append(_sig_ref(s))

    # -------------------------------------------------------------------------
    # 4. Cross-Vendor Conflict Detection (Rule 7)
    # -------------------------------------------------------------------------
    # Disagreements between vendors on same dimension within VENDOR_CONFLICT_WINDOW
    for s1 in dimension_signals:
        for s2 in recovery_signals:
            if s1.source_vendor != s2.source_vendor:
                time_diff = abs(s1.event_time - s2.event_time)
                if time_diff <= conflict_window:
                    reasons.append(
                        ContributingReason(
                            code=ReasonCode.VENDOR_CONFLICT,
                            attention=1,
                            rule=f"Disagreement between {s1.source_vendor} and {s2.source_vendor} within {settings.VENDOR_CONFLICT_WINDOW_MINUTES}m window.",
                            observed_value={
                                "alert_vendor": s1.source_vendor,
                                "recovery_vendor": s2.source_vendor,
                                "time_diff_seconds": time_diff.total_seconds(),
                            },
                            signal_refs=[_sig_ref(s1), _sig_ref(s2)],
                            event_time=max(s1.event_time, s2.event_time),
                        )
                    )

    # -------------------------------------------------------------------------
    # 5. Unmapped Alert Code Signals (Rule 3)
    # -------------------------------------------------------------------------
    unmapped_signals = [
        s
        for s in signals
        if s.canonical_event_type == CanonicalEventType.UNKNOWN_EVENT and s.event_time >= ttl_cutoff
    ]
    for u in unmapped_signals:
        reasons.append(
            ContributingReason(
                code=ReasonCode.UNMAPPED_ALERT_CODE,
                attention=1,
                rule="Unmapped alert code received; contributes attention 1, never cleared by NOMINAL.",
                observed_value=u.details.get("raw_alert_code") or u.details.get("raw_event_type"),
                signal_refs=[_sig_ref(u)],
                event_time=u.event_time,
            )
        )
        source_event_refs.append(_sig_ref(u))

    # -------------------------------------------------------------------------
    # 6. Sensor Quality & Trust (Rule 4 & User Directive on SENSOR_DEGRADED)
    # -------------------------------------------------------------------------
    # User directive: "SENSOR_DEGRADED signals (dimension SENSOR) never contribute
    # more than attention 1, regardless of their vendor severity. They only produce
    # LOW_SIGNAL_TRUST, as in engine rule 4."
    vendor_streams: dict[str, NormalizedSignal] = {}
    for s in signals:
        if (
            s.sensor_quality is not None
            or s.canonical_event_type == CanonicalEventType.SENSOR_DEGRADED
            or s.dimension == Dimension.SENSOR
        ):
            vendor_streams[s.source_vendor] = s

    for _vendor_name, latest_sensor_sig in vendor_streams.items():
        is_low_quality = (
            latest_sensor_sig.sensor_quality is not None
            and latest_sensor_sig.sensor_quality < settings.SENSOR_QUALITY_MIN
        )
        is_sensor_degraded = (
            latest_sensor_sig.canonical_event_type == CanonicalEventType.SENSOR_DEGRADED
            or latest_sensor_sig.dimension == Dimension.SENSOR
        )
        if is_low_quality or is_sensor_degraded:
            reasons.append(
                ContributingReason(
                    code=ReasonCode.LOW_SIGNAL_TRUST,
                    attention=1,
                    rule="Sensor quality degraded or below threshold; contributes attention 1, produces LOW_SIGNAL_TRUST.",
                    observed_value=latest_sensor_sig.sensor_quality
                    if latest_sensor_sig.sensor_quality is not None
                    else "SENSOR_DEGRADED",
                    signal_refs=[_sig_ref(latest_sensor_sig)],
                    event_time=latest_sensor_sig.event_time,
                )
            )
            source_event_refs.append(_sig_ref(latest_sensor_sig))

    # -------------------------------------------------------------------------
    # 7. Maintenance State (Rule 5: state-based, latest wins per kind)
    # -------------------------------------------------------------------------
    # Latest inspection
    inspections = [s for s in signals if s.signal_kind == SignalKind.INSPECTION]
    latest_inspection = inspections[-1] if inspections else None
    latest_insp_is_major = False

    if latest_inspection:
        insp_res = latest_inspection.details.get("inspection_result")
        if insp_res == "major_defect_found":
            latest_insp_is_major = True
            reasons.append(
                ContributingReason(
                    code=ReasonCode.INSPECTION_MAJOR_DEFECT,
                    attention=3,
                    rule="Latest inspection found major defect.",
                    observed_value=latest_inspection.details.get("technician_note"),
                    signal_refs=[_sig_ref(latest_inspection)],
                    event_time=latest_inspection.event_time,
                )
            )
            source_event_refs.append(_sig_ref(latest_inspection))
        elif insp_res in ("minor_defect_found", "passed_with_observation"):
            reasons.append(
                ContributingReason(
                    code=ReasonCode.INSPECTION_MINOR_DEFECT,
                    attention=1,
                    rule="Latest inspection recorded minor defect or observation.",
                    observed_value=latest_inspection.details.get("technician_note"),
                    signal_refs=[_sig_ref(latest_inspection)],
                    event_time=latest_inspection.event_time,
                )
            )
            source_event_refs.append(_sig_ref(latest_inspection))

    # Latest maintenance status
    maint_updates = [
        s
        for s in signals
        if s.details.get("maintenance_status") and s.details.get("maintenance_status") != "unknown"
    ]
    latest_maint_update = maint_updates[-1] if maint_updates else None

    if latest_maint_update:
        m_stat = latest_maint_update.details.get("maintenance_status")
        if m_stat == "overdue":
            reasons.append(
                ContributingReason(
                    code=ReasonCode.MAINT_OVERDUE,
                    attention=2,
                    rule="Latest maintenance status is overdue.",
                    observed_value=latest_maint_update.details.get("days_since_last_service"),
                    signal_refs=[_sig_ref(latest_maint_update)],
                    event_time=latest_maint_update.event_time,
                )
            )
            source_event_refs.append(_sig_ref(latest_maint_update))
        elif m_stat == "due_soon":
            reasons.append(
                ContributingReason(
                    code=ReasonCode.MAINT_DUE_SOON,
                    attention=1,
                    rule="Latest maintenance status is due soon.",
                    observed_value=latest_maint_update.details.get("days_since_last_service"),
                    signal_refs=[_sig_ref(latest_maint_update)],
                    event_time=latest_maint_update.event_time,
                )
            )
            source_event_refs.append(_sig_ref(latest_maint_update))

    # Calibration pending
    calibrations = [s for s in signals if s.signal_kind == SignalKind.CALIBRATION]
    latest_cal = calibrations[-1] if calibrations else None
    if latest_cal and latest_cal.details.get("inspection_result") == "calibration_pending":
        reasons.append(
            ContributingReason(
                code=ReasonCode.CALIBRATION_PENDING,
                attention=1,
                rule="Sensor package awaiting recalibration.",
                observed_value=latest_cal.details.get("technician_note"),
                signal_refs=[_sig_ref(latest_cal)],
                event_time=latest_cal.event_time,
            )
        )
        source_event_refs.append(_sig_ref(latest_cal))

    # Open Work Orders
    work_orders = [s for s in signals if s.signal_kind == SignalKind.WORK_ORDER]
    open_work_orders = [w for w in work_orders if w.details.get("status") == "open"]
    has_open_work_order = len(open_work_orders) > 0

    for wo in open_work_orders:
        reasons.append(
            ContributingReason(
                code=ReasonCode.OPEN_WORK_ORDER,
                attention=1,
                rule=f"Open work order '{wo.source_record_id}' ({wo.details.get('maintenance_type')}).",
                observed_value=wo.details.get("notes"),
                signal_refs=[_sig_ref(wo)],
                event_time=wo.event_time,
            )
        )
        source_event_refs.append(_sig_ref(wo))

    # Operator Notes Concern
    op_notes = [s for s in signals if s.signal_kind == SignalKind.OPERATOR_NOTE]
    for op in op_notes:
        if (op.severity or 0) > 0:
            suppressing_res = next((r for r_time, r in resolutions if r_time > op.event_time), None)
            if suppressing_res:
                reasons.append(
                    ContributingReason(
                        code=ReasonCode.RESOLVED_BY_MAINTENANCE,
                        attention=0,
                        rule="Operator concern cleared by completed maintenance.",
                        observed_value=op.details.get("technician_note"),
                        signal_refs=[_sig_ref(op), _sig_ref(suppressing_res)],
                        event_time=suppressing_res.event_time,
                    )
                )
            else:
                reasons.append(
                    ContributingReason(
                        code=ReasonCode.OPERATOR_NOTE_CONCERN,
                        attention=1,
                        rule="Operator note recorded machine anomaly/concern.",
                        observed_value=op.details.get("technician_note"),
                        signal_refs=[_sig_ref(op)],
                        event_time=op.event_time,
                    )
                )
                source_event_refs.append(_sig_ref(op))

    # -------------------------------------------------------------------------
    # 8. Compute Attention & Apply Criticality Bump (Rule 8)
    # -------------------------------------------------------------------------
    raw_max_attention = max((r.attention for r in reasons), default=0)

    # Criticality escalation: if machine criticality is high and computed attention >= 2, raise by at most 1
    final_attention = raw_max_attention
    if machine.criticality == "high" and raw_max_attention >= 2:
        final_attention = min(4, raw_max_attention + 1)

    # -------------------------------------------------------------------------
    # 9. Derived Status & Maintenance Overrides (Rule 9)
    # -------------------------------------------------------------------------
    # Derived status mapping from attention level:
    # 4 -> CRITICAL, 3 -> DEGRADED, 1-2 -> WATCH, 0 -> HEALTHY
    if final_attention == 4:
        derived_status = "CRITICAL"
    elif final_attention == 3:
        derived_status = "DEGRADED"
    elif final_attention in (1, 2):
        derived_status = "WATCH"
    else:
        derived_status = "HEALTHY"

    # Maintenance Overrides
    has_scheduled_maint_wo = any(
        wo.details.get("maintenance_type") in ("scheduled_service", "sensor_recalibration")
        for wo in open_work_orders
    )

    is_under_maintenance = machine.asset_status == "maintenance" or has_scheduled_maint_wo

    if is_under_maintenance:
        derived_status = "UNDER_MAINTENANCE"
        reasons.append(
            ContributingReason(
                code=ReasonCode.MACHINE_IN_MAINTENANCE,
                attention=0,
                rule="Machine marked under maintenance in reference or scheduled work order.",
                observed_value=machine.asset_status,
                signal_refs=[],
                event_time=None,
            )
        )

    # UNKNOWN + NO_RECENT_DATA override when no signal exists in TTL and not under maintenance
    signals_in_ttl = [s for s in signals if s.event_time >= ttl_cutoff]
    if not signals_in_ttl and not is_under_maintenance:
        derived_status = "UNKNOWN"
        reasons.append(
            ContributingReason(
                code=ReasonCode.NO_RECENT_DATA,
                attention=0,
                rule=f"No telemetry signals received within {settings.ALERT_TTL_MINUTES}m TTL window.",
                observed_value=None,
                signal_refs=[],
                event_time=None,
            )
        )

    # needs_attention: attention_level >= 2, OR (open work order AND latest inspection is major defect)
    needs_attention = (final_attention >= 2) or (has_open_work_order and latest_insp_is_major)

    # Maintenance machines do not need operational alarm attention
    if is_under_maintenance and final_attention < 2:
        needs_attention = False

    # -------------------------------------------------------------------------
    # 10. Processing Status (Rule 10: FAILED > PARTIAL > STALE > FRESH)
    # -------------------------------------------------------------------------
    has_dead_jobs = len(dead_jobs) > 0

    # PARTIAL if any REJECTED_INVALID or QUEUED raw record resolved to this machine with received_at within TTL
    partial_records = [
        r
        for r in raw_records
        if r.resolved_machine_id == machine.machine_id
        and r.processing_state in ("REJECTED_INVALID", "QUEUED")
        and r.received_at >= ttl_cutoff
    ]
    has_partial_records = len(partial_records) > 0

    # STALE if latest effective signal is older than FRESHNESS_MINUTES relative to as_of
    latest_sig = signals[-1] if signals else None
    is_stale = latest_sig is None or latest_sig.event_time < freshness_cutoff

    if has_dead_jobs:
        processing_status = "FAILED"
    elif has_partial_records:
        processing_status = "PARTIAL"
    elif is_stale:
        processing_status = "STALE"
    else:
        processing_status = "FRESH"

    # -------------------------------------------------------------------------
    # 11. Explanation & Ordering (Rule 11)
    # -------------------------------------------------------------------------
    # Reason codes ordered by contribution descending, then recency
    reasons.sort(
        key=lambda r: (
            r.attention,
            r.event_time or datetime.min.replace(tzinfo=UTC),
        ),
        reverse=True,
    )

    # Deduplicate reason codes preserving highest attention order
    ordered_reason_codes: list[str] = []
    for r in reasons:
        r_str = str(r.code)
        if r_str not in ordered_reason_codes:
            ordered_reason_codes.append(r_str)

    # latest_relevant_event_time = event_time of newest signal contributing to a reason (or newest effective signal)
    contributing_times = [r.event_time for r in reasons if r.event_time is not None]
    if contributing_times:
        latest_relevant_time = max(contributing_times)
    elif latest_sig:
        latest_relevant_time = latest_sig.event_time
    else:
        latest_relevant_time = None

    explanation_list = [r.to_dict() for r in reasons]

    # Deduplicate source event refs preserving order
    seen_refs: set[str] = set()
    deduped_refs: list[dict[str, Any]] = []
    for ref in source_event_refs:
        ref_key = f"{ref['vendor']}:{ref['record_id']}"
        if ref_key not in seen_refs:
            seen_refs.add(ref_key)
            deduped_refs.append(ref)

    return EngineResult(
        derived_status=derived_status,
        attention_level=final_attention,
        needs_attention=needs_attention,
        reason_codes=ordered_reason_codes,
        explanation=explanation_list,
        latest_relevant_event_time=latest_relevant_time,
        processing_status=processing_status,
        source_event_refs=deduped_refs,
        rules_version=settings.RULESET_VERSION,
    )


def recompute_machine_state(
    machine_id: str,
    session: Session,
    as_of: datetime | None = None,
) -> MachineStateView:
    """Load machine signals from database and compute/upsert machine_state_view."""
    settings = get_settings()
    clock = get_clock()
    eval_time = as_of or clock.now()

    machine = session.get(Machine, machine_id)
    if not machine:
        raise ValueError(f"Machine '{machine_id}' does not exist.")

    # 1. Fetch effective signals chronologically
    signals = (
        session.query(NormalizedSignal)
        .filter_by(machine_id=machine_id, is_effective=True)
        .order_by(NormalizedSignal.event_time.asc())
        .all()
    )

    # 2. Fetch raw records for partial status checks
    raw_records = session.query(RawRecord).filter_by(resolved_machine_id=machine_id).all()

    # 3. Fetch dead jobs for failure checks
    dead_jobs = (
        session.query(Job)
        .filter(
            Job.status == "DEAD",
            Job.payload["machine_id"].astext == machine_id,
        )
        .all()
    )

    # 4. Pure Engine Evaluation
    result = evaluate_machine_state(
        machine=machine,
        effective_signals=signals,
        raw_records=raw_records,
        dead_jobs=dead_jobs,
        as_of=eval_time,
        settings=settings,
    )

    # 5. Optimistic Lock Upsert of MachineStateView
    state_view = session.get(MachineStateView, machine_id)
    if state_view:
        state_view.plant_id = machine.plant_id
        state_view.line_id = machine.line_id
        state_view.derived_status = result.derived_status
        state_view.attention_level = result.attention_level
        state_view.needs_attention = result.needs_attention
        state_view.reason_codes = result.reason_codes
        state_view.explanation = result.explanation
        state_view.latest_relevant_event_time = result.latest_relevant_event_time
        state_view.last_processed_at = clock.now()
        state_view.processing_status = result.processing_status
        state_view.source_event_refs = result.source_event_refs
        state_view.rules_version = result.rules_version
        state_view.state_version += 1
    else:
        state_view = MachineStateView(
            machine_id=machine_id,
            plant_id=machine.plant_id,
            line_id=machine.line_id,
            derived_status=result.derived_status,
            attention_level=result.attention_level,
            needs_attention=result.needs_attention,
            reason_codes=result.reason_codes,
            explanation=result.explanation,
            latest_relevant_event_time=result.latest_relevant_event_time,
            last_processed_at=clock.now(),
            processing_status=result.processing_status,
            source_event_refs=result.source_event_refs,
            rules_version=result.rules_version,
            state_version=1,
        )
        session.add(state_view)

    session.commit()
    return state_view
