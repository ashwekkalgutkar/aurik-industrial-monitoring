"""Command-line interface for Aurik Equipment Monitoring service.

Supported commands:
- seed: Load reference tables, ingest all seed CSVs and JSON fixtures, drain queue, and print operational summary.
- worker: Run continuous background queue processor.
- recompute: Trigger status recomputation for machines.
"""

import argparse
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.clock import FrozenClock, get_clock
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.base import SessionLocal
from app.db.models import Machine, MachineStateView
from app.db.reference_loader import load_reference_data
from app.domain.engine import recompute_machine_state
from app.services.ingest_service import IngestionService
from app.vendors.mappers import (
    map_maintaflow_csv,
    map_pulseforge_csv,
    map_thermexwatch_csv,
    map_work_orders_csv,
)
from app.workers.runner import drain_queue, run_worker

logger = logging.getLogger(__name__)


def ingest_envelope_helper(
    service: IngestionService,
    envelope: dict[str, Any],
) -> None:
    """Detect vendor dialect from envelope structure and route to IngestionService."""
    if "events" in envelope:
        service.ingest_payload(
            vendor="PulseForge",
            envelope=envelope,
            record_array_key="events",
            record_id_key="event_id",
            machine_id_key="machine_id",
            required_envelope_keys=["vendor", "plant_id", "events"],
        )
    elif "readings" in envelope:
        service.ingest_payload(
            vendor="ThermexWatch",
            envelope=envelope,
            record_array_key="readings",
            record_id_key="readingId",
            machine_id_key="assetCode",
            required_envelope_keys=["source", "site_code", "readings"],
        )
    elif "records" in envelope:
        service.ingest_payload(
            vendor="MaintaFlow",
            envelope=envelope,
            record_array_key="records",
            record_id_key="record_id",
            machine_id_key="machine_ref",
            required_envelope_keys=["provider_name", "factory_id", "records"],
        )
    elif "work_orders" in envelope:
        service.ingest_payload(
            vendor="CMMS",
            envelope=envelope,
            record_array_key="work_orders",
            record_id_key="work_order_id",
            machine_id_key="machine_id",
            required_envelope_keys=["source", "work_orders"],
        )
    else:
        # Multi-vendor bundle dictionary (e.g. happy_path.json, duplicates.json)
        for _, sub_envelope in envelope.items():
            if isinstance(sub_envelope, dict):
                ingest_envelope_helper(service, sub_envelope)


def seed_database(
    base_dir: Path | None = None,
    as_of: datetime | None = None,
) -> None:
    """Execute end-to-end deterministic database seeding."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    eval_as_of = as_of or datetime(2026, 4, 18, 9, 0, 0, tzinfo=UTC)
    clock = FrozenClock(eval_as_of)

    root = base_dir or Path(__file__).resolve().parent.parent
    ref_dir = root / "fixtures" / "reference"
    seed_dir = root / "fixtures" / "seed_data"
    samples_dir = root / "fixtures" / "vendor_api_samples"

    session = SessionLocal()
    try:
        # 1. Reference Data Loading
        logger.info("Step 1: Loading authoritative reference tables from %s...", ref_dir)
        load_reference_data(session, ref_dir)

        ingest_service = IngestionService(session, clock)

        # 2. Ingest Seed CSVs
        logger.info("Step 2: Ingesting seed CSV datasets from %s...", seed_dir)
        # PulseForge
        pf_csv = seed_dir / "pulseforge_events.csv"
        if pf_csv.exists():
            for env in map_pulseforge_csv(pf_csv):
                ingest_envelope_helper(ingest_service, env)

        # ThermexWatch
        tw_csv = seed_dir / "thermexwatch_readings.csv"
        if tw_csv.exists():
            for env in map_thermexwatch_csv(tw_csv):
                ingest_envelope_helper(ingest_service, env)

        # MaintaFlow
        mf_csv = seed_dir / "maintaflow_records.csv"
        if mf_csv.exists():
            for env in map_maintaflow_csv(mf_csv):
                ingest_envelope_helper(ingest_service, env)

        # Work Orders
        wo_csv = seed_dir / "maintenance_work_orders.csv"
        if wo_csv.exists():
            for env in map_work_orders_csv(wo_csv):
                ingest_envelope_helper(ingest_service, env)

        # 3. Ingest JSON API Samples (Tests Idempotency on overlapping keys)
        logger.info("Step 3: Ingesting JSON sample payloads from %s...", samples_dir)
        sample_files = sorted(samples_dir.glob("*.json"))
        for s_file in sample_files:
            try:
                with s_file.open("r", encoding="utf-8") as f:
                    content = json.load(f)
                ingest_envelope_helper(ingest_service, content)
            except Exception as exc:
                logger.warning("Handled expected sample error or malformed envelope in %s: %s", s_file.name, exc)

        session.commit()

        # 4. Drain Worker Queue
        logger.info("Step 4: Draining background queue (processing normalization & recomputes)...")
        jobs_processed = drain_queue(session, clock, max_jobs=2000)
        logger.info("Processed %d asynchronous jobs.", jobs_processed)

        # 5. Ensure All Reference Machines Have Up-To-Date State Views
        logger.info("Step 5: Finalizing operational state recomputations as of %s...", eval_as_of.isoformat())
        machines = session.query(Machine).order_by(Machine.machine_id.asc()).all()
        for mach in machines:
            recompute_machine_state(mach.machine_id, session, as_of=eval_as_of)
        session.commit()

        # 6. Print Operational Summary Table
        print("\n" + "=" * 95)
        print(f"AURIK OPERATIONAL ATTENTION VIEW (as of {eval_as_of.isoformat()})")
        print("=" * 95)
        print(f"{'Machine ID':<12} | {'Plant':<10} | {'Line':<8} | {'Status':<18} | {'Attn':<4} | {'NeedAttn':<8} | {'ProcState':<10} | {'Reason Codes'}")
        print("-" * 95)

        views = (
            session.query(MachineStateView)
            .order_by(MachineStateView.attention_level.desc(), MachineStateView.machine_id.asc())
            .all()
        )
        for v in views:
            reasons_str = ", ".join(v.reason_codes) if v.reason_codes else "None"
            print(
                f"{v.machine_id:<12} | {v.plant_id:<10} | {v.line_id:<8} | "
                f"{v.derived_status:<18} | {v.attention_level:<4} | {str(v.needs_attention):<8} | "
                f"{v.processing_status:<10} | {reasons_str}"
            )
        print("=" * 95 + "\n")

    finally:
        session.close()


def main() -> None:
    """CLI argument parser entrypoint."""
    parser = argparse.ArgumentParser(description="Aurik Backend CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Seed
    seed_parser = subparsers.add_parser("seed", help="Load reference data and seed historical payloads")
    seed_parser.add_argument(
        "--as-of",
        type=str,
        default=None,
        help="Optional ISO UTC timestamp (default: 2026-04-18T09:00:00Z)",
    )

    # Worker
    subparsers.add_parser("worker", help="Start continuous background queue worker")

    # Recompute
    recompute_parser = subparsers.add_parser("recompute", help="Recompute operational state for machine")
    recompute_parser.add_argument("machine_id", type=str, help="Target machine ID (e.g. EQ-001) or 'all'")

    args = parser.parse_args()

    if args.command == "seed":
        as_of_dt = datetime.fromisoformat(args.as_of) if args.as_of else None
        seed_database(as_of=as_of_dt)
    elif args.command == "worker":
        run_worker()
    elif args.command == "recompute":
        session = SessionLocal()
        clock = get_clock()
        try:
            if args.machine_id.lower() == "all":
                machs = session.query(Machine).all()
                for m in machs:
                    recompute_machine_state(m.machine_id, session, as_of=clock.now())
                session.commit()
                print(f"Recomputed {len(machs)} machines.")
            else:
                view = recompute_machine_state(args.machine_id, session, as_of=clock.now())
                session.commit()
                print(f"Recomputed {view.machine_id}: status={view.derived_status}, attention={view.attention_level}")
        finally:
            session.close()


if __name__ == "__main__":
    main()
