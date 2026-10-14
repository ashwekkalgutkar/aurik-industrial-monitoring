"""Explicit CSV-to-JSON envelope mappers for seed datasets with strict type coercion."""

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any


def _clean_str(val: str | None) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _clean_float(val: str | None) -> float | None:
    s = _clean_str(val)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean_int(val: str | None, default: int | None = None) -> int | None:
    s = _clean_str(val)
    if s is None:
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def _clean_bool(val: str | None, default: bool = True) -> bool:
    s = _clean_str(val)
    if s is None:
        return default
    return s.lower() in ("true", "1", "t", "yes")


def map_pulseforge_csv(csv_path: Path | str) -> list[dict[str, Any]]:
    """Parse pulseforge_events.csv and group into valid PulseForge batch envelopes by plant_id."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"PulseForge CSV not found: {path}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            plant_id = _clean_str(row.get("plant_id")) or "PLANT_01"
            event = {
                "event_id": _clean_str(row.get("event_id")),
                "machine_id": _clean_str(row.get("machine_id")),
                "line_id": _clean_str(row.get("line_id")),
                "event_time": _clean_str(row.get("event_time")),
                "event_type": _clean_str(row.get("event_type")),
                "severity": _clean_str(row.get("severity")),
                "vibration_mm_s": _clean_float(row.get("vibration_mm_s")),
                "temperature_c": _clean_float(row.get("temperature_c")),
                "machine_state": _clean_str(row.get("machine_state")),
                "sensor_health": _clean_float(row.get("sensor_health")),
                "vendor_confidence": _clean_float(row.get("vendor_confidence")),
            }
            groups[plant_id].append(event)

    envelopes: list[dict[str, Any]] = []
    for plant_id, events in sorted(groups.items()):
        envelopes.append({
            "vendor": "PulseForge",
            "plant_id": plant_id,
            "events": events,
        })
    return envelopes


def map_thermexwatch_csv(csv_path: Path | str) -> list[dict[str, Any]]:
    """Parse thermexwatch_readings.csv and group into ThermexWatch envelopes by site_code."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"ThermexWatch CSV not found: {path}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            site_code = _clean_str(row.get("site_code")) or "PLANT_01"
            reading = {
                "readingId": _clean_str(row.get("reading_id")),
                "assetCode": _clean_str(row.get("asset_code")),
                "productionLine": _clean_str(row.get("production_line")),
                "timestampMs": _clean_int(row.get("timestamp_ms"), 0),
                "alertCode": _clean_str(row.get("alert_code")),
                "level": _clean_int(row.get("level"), 1),
                "vibration_g": _clean_float(row.get("vibration_g")),
                "temperature_f": _clean_float(row.get("temperature_f")),
                "power_kw": _clean_float(row.get("power_kw")),
                "is_active": _clean_bool(row.get("is_active"), True),
                "signal_quality": _clean_str(row.get("signal_quality")),
            }
            groups[site_code].append(reading)

    envelopes: list[dict[str, Any]] = []
    for site_code, readings in sorted(groups.items()):
        envelopes.append({
            "source": "ThermexWatch",
            "site_code": site_code,
            "readings": readings,
        })
    return envelopes


def map_maintaflow_csv(csv_path: Path | str) -> list[dict[str, Any]]:
    """Parse maintaflow_records.csv and group into MaintaFlow envelopes by factory_id."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"MaintaFlow CSV not found: {path}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            factory_id = _clean_str(row.get("factory_id")) or "PLANT_01"
            record = {
                "record_id": _clean_str(row.get("record_id")),
                "machine_ref": _clean_str(row.get("machine_ref")),
                "line_ref": _clean_str(row.get("line_ref")),
                "recorded_at": _clean_str(row.get("recorded_at")),
                "record_type": _clean_str(row.get("record_type")),
                "inspection_result": _clean_str(row.get("inspection_result")),
                "maintenance_status": _clean_str(row.get("maintenance_status")),
                "days_since_last_service": _clean_float(row.get("days_since_last_service")),
                "technician_note": _clean_str(row.get("technician_note")),
                "manual_confidence": _clean_str(row.get("manual_confidence")),
            }
            groups[factory_id].append(record)

    envelopes: list[dict[str, Any]] = []
    for factory_id, records in sorted(groups.items()):
        envelopes.append({
            "provider_name": "MaintaFlow",
            "factory_id": factory_id,
            "records": records,
        })
    return envelopes


def map_work_orders_csv(csv_path: Path | str) -> list[dict[str, Any]]:
    """Parse maintenance_work_orders.csv and group into CMMS work orders envelopes."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Work Orders CSV not found: {path}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            plant_id = _clean_str(row.get("plant_id")) or "PLANT_01"
            wo = {
                "work_order_id": _clean_str(row.get("work_order_id")),
                "machine_id": _clean_str(row.get("machine_id")),
                "line_id": _clean_str(row.get("line_id")),
                "opened_at": _clean_str(row.get("opened_at")),
                "closed_at": _clean_str(row.get("closed_at")),
                "maintenance_type": _clean_str(row.get("maintenance_type")),
                "trigger_source": _clean_str(row.get("trigger_source")),
                "status": _clean_str(row.get("status")),
                "downtime_minutes": _clean_int(row.get("downtime_minutes"), 0),
                "post_maintenance_check": _clean_str(row.get("post_maintenance_check")),
                "notes": _clean_str(row.get("notes")),
            }
            groups[plant_id].append(wo)

    envelopes: list[dict[str, Any]] = []
    for plant_id, work_orders in sorted(groups.items()):
        envelopes.append({
            "source": "CMMS",
            "plant_id": plant_id,
            "work_orders": work_orders,
        })
    return envelopes
