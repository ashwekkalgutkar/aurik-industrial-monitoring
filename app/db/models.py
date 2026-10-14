"""Typed SQLAlchemy 2.x data models with justified indexes.

Indexes are designed specifically for:
1. Low-latency Layer 1 and Layer 2 idempotency checks.
2. Concurrent, lock-free queue claiming via SELECT ... FOR UPDATE SKIP LOCKED.
3. Rapid chronological signal retrieval for the deterministic status engine.
4. Fast aggregation for plant and line operational summaries.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Plant(Base):
    """Manufacturing facility reference."""

    __tablename__ = "plants"

    plant_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    lines: Mapped[list["Line"]] = relationship("Line", back_populates="plant")
    machines: Mapped[list["Machine"]] = relationship("Machine", back_populates="plant")


class Line(Base):
    """Production line reference."""

    __tablename__ = "lines"

    line_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    plant_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("plants.plant_id", ondelete="CASCADE"), nullable=False
    )
    line_name: Mapped[str] = mapped_column(String(100), nullable=False)
    operating_window: Mapped[str] = mapped_column(String(50), nullable=False)

    plant: Mapped["Plant"] = relationship("Plant", back_populates="lines")
    machines: Mapped[list["Machine"]] = relationship("Machine", back_populates="line")


class Machine(Base):
    """Authoritative industrial equipment master record with rated limits."""

    __tablename__ = "machines"

    machine_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    plant_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("plants.plant_id", ondelete="RESTRICT"), nullable=False
    )
    line_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("lines.line_id", ondelete="RESTRICT"), nullable=False
    )
    machine_type: Mapped[str] = mapped_column(String(100), nullable=False)
    criticality: Mapped[str] = mapped_column(String(20), nullable=False)  # high | medium | low
    installed_date: Mapped[date] = mapped_column(Date, nullable=False)
    rated_max_temp_c: Mapped[float] = mapped_column(Float, nullable=False)
    rated_max_vibration_mm_s: Mapped[float] = mapped_column(Float, nullable=False)
    baseline_power_kw: Mapped[float] = mapped_column(Float, nullable=False)
    asset_status: Mapped[str] = mapped_column(String(50), nullable=False)  # active | maintenance

    plant: Mapped["Plant"] = relationship("Plant", back_populates="machines")
    line: Mapped["Line"] = relationship("Line", back_populates="machines")
    signals: Mapped[list["NormalizedSignal"]] = relationship(
        "NormalizedSignal", back_populates="machine"
    )
    state_view: Mapped["MachineStateView"] = relationship(
        "MachineStateView", back_populates="machine", uselist=False
    )

    __table_args__ = (
        # Index to accelerate filtering machines by plant and line during summary rollups
        Index("idx_machines_plant_line", "plant_id", "line_id"),
    )


class IngestionBatch(Base):
    """Tracking record for an ingested HTTP request payload batch."""

    __tablename__ = "ingestion_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    vendor: Mapped[str] = mapped_column(String(50), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )  # total, accepted, duplicate, revision, rejected
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="RECEIVED"
    )  # RECEIVED | PROCESSING | COMPLETED | FAILED

    raw_records: Mapped[list["RawRecord"]] = relationship("RawRecord", back_populates="batch")


class RawRecord(Base):
    """Raw payload store preserving the unmodified incoming record and ingestion lineage."""

    __tablename__ = "raw_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("ingestion_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    vendor: Mapped[str] = mapped_column(String(50), nullable=False)
    record_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # Exact original record
    envelope_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )  # Batch-level headers/metadata
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_machine_id: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # Extracted machine ID even if invalid
    processing_state: Mapped[str] = mapped_column(
        String(50), nullable=False, default="QUEUED"
    )  # QUEUED | PROCESSED | REJECTED_INVALID | DEAD | SUPERSEDED | DUPLICATE_CONTENT
    rejection_reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    batch: Mapped["IngestionBatch"] = relationship("IngestionBatch", back_populates="raw_records")
    signals: Mapped[list["NormalizedSignal"]] = relationship(
        "NormalizedSignal", back_populates="raw_record"
    )

    __table_args__ = (
        # Mandatory constraint: uniquely identify a record revision within a vendor
        UniqueConstraint(
            "vendor", "record_id", "revision", name="uq_raw_records_vendor_record_revision"
        ),
        # Fast lookup for Layer 1 idempotency: find existing records by vendor and record_id
        Index("idx_raw_records_vendor_record_id", "vendor", "record_id"),
        # Querying rejected/pending records by machine within the TTL window for processing_status computation
        Index("idx_raw_records_machine_received", "resolved_machine_id", "received_at"),
    )


class NormalizedSignal(Base):
    """Canonicalized event signal extracted from raw records and evaluated by the domain engine."""

    __tablename__ = "normalized_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    raw_record_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("raw_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_vendor: Mapped[str] = mapped_column(String(50), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(100), nullable=False)
    machine_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("machines.machine_id", ondelete="CASCADE"),
        nullable=False,
    )
    plant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    line_id: Mapped[str] = mapped_column(String(50), nullable=False)
    signal_kind: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # ALERT | TELEMETRY | INSPECTION | MAINTENANCE | CALIBRATION | OPERATOR_NOTE | WORK_ORDER
    canonical_event_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # VIBRATION_HIGH | TEMPERATURE_HIGH | etc.
    dimension: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # VIBRATION | TEMPERATURE | POWER | SENSOR | MAINTENANCE | NONE
    severity: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0 to 4
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )  # Source event timestamp
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )  # Ingestion timestamp
    vibration_mm_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    vibration_g_raw: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    power_kw: Mapped[float | None] = mapped_column(Float, nullable=True)
    machine_state: Mapped[str | None] = mapped_column(String(30), nullable=True)  # running | idle
    sensor_quality: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0.0 to 1.0
    vendor_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0.0 to 1.0
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    data_quality_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    duplicate_of: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )  # Layer 2 content duplicate reference
    is_effective: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )  # False if superseded or duplicate content

    raw_record: Mapped["RawRecord"] = relationship("RawRecord", back_populates="signals")
    machine: Mapped["Machine"] = relationship("Machine", back_populates="signals")

    __table_args__ = (
        # Primary engine query index: scan effective signals chronologically for a specific machine
        Index("idx_signals_machine_effective_time", "machine_id", "is_effective", "event_time"),
        # Layer 2 semantic deduplication index: find existing signals matching content signature
        Index(
            "idx_signals_semantic_dedupe",
            "source_vendor",
            "machine_id",
            "event_time",
            "canonical_event_type",
        ),
    )


class Job(Base):
    """PostgreSQL-backed queue job consumed via SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # NORMALIZE_PAYLOAD | RECOMPUTE_MACHINE
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING"
    )  # PENDING | RUNNING | SUCCEEDED | FAILED_RETRYABLE | DEAD
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        # Worker queue polling index: claim pending/retryable jobs ready for execution
        Index("idx_jobs_claim", "status", "run_after", "locked_until"),
        # Partial unique index: deduplicate against PENDING jobs only
        # Ensures that a job currently RUNNING does not prevent enqueuing a fresh recompute
        Index(
            "uq_jobs_dedupe_pending",
            "dedupe_key",
            unique=True,
            postgresql_where=(status == "PENDING"),
        ),
    )


class MachineStateView(Base):
    """Materialized operational state view for a single machine with optimistic versioning."""

    __tablename__ = "machine_state_view"

    machine_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("machines.machine_id", ondelete="CASCADE"), primary_key=True
    )
    plant_id: Mapped[str] = mapped_column(String(50), nullable=False)
    line_id: Mapped[str] = mapped_column(String(50), nullable=False)
    derived_status: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # HEALTHY | WATCH | DEGRADED | CRITICAL | UNDER_MAINTENANCE | UNKNOWN
    attention_level: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 to 4
    needs_attention: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    explanation: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    latest_relevant_event_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processing_status: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # FRESH | STALE | PARTIAL | FAILED
    source_event_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    rules_version: Mapped[str] = mapped_column(String(20), nullable=False)
    state_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )  # Optimistic concurrency lock

    machine: Mapped["Machine"] = relationship("Machine", back_populates="state_view")

    __table_args__ = (
        # Filter and rank machines efficiently by plant, line, attention, and status
        Index(
            "idx_machine_state_view_summary",
            "plant_id",
            "line_id",
            "derived_status",
            "attention_level",
        ),
    )
