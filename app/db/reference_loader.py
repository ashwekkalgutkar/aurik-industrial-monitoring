"""Reference data loader for plants, lines, and machines.

Authoritatively loads asset_reference.csv and line_reference.csv into
the database with idempotent upserts.
"""

import csv
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.clock import get_clock
from app.core.config import get_settings
from app.db.models import Line, Machine, MachineStateView, Plant


def load_reference_data(
    session: Session,
    reference_dir: Path | str = "fixtures/reference",
) -> dict[str, int]:
    """Load reference CSVs into PostgreSQL with idempotent upserts.

    Returns a count summary of loaded entities: plants, lines, machines.
    """
    ref_path = Path(reference_dir)
    line_file = ref_path / "line_reference.csv"
    asset_file = ref_path / "asset_reference.csv"

    if not line_file.exists():
        raise FileNotFoundError(f"Missing line reference CSV at {line_file}")
    if not asset_file.exists():
        raise FileNotFoundError(f"Missing asset reference CSV at {asset_file}")

    clock = get_clock()
    settings = get_settings()
    now_utc = clock.now()

    # 1. Load Lines & Extract Plants
    plants_seen: set[str] = set()
    lines_to_upsert: list[dict[str, str]] = []

    with open(line_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            plant_id = row["plant_id"].strip()
            line_id = row["line_id"].strip()
            line_name = row["line_name"].strip()
            op_window = row["operating_window"].strip()

            plants_seen.add(plant_id)
            lines_to_upsert.append(
                {
                    "plant_id": plant_id,
                    "line_id": line_id,
                    "line_name": line_name,
                    "operating_window": op_window,
                }
            )

    # Upsert Plants
    for pid in sorted(plants_seen):
        existing_plant = session.get(Plant, pid)
        if not existing_plant:
            session.add(Plant(plant_id=pid, name=f"Manufacturing Facility {pid}"))

    session.flush()

    # Upsert Lines
    line_count = 0
    for l_data in lines_to_upsert:
        line = session.get(Line, l_data["line_id"])
        if line:
            line.plant_id = l_data["plant_id"]
            line.line_name = l_data["line_name"]
            line.operating_window = l_data["operating_window"]
        else:
            session.add(
                Line(
                    line_id=l_data["line_id"],
                    plant_id=l_data["plant_id"],
                    line_name=l_data["line_name"],
                    operating_window=l_data["operating_window"],
                )
            )
        line_count += 1

    session.flush()

    # 2. Load Machines
    machine_count = 0
    with open(asset_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            m_id = row["machine_id"].strip()
            plant_id = row["plant_id"].strip()
            line_id = row["line_id"].strip()
            m_type = row["machine_type"].strip()
            criticality = row["criticality"].strip()
            installed = date.fromisoformat(row["installed_date"].strip())
            max_temp = float(row["rated_max_temp_c"].strip())
            max_vib = float(row["rated_max_vibration_mm_s"].strip())
            baseline_pwr = float(row["baseline_power_kw"].strip())
            status = row["asset_status"].strip()

            machine = session.get(Machine, m_id)
            if machine:
                machine.plant_id = plant_id
                machine.line_id = line_id
                machine.machine_type = m_type
                machine.criticality = criticality
                machine.installed_date = installed
                machine.rated_max_temp_c = max_temp
                machine.rated_max_vibration_mm_s = max_vib
                machine.baseline_power_kw = baseline_pwr
                machine.asset_status = status
            else:
                session.add(
                    Machine(
                        machine_id=m_id,
                        plant_id=plant_id,
                        line_id=line_id,
                        machine_type=m_type,
                        criticality=criticality,
                        installed_date=installed,
                        rated_max_temp_c=max_temp,
                        rated_max_vibration_mm_s=max_vib,
                        baseline_power_kw=baseline_pwr,
                        asset_status=status,
                    )
                )

            # Ensure initial MachineStateView exists
            state_view = session.get(MachineStateView, m_id)
            if not state_view:
                session.add(
                    MachineStateView(
                        machine_id=m_id,
                        plant_id=plant_id,
                        line_id=line_id,
                        derived_status="UNDER_MAINTENANCE"
                        if status == "maintenance"
                        else "HEALTHY",
                        attention_level=0,
                        needs_attention=False,
                        reason_codes=["MACHINE_IN_MAINTENANCE"] if status == "maintenance" else [],
                        explanation=[],
                        latest_relevant_event_time=None,
                        last_processed_at=now_utc,
                        processing_status="FRESH",
                        source_event_refs=[],
                        rules_version=settings.RULESET_VERSION,
                        state_version=1,
                    )
                )

            machine_count += 1

    session.commit()
    return {
        "plants": len(plants_seen),
        "lines": line_count,
        "machines": machine_count,
    }
