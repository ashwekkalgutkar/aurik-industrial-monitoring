"""Administrative endpoints for queue introspection, job requeueing, and operational metrics."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.errors import NotFoundError
from app.core.security import get_api_key_validator
from app.db.base import get_db_session
from app.db.models import Job, NormalizedSignal, RawRecord
from app.vendors.enums import CanonicalEventType
from app.workers.queue import PostgresJobQueue

router = APIRouter(prefix="/v1/admin", tags=["Admin"])
require_admin_auth = Depends(get_api_key_validator("admin"))

SessionDep = Annotated[Session, Depends(get_db_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]


@router.get(
    "/jobs",
    dependencies=[require_admin_auth],
    summary="List queue jobs with optional status filter",
)
def list_jobs(
    session: SessionDep,
    job_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Retrieve queue jobs for introspection and DLQ monitoring."""
    query = session.query(Job)
    if job_status:
        query = query.filter(Job.status == job_status.upper())

    total = query.count()
    jobs = query.order_by(Job.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "jobs": [
            {
                "id": j.id,
                "type": j.type,
                "status": j.status,
                "attempts": j.attempts,
                "max_attempts": j.max_attempts,
                "run_after": j.run_after.isoformat() if j.run_after else None,
                "locked_until": j.locked_until.isoformat() if j.locked_until else None,
                "last_error": j.last_error,
                "created_at": j.created_at.isoformat(),
                "payload": j.payload,
            }
            for j in jobs
        ],
    }


@router.post(
    "/jobs/{job_id}/requeue",
    status_code=status.HTTP_200_OK,
    dependencies=[require_admin_auth],
    summary="Requeue a dead or failed job back to PENDING",
)
def requeue_job(
    job_id: str,
    session: SessionDep,
    clock: ClockDep,
) -> dict[str, Any]:
    """Requeue a dead or failed job."""
    queue = PostgresJobQueue(session, clock)
    success = queue.requeue_job(job_id)
    if not success:
        raise NotFoundError(
            f"Job '{job_id}' not found or not in a retryable state (must be DEAD or FAILED_RETRYABLE)."
        )
    return {"message": f"Job '{job_id}' successfully reset to PENDING."}


@router.post(
    "/machines/{machine_id}/recompute",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[require_admin_auth],
    summary="Manually trigger operational status recompute for a machine",
)
def recompute_machine(
    machine_id: str,
    session: SessionDep,
    clock: ClockDep,
) -> dict[str, Any]:
    """Enqueue a machine status recompute job."""
    queue = PostgresJobQueue(session, clock)
    queue.enqueue_recompute(machine_id)
    return {"message": f"Recomputation enqueued for machine '{machine_id}'."}


@router.get(
    "/metrics",
    dependencies=[require_admin_auth],
    summary="Retrieve operational queue depths, error rates, and system telemetry",
)
def get_metrics(session: SessionDep) -> dict[str, Any]:
    """Return operational queue metrics, dead-letter counts, and rejection totals."""
    # 1. Queue depth by status
    status_counts = session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    queue_depth_by_status = dict(status_counts)

    # 2. Rejections by vendor
    rejections = (
        session.query(RawRecord.vendor, func.count(RawRecord.id))
        .filter(RawRecord.processing_state == "REJECTED_INVALID")
        .group_by(RawRecord.vendor)
        .all()
    )
    rejections_by_vendor = dict(rejections)

    # 3. Unknown-event counts
    unknown_event_count = (
        session.query(func.count(NormalizedSignal.id))
        .filter(NormalizedSignal.canonical_event_type == CanonicalEventType.UNKNOWN_EVENT)
        .scalar()
        or 0
    )

    return {
        "queue_depth_by_status": queue_depth_by_status,
        "dead_jobs_count": queue_depth_by_status.get("DEAD", 0),
        "pending_jobs_count": queue_depth_by_status.get("PENDING", 0),
        "running_jobs_count": queue_depth_by_status.get("RUNNING", 0),
        "rejections_by_vendor": rejections_by_vendor,
        "unknown_event_count": unknown_event_count,
    }
