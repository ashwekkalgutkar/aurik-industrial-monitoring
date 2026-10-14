# ruff: noqa: E402
"""Independent anchor verification script for Aurik Tech Lead Assessment.

Reads all raw assessment fixture files directly (independent of the database),
prints every contributing signal for each of the 8 monitored machines, and verifies
that the persisted machine_state_view in the database matches the expected
authoritative states as of 2026-04-18T09:00:00Z.
"""

import csv
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Line, Machine, MachineStateView


def parse_raw_signals(fixtures_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Parse raw fixture CSVs and JSONs directly without using DB."""
    signals_by_machine: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # 1. PulseForge CSV
    pf_csv = fixtures_dir / "seed_data" / "pulseforge_events.csv"
    if pf_csv.exists():
        with pf_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                m_id = row.get("machine_id", "").strip()
                if m_id:
                    signals_by_machine[m_id].append({
                        "source": "pulseforge_events.csv",
                        "vendor": "PulseForge",
                        "record_id": row.get("event_id"),
                        "event_time": row.get("event_time"),
                        "event_type": row.get("event_type"),
                        "value": f"vib={row.get('vibration_mm_s')} mm/s, temp={row.get('temperature_c')} C",
                    })

    # 2. ThermexWatch CSV
    tw_csv = fixtures_dir / "seed_data" / "thermexwatch_readings.csv"
    if tw_csv.exists():
        with tw_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                m_id = row.get("asset_code", "").strip()
                if m_id:
                    ts_ms = row.get("timestamp_ms")
                    ts_iso = (
                        datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=UTC).isoformat()
                        if ts_ms
                        else "N/A"
                    )
                    signals_by_machine[m_id].append({
                        "source": "thermexwatch_readings.csv",
                        "vendor": "ThermexWatch",
                        "record_id": row.get("reading_id"),
                        "event_time": ts_iso,
                        "event_type": row.get("alert_code"),
                        "value": f"vib={row.get('vibration_g')} g, temp={row.get('temperature_f')} F",
                    })

    # 3. MaintaFlow CSV
    mf_csv = fixtures_dir / "seed_data" / "maintaflow_records.csv"
    if mf_csv.exists():
        with mf_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                m_id = row.get("machine_ref", "").strip()
                if m_id:
                    signals_by_machine[m_id].append({
                        "source": "maintaflow_records.csv",
                        "vendor": "MaintaFlow",
                        "record_id": row.get("record_id"),
                        "event_time": row.get("recorded_at"),
                        "event_type": row.get("record_type"),
                        "value": f"insp={row.get('inspection_result')}, maint={row.get('maintenance_status')}",
                    })

    # 4. CMMS Work Orders CSV
    wo_csv = fixtures_dir / "seed_data" / "maintenance_work_orders.csv"
    if wo_csv.exists():
        with wo_csv.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                m_id = row.get("machine_id", "").strip()
                if m_id:
                    signals_by_machine[m_id].append({
                        "source": "maintenance_work_orders.csv",
                        "vendor": "CMMS",
                        "record_id": row.get("work_order_id"),
                        "event_time": row.get("opened_at"),
                        "event_type": row.get("maintenance_type"),
                        "value": f"status={row.get('status')}, check={row.get('post_maintenance_check')}",
                    })

    # 5. All 9 JSON Sample / Fixture Files
    samples_dir = fixtures_dir / "vendor_api_samples"
    if samples_dir.exists():
        for jf in sorted(samples_dir.glob("*.json")):
            with jf.open("r", encoding="utf-8") as f:
                data = json.load(f)

            def _extract_from_env(env: dict[str, Any], origin_file: str) -> None:
                if "events" in env:
                    for ev in env["events"]:
                        m = ev.get("machine_id")
                        if m:
                            signals_by_machine[m].append({
                                "source": origin_file,
                                "vendor": "PulseForge",
                                "record_id": ev.get("event_id"),
                                "event_time": ev.get("event_time"),
                                "event_type": ev.get("event_type"),
                                "value": f"vib={ev.get('vibration_mm_s')} mm/s, temp={ev.get('temperature_c')} C",
                            })
                elif "readings" in env:
                    for rd in env["readings"]:
                        m = rd.get("assetCode")
                        if m:
                            ts_ms = rd.get("timestampMs")
                            ts_iso = (
                                datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=UTC).isoformat()
                                if ts_ms
                                else "N/A"
                            )
                            signals_by_machine[m].append({
                                "source": origin_file,
                                "vendor": "ThermexWatch",
                                "record_id": rd.get("readingId"),
                                "event_time": ts_iso,
                                "event_type": rd.get("alertCode"),
                                "value": f"vib={rd.get('vibration_g')} g, temp={rd.get('temperature_f')} F",
                            })
                elif "records" in env:
                    for rc in env["records"]:
                        m = rc.get("machine_ref")
                        if m:
                            signals_by_machine[m].append({
                                "source": origin_file,
                                "vendor": "MaintaFlow",
                                "record_id": rc.get("record_id"),
                                "event_time": rc.get("recorded_at"),
                                "event_type": rc.get("record_type"),
                                "value": f"insp={rc.get('inspection_result')}, maint={rc.get('maintenance_status')}",
                            })
                else:
                    for k, sub in env.items():
                        if isinstance(sub, dict):
                            _extract_from_env(sub, f"{origin_file}:{k}")

            _extract_from_env(data, jf.name)

    return signals_by_machine


EXPECTED_ANCHORS = {
    "EQ-001": {
        "status": "WATCH",
        "attention": 1,
        "needs_attention": False,
        "processing_status": "FRESH",
        "exact_reasons": {"RECOVERING", "INSPECTION_MINOR_DEFECT"},
        "rationale": "All vibration alerts PF-1001, PF-1101, TW-8801, PF-1301 are older than recovery PF-1302 at 08:32:05.",
    },
    "EQ-002": {
        "status": "CRITICAL",
        "attention": 4,
        "needs_attention": True,
        "processing_status": "PARTIAL",
        "required_reasons": {"TEMPERATURE_ABOVE_RATED", "MAINT_OVERDUE", "OPEN_WORK_ORDER"},
        "rationale": "PF-1002 96.4C vs rated 90, TW-8803 TEMP_CRIT, WO-1001, MF-503, MF-901; PF-1202 malformed timestamp.",
    },
    "EQ-003": {
        "status": "HEALTHY",
        "attention": 0,
        "needs_attention": False,
        "processing_status": "STALE",
        "exact_reasons": set(),
        "rationale": "Latest signal PF-1102 at 08:17:01; all nominal/recovery, no reasons.",
    },
    "EQ-004": {
        "status": "HEALTHY",
        "attention": 0,
        "needs_attention": False,
        "processing_status": "FRESH",
        "exact_reasons": {"RESOLVED_BY_MAINTENANCE"},
        "rationale": "WO-1002 closed+passed 08:10 and MF-701 completed 08:40 supersede PF-1004, TW-8804, MF-502, MF-700.",
    },
    "EQ-005": {
        "status": "DEGRADED",
        "attention": 3,
        "needs_attention": True,
        "processing_status": "PARTIAL",
        "required_reasons": {"VIBRATION_ABOVE_RATED", "INSPECTION_MINOR_DEFECT", "MAINT_DUE_SOON"},
        "rationale": "Vibration 8.4/8.8 > rated 8.0, inspection passed_with_observation, maint due_soon; PF-1502 bad timestamp; no bump (medium).",
    },
    "EQ-006": {
        "status": "WATCH",
        "attention": 1,
        "needs_attention": False,
        "processing_status": "STALE",
        "exact_reasons": {"LOW_SIGNAL_TRUST"},
        "rationale": "PF-1006 sensor_health 0.49; per rule 4 capped at attention 1, produces LOW_SIGNAL_TRUST.",
    },
    "EQ-007": {
        "status": "UNDER_MAINTENANCE",
        "attention": 1,
        "needs_attention": False,
        "processing_status": "STALE",
        "exact_reasons": {"MACHINE_IN_MAINTENANCE", "CALIBRATION_PENDING", "OPEN_WORK_ORDER"},
        "rationale": "Asset status maintenance, open calibration work order WO-1004, TW-8807 is_active=false is idle.",
    },
    "EQ-008": {
        "status": "DEGRADED",
        "attention": 3,
        "needs_attention": True,
        "processing_status": "FRESH",
        "exact_reasons": {"INSPECTION_MAJOR_DEFECT", "OPEN_WORK_ORDER", "RECOVERING", "UNMAPPED_ALERT_CODE"},
        "rationale": "TW-9921 TEMP_CRIT 08:29 cleared by PF-1401 08:39 (10m > 5m, no VENDOR_CONFLICT); TW-9912 UNKNOWN_ALERT never cleared.",
    },
}


def verify_anchors() -> int:
    """Execute raw signal extraction and database reconciliation."""
    root = Path(__file__).resolve().parent.parent
    fixtures_dir = root / "fixtures"

    print("=" * 100)
    print("INDEPENDENT RAW FIXTURE SIGNAL EXTRACTION")
    print("=" * 100)

    raw_signals = parse_raw_signals(fixtures_dir)
    for m_id in sorted(raw_signals.keys()):
        sigs = raw_signals[m_id]
        print(f"\n[{m_id}] Found {len(sigs)} raw signals across CSVs and JSONs:")
        for s in sigs:
            print(f"  - {s['vendor']:<12} | ID: {s['record_id']:<10} | Time: {s['event_time']:<24} | Type: {s['event_type']:<20} | {s['value']}")

    print("\n" + "=" * 100)
    print("DATABASE RECONCILIATION AGAINST EXPECTED ANCHORS TABLE")
    print("=" * 100)

    session = SessionLocal()
    failures: list[str] = []

    try:
        views = {v.machine_id: v for v in session.execute(select(MachineStateView)).scalars().all()}

        for m_id, exp in EXPECTED_ANCHORS.items():
            view = views.get(m_id)
            if not view:
                failures.append(f"FAIL: {m_id} missing from database machine_state_view.")
                continue

            view_reasons = set(view.reason_codes or [])
            errors: list[str] = []

            if view.derived_status != exp["status"]:
                errors.append(f"status: got '{view.derived_status}', expected '{exp['status']}'")

            if view.attention_level != exp["attention"]:
                errors.append(f"attention: got {view.attention_level}, expected {exp['attention']}")

            if view.needs_attention != exp["needs_attention"]:
                errors.append(f"needs_attention: got {view.needs_attention}, expected {exp['needs_attention']}")

            if view.processing_status != exp["processing_status"]:
                errors.append(f"processing_status: got '{view.processing_status}', expected '{exp['processing_status']}'")

            expected_exact = exp.get("exact_reasons")
            if isinstance(expected_exact, set):
                if view_reasons != expected_exact:
                    errors.append(f"exact reasons: got {sorted(view_reasons)}, expected {sorted(expected_exact)}")
            expected_required = exp.get("required_reasons")
            if isinstance(expected_required, set):
                missing = expected_required - view_reasons
                if missing:
                    errors.append(f"missing required reasons: {sorted(missing)} (got {sorted(view_reasons)})")

            if errors:
                failures.append(f"FAIL: {m_id} -> {'; '.join(errors)}")
            else:
                reasons_str = ", ".join(view.reason_codes) if view.reason_codes else "None"
                print(f"PASS: {m_id:<8} | Status: {view.derived_status:<17} | Attn: {view.attention_level} | NeedsAttn: {str(view.needs_attention):<5} | State: {view.processing_status:<8} | Reasons: [{reasons_str}]")

        # Summary Metrics Verification
        print("-" * 100)
        status_counts: dict[str, int] = defaultdict(int)
        proc_counts: dict[str, int] = defaultdict(int)
        needing_attention = 0
        critical_ids: list[str] = []

        for v in views.values():
            status_counts[v.derived_status] += 1
            proc_counts[v.processing_status] += 1
            if v.needs_attention:
                needing_attention += 1
            if v.derived_status == "CRITICAL" or v.attention_level == 4:
                critical_ids.append(v.machine_id)

        print("Global System Summaries:")
        print(f"  - Status Counts: CRITICAL={status_counts['CRITICAL']}, DEGRADED={status_counts['DEGRADED']}, WATCH={status_counts['WATCH']}, HEALTHY={status_counts['HEALTHY']}, UNDER_MAINTENANCE={status_counts['UNDER_MAINTENANCE']}")
        print(f"  - Needs Attention: {needing_attention} | Stale: {proc_counts['STALE']} | Partial: {proc_counts['PARTIAL']}")
        print(f"  - Critical Machines: {critical_ids}")

        if status_counts["CRITICAL"] != 1 or critical_ids != ["EQ-002"]:
            failures.append(f"Summary mismatch: CRITICAL machines expected ['EQ-002'], got {critical_ids}")
        if status_counts["DEGRADED"] != 2:
            failures.append(f"Summary mismatch: DEGRADED expected 2, got {status_counts['DEGRADED']}")
        if status_counts["WATCH"] != 2:
            failures.append(f"Summary mismatch: WATCH expected 2, got {status_counts['WATCH']}")
        if status_counts["HEALTHY"] != 2:
            failures.append(f"Summary mismatch: HEALTHY expected 2, got {status_counts['HEALTHY']}")
        if status_counts["UNDER_MAINTENANCE"] != 1:
            failures.append(f"Summary mismatch: UNDER_MAINTENANCE expected 1, got {status_counts['UNDER_MAINTENANCE']}")
        if needing_attention != 3:
            failures.append(f"Summary mismatch: needs_attention expected 3, got {needing_attention}")
        if proc_counts["STALE"] != 3:
            failures.append(f"Summary mismatch: STALE expected 3, got {proc_counts['STALE']}")
        if proc_counts["PARTIAL"] != 2:
            failures.append(f"Summary mismatch: PARTIAL expected 2, got {proc_counts['PARTIAL']}")

        # Line Rankings Check
        machines = session.execute(select(Machine)).scalars().all()
        lines = session.execute(select(Line)).scalars().all()
        line_rankings = []
        for line in lines:
            line_m = [m for m in machines if m.line_id == line.line_id]
            line_views = [views[m.machine_id] for m in line_m if m.machine_id in views]
            need_att = sum(1 for lv in line_views if lv.needs_attention)
            crit_cnt = sum(1 for lv in line_views if lv.derived_status == "CRITICAL" or lv.attention_level == 4)
            line_rankings.append((line.line_id, need_att, crit_cnt))

        # Sort: needs_attention desc, critical_count desc, line_id asc
        line_rankings.sort(key=lambda x: (-x[1], -x[2], x[0]))
        lines_needing_att = [r[0] for r in line_rankings if r[1] > 0]
        print(f"  - Lines Ranked by Attention: {lines_needing_att}")
        if lines_needing_att != ["LINE-A", "LINE-D", "LINE-E"]:
            failures.append(f"Summary mismatch: Lines ranked expected ['LINE-A', 'LINE-D', 'LINE-E'], got {lines_needing_att}")

    finally:
        session.close()

    print("=" * 100)
    if failures:
        print(f"\nVERIFICATION FAILED WITH {len(failures)} ERROR(S):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nALL 8 MACHINE ANCHORS AND SYSTEM SUMMARIES VERIFIED SUCCESSFULLY.")
    return 0


if __name__ == "__main__":
    sys.exit(verify_anchors())
