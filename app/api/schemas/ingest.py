"""Schemas for vendor ingestion requests and HTTP 202 batch responses."""

from enum import StrEnum

from pydantic import BaseModel, Field


class RecordDisposition(StrEnum):
    """Disposition of an individual record evaluated during ingestion."""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    REVISION = "REVISION"
    REJECTED = "REJECTED"


class RecordIngestResult(BaseModel):
    """Status summary for a single record within an ingestion batch."""

    record_id: str | None = Field(default=None, description="Extracted vendor record identifier.")
    disposition: RecordDisposition = Field(description="Ingest disposition outcome.")
    reason: str | None = Field(default=None, description="Structural rejection reason if rejected.")


class IngestResponse(BaseModel):
    """Standard HTTP 202 response returned by all vendor ingest endpoints."""

    batch_id: str = Field(description="Unique tracking identifier for this ingestion batch.")
    status: str = Field(default="ACCEPTED", description="Overall batch ingestion status.")
    total: int = Field(description="Total records processed in the request envelope.")
    accepted: int = Field(description="Count of newly accepted records enqueued for processing.")
    duplicate: int = Field(description="Count of duplicate records ignored as no-ops.")
    revision: int = Field(description="Count of superseded revisions enqueued for reprocessing.")
    rejected: int = Field(description="Count of structurally rejected records stored raw.")
    records: list[RecordIngestResult] = Field(
        description="Per-record ingestion dispositions in order."
    )
