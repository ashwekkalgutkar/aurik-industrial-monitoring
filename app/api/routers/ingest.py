"""FastAPI router for external vendor data ingestion."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.schemas.ingest import IngestResponse
from app.core.clock import Clock, get_clock
from app.core.security import get_api_key_validator
from app.db.base import get_db_session
from app.services.ingest_service import IngestionService

router = APIRouter(prefix="/v1/ingest", tags=["Ingestion"])
require_ingest_auth = Depends(get_api_key_validator("ingest"))

SessionDep = Annotated[Session, Depends(get_db_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]


@router.post(
    "/pulseforge",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestResponse,
    dependencies=[require_ingest_auth],
    summary="Ingest PulseForge event envelope",
)
def ingest_pulseforge(
    envelope: dict[str, Any],
    session: SessionDep,
    clock: ClockDep,
) -> IngestResponse:
    """Accept and persist a PulseForge batch envelope."""
    service = IngestionService(session, clock)
    return service.ingest_payload(
        vendor="PulseForge",
        envelope=envelope,
        record_array_key="events",
        record_id_key="event_id",
        machine_id_key="machine_id",
        required_envelope_keys=["vendor", "plant_id", "events"],
    )


@router.post(
    "/thermexwatch",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestResponse,
    dependencies=[require_ingest_auth],
    summary="Ingest ThermexWatch readings envelope",
)
def ingest_thermexwatch(
    envelope: dict[str, Any],
    session: SessionDep,
    clock: ClockDep,
) -> IngestResponse:
    """Accept and persist a ThermexWatch batch envelope."""
    service = IngestionService(session, clock)
    return service.ingest_payload(
        vendor="ThermexWatch",
        envelope=envelope,
        record_array_key="readings",
        record_id_key="readingId",
        machine_id_key="assetCode",
        required_envelope_keys=["source", "site_code", "readings"],
    )


@router.post(
    "/maintaflow",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestResponse,
    dependencies=[require_ingest_auth],
    summary="Ingest MaintaFlow records envelope",
)
def ingest_maintaflow(
    envelope: dict[str, Any],
    session: SessionDep,
    clock: ClockDep,
) -> IngestResponse:
    """Accept and persist a MaintaFlow batch envelope."""
    service = IngestionService(session, clock)
    return service.ingest_payload(
        vendor="MaintaFlow",
        envelope=envelope,
        record_array_key="records",
        record_id_key="record_id",
        machine_id_key="machine_ref",
        required_envelope_keys=["provider_name", "factory_id", "records"],
    )


@router.post(
    "/work-orders",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestResponse,
    dependencies=[require_ingest_auth],
    summary="Ingest internal CMMS work orders envelope",
)
def ingest_work_orders(
    envelope: dict[str, Any],
    session: SessionDep,
    clock: ClockDep,
) -> IngestResponse:
    """Accept and persist an internal CMMS work orders envelope."""
    service = IngestionService(session, clock)
    return service.ingest_payload(
        vendor="CMMS",
        envelope=envelope,
        record_array_key="work_orders",
        record_id_key="work_order_id",
        machine_id_key="machine_id",
        required_envelope_keys=["source", "work_orders"],
    )
