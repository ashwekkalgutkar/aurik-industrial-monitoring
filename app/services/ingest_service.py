"""Ingestion orchestration service implementing raw-first storage and Layer 1 idempotency."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.api.schemas.ingest import IngestResponse, RecordDisposition, RecordIngestResult
from app.core.canonical import compute_canonical_record_hash
from app.core.clock import Clock
from app.core.errors import PayloadValidationError
from app.db.models import IngestionBatch, Job, NormalizedSignal, RawRecord

SUPPORTED_SCHEMA_VERSIONS = {"1.0", "1", "v1"}


class IngestionService:
    """Service handling structural envelope validation, raw persistence, and Layer 1 idempotency."""

    def __init__(self, session: Session, clock: Clock) -> None:
        self.session = session
        self.clock = clock

    def ingest_payload(
        self,
        vendor: str,
        envelope: dict[str, Any],
        record_array_key: str,
        record_id_key: str,
        machine_id_key: str,
        required_envelope_keys: Sequence[str],
    ) -> IngestResponse:
        """Validate envelope structure, store records raw, apply Layer 1 idempotency, and enqueue jobs."""
        now_utc: datetime = self.clock.now()

        # 1. Structural Envelope Validation
        if not isinstance(envelope, dict):
            raise PayloadValidationError("Request payload must be a JSON object envelope.")

        for req_key in required_envelope_keys:
            if req_key not in envelope or envelope[req_key] is None or envelope[req_key] == "":
                raise PayloadValidationError(
                    f"Envelope missing required key '{req_key}'.",
                    details={"missing_key": req_key, "vendor": vendor},
                )

        schema_version = envelope.get("schema_version")
        if schema_version is not None:
            v_str = str(schema_version).strip()
            if v_str not in SUPPORTED_SCHEMA_VERSIONS:
                raise PayloadValidationError(
                    f"Unsupported schema_version '{v_str}'. Supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
                    details={"schema_version": v_str},
                )

        records_raw = envelope.get(record_array_key)
        if not isinstance(records_raw, list):
            raise PayloadValidationError(
                f"Envelope key '{record_array_key}' must be a list of records.",
                details={"record_array_key": record_array_key},
            )

        # Separate envelope metadata from records array
        envelope_meta = {k: v for k, v in envelope.items() if k != record_array_key}

        # 2. Initialize Ingestion Batch
        batch_id = str(uuid.uuid4())
        batch = IngestionBatch(
            id=batch_id,
            vendor=vendor,
            received_at=now_utc,
            counts={},
            status="PROCESSING",
        )
        self.session.add(batch)

        results: list[RecordIngestResult] = []
        accepted_count = 0
        duplicate_count = 0
        revision_count = 0
        rejected_count = 0

        # Track records seen earlier in this same batch for intra-batch duplicate detection
        seen_in_batch: dict[str, str] = {}

        # 3. Process Each Record Individually
        for item in records_raw:
            # Check structural record format
            if not isinstance(item, dict):
                rejected_count += 1
                raw_rec_id = str(uuid.uuid4())
                self.session.add(
                    RawRecord(
                        id=raw_rec_id,
                        batch_id=batch_id,
                        vendor=vendor,
                        record_id=None,
                        revision=1,
                        payload={"raw_invalid": str(item)},
                        envelope_meta=envelope_meta,
                        payload_sha256="",
                        schema_version=schema_version,
                        received_at=now_utc,
                        resolved_machine_id=None,
                        processing_state="REJECTED_INVALID",
                        rejection_reasons=["Record item is not a JSON object."],
                    )
                )
                results.append(
                    RecordIngestResult(
                        record_id=None,
                        disposition=RecordDisposition.REJECTED,
                        reason="Record item is not a JSON object.",
                    )
                )
                continue

            raw_rec_id_val = item.get(record_id_key)
            rec_id_str = str(raw_rec_id_val).strip() if raw_rec_id_val is not None else ""

            # Check machine key to preserve resolved_machine_id even for rejected items
            raw_mach_val = item.get(machine_id_key)
            mach_id_str = str(raw_mach_val).strip() if raw_mach_val is not None else None
            if mach_id_str == "":
                mach_id_str = None

            if not rec_id_str:
                # Structural rejection: missing record identifier
                rejected_count += 1
                self.session.add(
                    RawRecord(
                        id=str(uuid.uuid4()),
                        batch_id=batch_id,
                        vendor=vendor,
                        record_id=None,
                        revision=1,
                        payload=item,
                        envelope_meta=envelope_meta,
                        payload_sha256="",
                        schema_version=schema_version,
                        received_at=now_utc,
                        resolved_machine_id=mach_id_str,
                        processing_state="REJECTED_INVALID",
                        rejection_reasons=["Missing or empty record identifier."],
                    )
                )
                results.append(
                    RecordIngestResult(
                        record_id=None,
                        disposition=RecordDisposition.REJECTED,
                        reason="Missing or empty record identifier.",
                    )
                )
                continue

            # Compute canonical SHA-256 hash (envelope excluded, numerics normalized)
            content_hash = compute_canonical_record_hash(item)

            # Intra-batch duplicate check
            if rec_id_str in seen_in_batch:
                prior_hash = seen_in_batch[rec_id_str]
                if prior_hash == content_hash:
                    # Same id + same hash within same batch = DUPLICATE no-op
                    duplicate_count += 1
                    results.append(
                        RecordIngestResult(
                            record_id=rec_id_str,
                            disposition=RecordDisposition.DUPLICATE,
                            reason="Duplicate record within same batch.",
                        )
                    )
                    continue

            # Inter-batch Layer 1 Idempotency Check in Database
            existing_records = (
                self.session.query(RawRecord)
                .filter_by(vendor=vendor, record_id=rec_id_str)
                .order_by(RawRecord.revision.desc())
                .all()
            )

            if existing_records:
                latest_existing = existing_records[0]
                if latest_existing.payload_sha256 == content_hash:
                    # Same id + same hash = DUPLICATE no-op
                    duplicate_count += 1
                    seen_in_batch[rec_id_str] = content_hash
                    results.append(
                        RecordIngestResult(
                            record_id=rec_id_str,
                            disposition=RecordDisposition.DUPLICATE,
                        )
                    )
                    continue
                else:
                    # Same id + different hash = REVISION superseding old record
                    revision_count += 1
                    seen_in_batch[rec_id_str] = content_hash
                    new_revision = latest_existing.revision + 1

                    # Mark prior raw records as SUPERSEDED
                    for old_raw in existing_records:
                        old_raw.processing_state = "SUPERSEDED"

                    # Mark prior normalized signals as ineffective
                    self.session.query(NormalizedSignal).filter_by(
                        source_vendor=vendor, source_record_id=rec_id_str
                    ).update({"is_effective": False}, synchronize_session=False)

                    new_raw_id = str(uuid.uuid4())
                    self.session.add(
                        RawRecord(
                            id=new_raw_id,
                            batch_id=batch_id,
                            vendor=vendor,
                            record_id=rec_id_str,
                            revision=new_revision,
                            payload=item,
                            envelope_meta=envelope_meta,
                            payload_sha256=content_hash,
                            schema_version=schema_version,
                            received_at=now_utc,
                            resolved_machine_id=mach_id_str,
                            processing_state="QUEUED",
                            rejection_reasons=[],
                        )
                    )

                    # Enqueue NORMALIZE_PAYLOAD job
                    self.session.add(
                        Job(
                            id=str(uuid.uuid4()),
                            type="NORMALIZE_PAYLOAD",
                            payload={
                                "raw_record_id": new_raw_id,
                                "vendor": vendor,
                                "batch_id": batch_id,
                            },
                            dedupe_key=f"NORMALIZE:{vendor}:{rec_id_str}:{new_revision}",
                            status="PENDING",
                            attempts=0,
                            max_attempts=3,
                            run_after=now_utc,
                            created_at=now_utc,
                            updated_at=now_utc,
                        )
                    )

                    results.append(
                        RecordIngestResult(
                            record_id=rec_id_str,
                            disposition=RecordDisposition.REVISION,
                        )
                    )
                    continue

            # Brand new record -> ACCEPTED
            accepted_count += 1
            seen_in_batch[rec_id_str] = content_hash
            new_raw_id = str(uuid.uuid4())

            self.session.add(
                RawRecord(
                    id=new_raw_id,
                    batch_id=batch_id,
                    vendor=vendor,
                    record_id=rec_id_str,
                    revision=1,
                    payload=item,
                    envelope_meta=envelope_meta,
                    payload_sha256=content_hash,
                    schema_version=schema_version,
                    received_at=now_utc,
                    resolved_machine_id=mach_id_str,
                    processing_state="QUEUED",
                    rejection_reasons=[],
                )
            )

            # Enqueue NORMALIZE_PAYLOAD job
            self.session.add(
                Job(
                    id=str(uuid.uuid4()),
                    type="NORMALIZE_PAYLOAD",
                    payload={
                        "raw_record_id": new_raw_id,
                        "vendor": vendor,
                        "batch_id": batch_id,
                    },
                    dedupe_key=f"NORMALIZE:{vendor}:{rec_id_str}:1",
                    status="PENDING",
                    attempts=0,
                    max_attempts=3,
                    run_after=now_utc,
                    created_at=now_utc,
                    updated_at=now_utc,
                )
            )

            results.append(
                RecordIngestResult(
                    record_id=rec_id_str,
                    disposition=RecordDisposition.ACCEPTED,
                )
            )

        # 4. Finalize Batch Counts & Commit
        batch_counts = {
            "total": len(records_raw),
            "accepted": accepted_count,
            "duplicate": duplicate_count,
            "revision": revision_count,
            "rejected": rejected_count,
        }
        batch.counts = batch_counts
        batch.status = "COMPLETED"
        self.session.commit()

        return IngestResponse(
            batch_id=batch_id,
            status="ACCEPTED",
            total=len(records_raw),
            accepted=accepted_count,
            duplicate=duplicate_count,
            revision=revision_count,
            rejected=rejected_count,
            records=results,
        )
