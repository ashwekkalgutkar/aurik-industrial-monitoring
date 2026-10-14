# Data Findings & Quirks Analysis

This document provides a comprehensive audit of all provided assessment fixtures, seed datasets, and vendor API samples for the Aurik Tech Lead Assessment. Every finding here has been verified against the physical files in `fixtures/reference/`, `fixtures/seed_data/`, and `fixtures/vendor_api_samples/`.

---

## 1. Reference Data Verification

### 1.1 Machines (`asset_reference.csv`)
- **Count**: Exactly 8 machines (`EQ-001` through `EQ-008`).
- **Schema**: `machine_id, plant_id, line_id, machine_type, criticality, installed_date, rated_max_temp_c, rated_max_vibration_mm_s, baseline_power_kw, asset_status`.
- **Special States**:
  - `EQ-007` has `asset_status = "maintenance"`. All other 7 machines have `asset_status = "active"`.
  - Machine criticality distribution:
    - `high`: `EQ-001`, `EQ-003`, `EQ-004`, `EQ-007`
    - `medium`: `EQ-002`, `EQ-005`, `EQ-008`
    - `low`: `EQ-006`
  - Rated limits:
    - `EQ-001`: Max Temp 85.0°C, Max Vib 9.0 mm/s, Baseline Power 36.0 kW
    - `EQ-002`: Max Temp 90.0°C, Max Vib 8.5 mm/s, Baseline Power 29.0 kW
    - `EQ-003`: Max Temp 82.0°C, Max Vib 7.5 mm/s, Baseline Power 18.0 kW
    - `EQ-004`: Max Temp 95.0°C, Max Vib 10.0 mm/s, Baseline Power 42.0 kW
    - `EQ-005`: Max Temp 88.0°C, Max Vib 8.0 mm/s, Baseline Power 24.0 kW
    - `EQ-006`: Max Temp 80.0°C, Max Vib 6.5 mm/s, Baseline Power 12.0 kW
    - `EQ-007`: Max Temp 84.0°C, Max Vib 7.8 mm/s, Baseline Power 16.0 kW
    - `EQ-008`: Max Temp 92.0°C, Max Vib 9.5 mm/s, Baseline Power 33.0 kW

### 1.2 Lines (`line_reference.csv`)
- **Count**: Exactly 5 lines across 2 plants:
  - `PLANT_01`: `LINE-A` (Assembly, day_shift), `LINE-B` (Finishing, day_shift), `LINE-C` (Utilities, 24x7)
  - `PLANT_02`: `LINE-D` (Mixing, day_shift), `LINE-E` (Packaging, night_shift)

---

## 2. Ingestion Envelopes & Vendor Quirk Analysis

### 2.1 PulseForge
- **Envelope Structure**:
  - Top-level: `vendor` ("PulseForge"), `plant_id` ("PLANT_01"), `batch_generated_at` (ISO-8601 UTC timestamp).
  - Array key: `events`.
- **Record Fields**:
  - `event_id`: e.g. `PF-1001`
  - `machine_id`: e.g. `EQ-001`
  - `line_id`: e.g. `LINE-A`
  - `event_time`: ISO-8601 UTC with `Z` (e.g. `2026-04-18T07:59:12Z`)
  - `event_type`: `HIGH_VIBRATION`, `TEMP_SPIKE`, `SENSOR_HEALTH_DROP`, `POWER_FLUCTUATION`, `RECOVERY_SIGNAL`
  - `severity`: `"low"`, `"medium"`, `"high"`, `"critical"` (malformed records may contain invalid values like `"urgent"`)
  - `vibration_mm_s`: float in mm/s (comparable to `rated_max_vibration_mm_s`)
  - `temperature_c`: float in Celsius (comparable to `rated_max_temp_c`)
  - `machine_state`: `"running"`, `"idle"`
  - `sensor_health`: float between 0.0 and 1.0 (malformed records may contain strings like `"bad"`)
  - `vendor_confidence`: float between 0.0 and 1.0 (malformed records may contain out-of-range floats like `1.2`)
- **CSV Quirks (`pulseforge_events.csv`)**:
  - Columns include per-row `vendor`, `plant_id`, and `ingestion_time` (`2026-04-18T08:00:10Z`).
  - Ingestion time is a transport artifact; it must not be included in the normalized canonical record hash.
  - Rows correspond 1:1 with JSON samples (e.g., `PF-1001` and `PF-1002`).

### 2.2 ThermexWatch
- **Envelope Structure (camelCase)**:
  - Top-level: `source` ("ThermexWatch"), `site_code` ("PLANT_01"), `response_time_epoch_ms` (integer epoch ms).
  - Array key: `readings`.
- **Record Fields**:
  - `readingId`: e.g. `TW-8801`
  - `assetCode`: e.g. `EQ-001`
  - `productionLine`: single letter string e.g. `"A"`, `"B"`, `"C"`, `"D"`, `"E"`, or invalid like `"ZZ"`
  - `timestampMs`: integer epoch milliseconds e.g. `1776499152000` (malformed records may contain `"bad-ts"`)
  - `alertCode`: `VIB_WARN`, `TEMP_WARN`, `TEMP_CRIT`, `POWER_DROP`, `OK`, or unmapped like `UNKNOWN_ALERT`
  - `level`: integer `1` to `5` (malformed records may contain string `"four"`)
  - `vibration_g`: float in Gs (not mm/s; no frequency provided, so must NOT invent a conversion)
  - `temperature_f`: float in Fahrenheit (converted to Celsius: `(T_f - 32) * 5 / 9`)
  - `power_kw`: float
  - `is_active`: boolean `true`/`false`
  - `signal_quality`: `"GOOD"`, `"FAIR"`, `"POOR"`
- **CSV Quirks (`thermexwatch_readings.csv`)**:
  - Uses snake_case column names: `reading_id, source, site_code, asset_code, production_line, timestamp_ms, received_at_ms, alert_code, level, vibration_g, temperature_f, power_kw, is_active, signal_quality`.
  - `is_active` is formatted as string `"True"` or `"False"`.
  - `received_at_ms` is an envelope-level equivalent transport timestamp.
  - Mapping CSV rows to ThermexWatch envelopes requires coercing keys to camelCase, `"True"`/`"False"` to boolean `True`/`False`, and numeric types to match JSON samples for idempotency.

### 2.3 MaintaFlow
- **Envelope Structure**:
  - Top-level: `provider_name` ("MaintaFlow"), `factory_id` ("PLANT_01").
  - Array key: `records`.
- **Record Fields**:
  - `record_id`: e.g. `MF-501`
  - `machine_ref`: e.g. `EQ-001`
  - `line_ref`: e.g. `"LINE-A"` or short `"C"`
  - `recorded_at`: format `"%Y/%m/%d %H:%M:%S"` (e.g. `"2026/04/18 08:14:05"`), naive, no timezone! Must assume UTC by default and flag `ASSUMED_TIMEZONE`.
  - `record_type`: `"inspection"`, `"maintenance_update"`, `"operator_note"`, `"calibration"`
  - `inspection_result`: nullable string e.g. `"minor_defect_found"`, `"major_defect_found"`, `"passed_with_observation"`, `"calibration_pending"`, or `null`
  - `maintenance_status`: `"not_due"`, `"due_soon"`, `"overdue"`, `"completed"`, `"unknown"`
  - `days_since_last_service`: nullable integer
  - `technician_note`: free text string
  - `manual_confidence`: `"low"`, `"medium"`, `"high"`
- **CSV Quirks (`maintaflow_records.csv`)**:
  - Empty values for nullable columns (e.g. `inspection_result,,maintenance_status...`) are empty strings `""` instead of `null`.
  - Numeric integers like `days_since_last_service` are formatted as floats e.g. `18.0`.
  - When mapping CSV to envelope, empty strings must become `None` (`null`) and float-like integers must become `int` (`18`).

### 2.4 Internal CMMS (Work Orders)
- **Envelope Structure**:
  - No vendor JSON sample provided in assessment assets.
  - Defined internal envelope mirroring the CSV:
    ```json
    {
      "source": "CMMS",
      "work_orders": [
        {
          "work_order_id": "WO-1001",
          "plant_id": "PLANT_01",
          "machine_id": "EQ-002",
          "line_id": "LINE-A",
          "opened_at": "2026-04-18T08:07:00Z",
          "closed_at": null,
          "maintenance_type": "cooling_system",
          "trigger_source": "sensor_alert",
          "status": "open",
          "downtime_minutes": 0,
          "post_maintenance_check": "pending",
          "notes": "Work order created after temperature spike"
        }
      ]
    }
    ```

---

## 3. Edge-Case Fixture Findings

### 3.1 `happy_path.json`
- Contains standard updates across PulseForge (`PF-1101`, `PF-1102`), ThermexWatch (`TW-9901`, `TW-9902`), and MaintaFlow (`MF-601`).
- All machines and lines are valid and format is fully compliant.

### 3.2 `duplicates.json`
- **Intra-batch duplicate**: PulseForge contains `PF-1001` twice with identical values.
  - First occurrence must be `ACCEPTED`.
  - Second occurrence within the same batch must be `DUPLICATE`.
- **Content duplicate with distinct ID**: ThermexWatch contains `TW-8801` and `TW-8801-R` with identical measurements, timestamps, and assetCode.
  - `TW-8801-R` passes ingest layer 1 (different ID).
  - Worker layer 2 semantic idempotency detects identical machine, timestamp, event type, and measurements: marked `DUPLICATE_CONTENT`, `duplicate_of = TW-8801`, `is_effective = false`.

### 3.3 `conflicting_updates.json`
- PulseForge `PF-1401` on `EQ-008` (RECOVERY_SIGNAL at `2026-04-18T08:39:10Z`).
- ThermexWatch `TW-9921` on `EQ-008` (TEMP_CRIT at `1776500950000` = `2026-04-18T08:29:10Z`).
- MaintaFlow `MF-801` on `EQ-008` (major_defect_found at `2026/04/18 08:41:00`).
- **Timing Analysis**:
  - `TW-9921` is at 08:29:10Z. `PF-1401` is at 08:39:10Z (difference = 10 minutes).
  - Config `VENDOR_CONFLICT_WINDOW_MINUTES = 5`.
  - Because 10 minutes > 5 minutes, these signals do **not** trigger `VENDOR_CONFLICT`.
  - Later signal (`PF-1401` recovery) clears earlier temperature alert (`TW-9921`).
  - `MF-801` inspection defect remains active.

### 3.4 `malformed.json`
- **PulseForge**:
  - `PF-1201`: `machine_id: ""` -> empty string -> `REJECTED_INVALID` (missing machine).
  - `PF-1202`: `event_time: "18-04-2026 08:28:20"` -> non-standard timestamp format -> `REJECTED_INVALID`.
    - Also contains sentinel `temperature_c = -999`, invalid severity `"urgent"`, invalid sensor_health `"bad"`, invalid vendor_confidence `1.2`.
    - Machine ID `EQ-002` is resolved, so `resolved_machine_id = "EQ-002"` is preserved on the raw record!
- **ThermexWatch**:
  - `TW-9911`: `assetCode: null`, `timestampMs: "bad-ts"` -> `REJECTED_INVALID` (null machine, invalid timestamp).
  - `TW-9912`: `assetCode: "EQ-008"`, `productionLine: "ZZ"`, `alertCode: "UNKNOWN_ALERT"`.
    - Machine `EQ-008` is valid (exists in reference, which is authoritative for LINE-E).
    - `ZZ` does not match reference line -> resolved via reference, flagged `LINE_MISMATCH_RESOLVED`.
    - `UNKNOWN_ALERT` is unmapped -> mapped to canonical `UNKNOWN_EVENT` with flag `UNMAPPED_ALERT_CODE`.

### 3.5 `out_of_order.json`
- PulseForge has `PF-1302` (recovery at 08:32:05Z) positioned *before* `PF-1301` (high vibration at 08:31:02Z).
  - Engine must sort strictly by `event_time`.
  - In event time order: `PF-1301` (08:31:02Z) happened before `PF-1302` (08:32:05Z).
  - Therefore, the recovery clears `PF-1301`.
- MaintaFlow has `MF-701` (maintenance completed at 08:40:00) before `MF-700` (operator note at 08:05:00).
  - `MF-701` completed maintenance suppresses `MF-700` and earlier power alerts on `EQ-004`.

### 3.6 `retry_cases.json`
- `PF-1501`: valid high vibration on `EQ-005` at 08:47:12Z -> processed normally.
- `PF-1502`: `line_id: null`, `event_time: "bad-time"` on `EQ-005` -> `REJECTED_INVALID` (no retries).
  - `resolved_machine_id = "EQ-005"` stored on raw record.
  - This rejected record within TTL window triggers `processing_status = PARTIAL` for `EQ-005`!
- `MF-901`: valid overdue maintenance on `EQ-002` at 08:49:00 -> processed normally.
- `MF-902`: `machine_ref: ""` -> empty machine ID -> `REJECTED_INVALID` with zero retries.

---

## 4. Contradictions Between Files and Specification

No blocking contradictions exist between the assessment files and the prompt specifications:
1. All column names and schema differences between CSV seed files and JSON API samples are accounted for in the mapper design.
2. The anchor test outcomes match the exact chronological sequence and evaluation of all sample records.
3. The internal CMMS envelope assumption for work orders directly mirrors `maintenance_work_orders.csv`.
