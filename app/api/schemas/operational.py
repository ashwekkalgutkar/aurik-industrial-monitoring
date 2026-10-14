"""Pydantic schemas for operational machine status views, plant/line summaries, and audit lookups."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MachineStatusResponse(BaseModel):
    """Authoritative machine operational attention view."""

    model_config = ConfigDict(from_attributes=True)

    machine_id: str = Field(description="Unique machine identifier (e.g. EQ-001)")
    plant_id: str = Field(description="Plant location identifier")
    line_id: str = Field(description="Production line identifier")
    machine_name: str | None = Field(default=None, description="Human-readable machine name")
    machine_type: str | None = Field(default=None, description="Type or category of industrial asset")
    criticality: str | None = Field(default=None, description="Equipment criticality: low | medium | high")
    derived_status: str = Field(
        description="Calculated operational health: HEALTHY | WATCH | DEGRADED | CRITICAL | UNDER_MAINTENANCE"
    )
    attention_level: int = Field(description="Normalized attention level from 0 (nominal) to 4 (critical)")
    needs_attention: bool = Field(description="True if machine requires operator or maintenance action (attention >= 2)")
    reason_codes: list[str] = Field(description="Authoritative reason codes explaining status determination")
    explanation: list[dict[str, Any]] = Field(description="Detailed trace explaining each contributing factor")
    latest_relevant_event_time: datetime | None = Field(
        default=None, description="Timestamp of the latest processed event contributing to state"
    )
    last_processed_at: datetime = Field(description="Timestamp when state was recomputed")
    processing_status: str = Field(description="Freshness and reliability state: FRESH | STALE | PARTIAL | FAILED")
    source_event_refs: list[dict[str, Any]] = Field(
        description="Audit references to source vendor records contributing to state"
    )
    rules_version: str = Field(description="Semantic version of the rules engine used for computation")


class PaginatedMachineStatusResponse(BaseModel):
    """Paginated collection of machine operational status views."""

    total: int = Field(description="Total count of machines matching filter criteria")
    page: int = Field(description="Current 1-based page number")
    page_size: int = Field(description="Page size limit")
    items: list[MachineStatusResponse] = Field(description="Page records ordered by attention desc, machine_id asc")


class LineAttentionSummary(BaseModel):
    """Production line summary highlighting assets requiring operational intervention."""

    line_id: str = Field(description="Line identifier")
    line_name: str = Field(description="Line display name")
    plant_id: str = Field(description="Parent plant identifier")
    total_machines: int = Field(description="Total machines configured on line")
    machines_needing_attention: int = Field(description="Count of machines requiring attention (attention >= 2)")
    critical_machines: list[str] = Field(description="List of machine IDs currently in CRITICAL status")
    status_counts: dict[str, int] = Field(description="Count of machines per derived_status")


class PlantSummaryResponse(BaseModel):
    """Plant-level operational summary."""

    plant_id: str = Field(description="Plant identifier")
    plant_name: str = Field(description="Plant name")
    total_machines: int = Field(description="Total machines in plant")
    machines_needing_attention: int = Field(description="Total machines requiring operational intervention")
    critical_machines: list[str] = Field(description="Machine IDs in CRITICAL status")
    status_counts: dict[str, int] = Field(description="Count of machines per derived_status")
    processing_status_counts: dict[str, int] = Field(description="Count of machines per processing_status")
    lines_needing_attention: list[LineAttentionSummary] = Field(
        description="Lines ranked by number of machines requiring attention desc"
    )


class LineSummaryResponse(BaseModel):
    """Detailed line operational summary including all machine operational views."""

    line_id: str = Field(description="Line identifier")
    line_name: str = Field(description="Line name")
    plant_id: str = Field(description="Parent plant identifier")
    total_machines: int = Field(description="Total machines on line")
    machines_needing_attention: int = Field(description="Machines requiring attention")
    critical_machines: list[str] = Field(description="Machine IDs in CRITICAL status")
    status_counts: dict[str, int] = Field(description="Count of machines per derived_status")
    processing_status_counts: dict[str, int] = Field(description="Count of machines per processing_status")
    machines: list[MachineStatusResponse] = Field(description="All machines on this line")


class GlobalSummaryResponse(BaseModel):
    """System-wide operational overview across all monitored plants and production lines."""

    total_machines: int = Field(description="Total machines across all plants")
    machines_needing_attention: int = Field(description="Total machines requiring operational intervention")
    critical_machines: list[str] = Field(description="All machine IDs in CRITICAL status")
    status_counts: dict[str, int] = Field(description="Count of machines per derived_status")
    processing_status_counts: dict[str, int] = Field(description="Count of machines per processing_status")
    plants: list[PlantSummaryResponse] = Field(description="Plant-by-plant breakdowns")
    worst_lines: list[LineAttentionSummary] = Field(description="Top lines with highest number of machines needing attention")


class BatchDetailRecordItem(BaseModel):
    """Raw record item within an ingestion batch."""

    raw_record_id: str
    record_id: str | None = None
    revision: int
    processing_state: str
    received_at: datetime
    rejection_reasons: list[str] = Field(default_factory=list)


class IngestionBatchDetailResponse(BaseModel):
    """Detailed audit view of an ingested batch."""

    batch_id: str
    vendor: str
    source_filename: str | None = None
    received_at: datetime
    status: str
    counts: dict[str, Any] = Field(default_factory=dict)
    records: list[BatchDetailRecordItem] = Field(default_factory=list)


class RawRecordDetailResponse(BaseModel):
    """Deep audit inspection for an individual raw ingested record and its generated signals."""

    id: str
    batch_id: str
    vendor: str
    record_id: str | None = None
    revision: int
    payload_hash: str
    processing_state: str
    received_at: datetime
    rejection_reasons: list[str] = Field(default_factory=list)
    payload: dict[str, Any]
    signals: list[dict[str, Any]] = Field(default_factory=list)
