# Operational Attention Engine Rules

This document specifies the deterministic, explainable rules used by the operational status engine (`app.domain.engine`) to derive machine health, attention level, and contributing reason codes from multi-vendor telemetry and maintenance signals.

---

## 1. Core Operating Principles

1. **Pure & Deterministic**: The engine receives machine metadata, chronological normalized signals, raw ingest records, and job failure states. No database queries, network calls, or non-deterministic clock accesses occur during evaluation.
2. **Order Independence**: Signals are sorted strictly by `(event_time, ingested_at)`. Replaying identical signals in any arbitrary order yields identical derived states (verified via permutation testing).
3. **Traceability**: Every contributing reason code links directly to its source vendor record identifiers (`source_vendor`, `source_record_id`, `raw_record_id`, `event_time`).
4. **Explainability**: Output explicitly lists all active and historical context reasons (`reasons_codes`, `explanation`), preventing "black box" decisions.

---

## 2. Operational Status & Attention Levels

| Attention Level | Derived Status | Needs Attention? | Operational Definition |
|---|---|---|---|
| **0** | `HEALTHY` | `false` | All monitored dimensions nominal. No active defects or overdue maintenance. |
| **1** | `WATCH` | `false` | Minor observation, idle power anomaly, unmapped event, or sensor degradation. Non-urgent. |
| **2** | `DEGRADED` | `true` | Approaching rated thresholds, overdue maintenance, or moderate vendor alert. Action required. |
| **3** | `DEGRADED` | `true` | Exceeding rated operating limits, major inspection defect, or high vendor alert. Prompt action required. |
| **4** | `CRITICAL` | `true` | Immediate danger of equipment failure (e.g. `TEMP_CRIT`, high criticality near limit, runaway vibration). Immediate intervention required. |
| Special | `UNDER_MAINTENANCE` | `false` | Machine is currently undergoing scheduled or active maintenance. Regular alert escalation is suppressed. |

---

## 3. Detailed Engine Rules Catalog

### Rule 1: NOMINAL / Recovery Signal Clearing
- When a `NOMINAL` event (e.g., PulseForge `RECOVERY_SIGNAL` or ThermexWatch `OK`) is received, it clears preceding active dimension alerts (vibration, temperature, power) that occurred prior to the recovery event timestamp.
- Produces reason code `RECOVERING` (attention 0) for traceability.
- **Limitation**: `NOMINAL` does *not* clear maintenance states, inspection defects, unmapped alert codes (`UNMAPPED_ALERT_CODE`), or sensor health degradation (`LOW_SIGNAL_TRUST`).

### Rule 2: Physical Limits, Rated Thresholds & Power Anomalies
- **Vibration**:
  - `vibration_mm_s >= machine.rated_max_vibration_mm_s`: attention 3 (`VIBRATION_ABOVE_RATED`).
  - `vibration_mm_s >= machine.rated_max_vibration_mm_s * 0.85`: attention 2 (`VIBRATION_HIGH`).
  - Otherwise, follows vendor severity (1 to 3).
- **Temperature**:
  - Event `TEMPERATURE_CRITICAL`: attention 4 (`TEMPERATURE_ABOVE_RATED`).
  - `temperature_c >= machine.rated_max_temp_c`: attention 3 (`TEMPERATURE_ABOVE_RATED`).
  - `temperature_c >= machine.rated_max_temp_c * 0.90`: attention 2 (`TEMPERATURE_HIGH`).
  - Otherwise, follows vendor severity (1 to 3).
- **Power**:
  - In `idle` state: power anomalies are de-escalated to attention 1 (`POWER_ANOMALY`).
  - In `running` state: deviation `> 25%` from `machine.baseline_power_kw` with vendor severity `>= 3`: attention 3 (`POWER_ANOMALY`). Standard power alerts default to attention 2.

### Rule 3: Unmapped Alert Codes
- Any vendor event with an unknown or unrecognized alert code maps to `CanonicalEventType.UNKNOWN_EVENT`.
- Unmapped alert events within `ALERT_TTL_MINUTES` contribute attention 1 with reason code `UNMAPPED_ALERT_CODE`.
- They are **never** cleared by subsequent `NOMINAL` events, ensuring engineering investigation is preserved.

### Rule 4: Sensor Quality & Degraded Signals (The EQ-006 Resolution Rule)
- **Direct Sensor Degradation**:
  - `SENSOR_DEGRADED` signals (dimension `Dimension.SENSOR`) represent telemetry stream degradation or instrument failure rather than physical equipment breakdown.
  - **Resolution Rule**: Signals with canonical event type `SENSOR_DEGRADED` or dimension `SENSOR` **never contribute more than attention 1**, regardless of vendor severity (e.g., vendor severity 3 or 4).
  - They produce solely reason code `LOW_SIGNAL_TRUST` (attention 1).
  - Machines with only sensor degradation (such as anchor machine `EQ-006`) remain in status **`WATCH`**, attention level **1**, with **`needs_attention = false`**.
- **Sensor Stream Trust**:
  - If a vendor's latest telemetry quality score drops below `SENSOR_QUALITY_MIN` (`0.50`), reason code `LOW_SIGNAL_TRUST` is added (attention 1).

### Rule 5: State-Based Maintenance Signals
- Unlike transient telemetry spikes, maintenance records represent persistent state (latest record wins):
  - **Inspections**:
    - `major_defect_found`: attention 3 (`INSPECTION_MAJOR_DEFECT`).
    - `minor_defect_found` or `passed_with_observation`: attention 1 (`INSPECTION_MINOR_DEFECT`).
  - **Maintenance Status**:
    - `overdue`: attention 2 (`MAINT_OVERDUE`).
    - `due_soon`: attention 1 (`MAINT_DUE_SOON`).
  - **Calibration**:
    - `overdue` / `calibration_required`: attention 1 (`CALIBRATION_PENDING`).
  - **Open Work Orders**:
    - Unresolved open work order: attention 1 (`OPEN_WORK_ORDER`).
  - **Operator Notes**:
    - Qualitative technician concerns (e.g. unusual noise, fluid leaks): attention 1 (`OPERATOR_NOTE_CONCERN`).

### Rule 6: Maintenance Resolutions
- A work order marked `status = closed` with `post_maintenance_check = passed`, or a MaintaFlow record with `maintenance_status = completed`, resolves:
  - All preceding vibration, temperature, and power alerts dated prior to the maintenance event.
  - Preceding operator concerns and open work orders.
- Produces reason code `RESOLVED_BY_MAINTENANCE` (attention 0) to preserve complete audit history.

### Rule 7: Cross-Vendor Conflict Detection
- When two different vendors report opposing states for the same machine on the same dimension within the `VENDOR_CONFLICT_WINDOW_MINUTES` (5 minutes) — for instance, Vendor A reports an alert while Vendor B reports nominal:
  - The engine flags a cross-vendor discrepancy with reason code `VENDOR_CONFLICT` (attention 1).

### Rule 8: Machine Under Maintenance Override
- If an active work order has `status = in_progress`, or the machine has an active maintenance update indicating work in progress:
  - `derived_status` is forced to **`UNDER_MAINTENANCE`**.
  - `attention_level` is set to **0** (or preserved as informational only).
  - `needs_attention` is set to **`false`**.
  - Reason code `MACHINE_IN_MAINTENANCE` is added.
  - Operational alarms are suppressed so maintenance technicians do not trigger false operational dispatch.

### Rule 9: High-Criticality Equipment Bump
- If `machine.criticality == "high"` and the maximum base attention calculated from active alerts is `>= 2`:
  - Attention level is escalated by `+1` (capped at maximum attention 4).
  - Machines with only attention 1 (e.g., minor observations or sensor degradation) are **not** escalated, avoiding alert fatigue on high-criticality assets.
  - Recorded in the `explanation` list only when actually applied (`reason_code: null`, attention contribution: 1). It is **never** emitted as a reason code.

---

## 4. Reason Codes Reference Table

| Reason Code | Attention Contribution | Cleared by NOMINAL? | Resolved by Maintenance? | Description |
|---|---|---|---|---|
| `VIBRATION_ABOVE_RATED` | 3 - 4 | Yes | Yes | Vibration exceeds rated machine limit. |
| `VIBRATION_HIGH` | 2 - 3 | Yes | Yes | Vibration above 85% of rated limit or moderate vendor alert. |
| `TEMPERATURE_ABOVE_RATED` | 3 - 4 | Yes | Yes | Temperature exceeds rated limit or `TEMP_CRIT` alert. |
| `TEMPERATURE_HIGH` | 2 - 3 | Yes | Yes | Temperature above 90% of rated limit or moderate vendor alert. |
| `POWER_ANOMALY` | 1 - 3 | Yes | Yes | Power spike/drop. De-escalated to 1 if machine is idle. |
| `LOW_SIGNAL_TRUST` | 1 | No | No | Sensor quality degraded or `< 0.50`. Capped at attention 1. |
| `UNMAPPED_ALERT_CODE` | 1 | No | No | Unrecognized vendor alert code received within TTL. |
| `INSPECTION_MAJOR_DEFECT`| 3 | No | Yes (subsequent clean inspection or closed WO) | Major defect recorded during physical inspection. |
| `INSPECTION_MINOR_DEFECT`| 1 | No | Yes (subsequent clean inspection or closed WO) | Minor defect or observation noted during inspection. |
| `MAINT_OVERDUE` | 2 | No | Yes (completed service) | Scheduled preventive maintenance is overdue. |
| `MAINT_DUE_SOON` | 1 | No | Yes (completed service) | Maintenance window is approaching. |
| `CALIBRATION_PENDING` | 1 | No | Yes (completed calibration) | Sensor or tool calibration is required. |
| `OPEN_WORK_ORDER` | 1 | No | Yes (closed WO) | An active corrective or inspection work order is open. |
| `OPERATOR_NOTE_CONCERN` | 1 | No | Yes | Qualitative technician concern recorded in log. |
| `RECOVERING` | 0 | N/A | N/A | Informational marker indicating recovery signal received. |
| `RESOLVED_BY_MAINTENANCE`| 0 | N/A | N/A | Informational marker indicating alert resolved by service. |
| `VENDOR_CONFLICT` | 1 | No | No | Conflicting vendor reports within 5-minute window. |
| `MACHINE_IN_MAINTENANCE` | 0 | N/A | N/A | Equipment is actively undergoing service. |
| `NO_RECENT_DATA` | 1 | No | No | No telemetry received within freshness window (30m). |

---

## 5. Processing Status Determination

| Processing Status | Condition |
|---|---|
| `FAILED` | Any unhandled queue job for this machine has reached dead-letter status (`DEAD`). |
| `PARTIAL` | The machine has raw records that could not be fully parsed or validated (`REJECTED_INVALID`), or had malformed fields skipped during ingestion. |
| `STALE` | No effective telemetry signals have an `event_time` within the freshness window (`FRESHNESS_MINUTES = 30`). |
| `FRESH` | Telemetry is recent, valid, and all associated ingest jobs processed without errors. |
