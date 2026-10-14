"""Operational read endpoints for machine states, plant/line summaries, batch audits, and health checks."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.api.schemas.operational import (
    BatchDetailRecordItem,
    ContributingReasonExplanation,
    GlobalSummaryResponse,
    IngestionBatchDetailResponse,
    LineAttentionSummary,
    LineSummaryResponse,
    MachineStatusResponse,
    PaginatedMachineStatusResponse,
    PlantSummaryResponse,
    RawRecordDetailResponse,
    SourceEventRef,
)
from app.core.clock import Clock, get_clock
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.security import get_api_key_validator
from app.db.base import get_db_session
from app.db.models import (
    IngestionBatch,
    Job,
    Line,
    Machine,
    MachineStateView,
    NormalizedSignal,
    Plant,
    RawRecord,
)
from app.domain.engine import evaluate_machine_state, recompute_machine_state

router = APIRouter(tags=["Operational Read"])

SessionDep = Annotated[Session, Depends(get_db_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]
require_read_auth = Depends(get_api_key_validator("read"))

# Query Parameter Annotated Types to satisfy B008
PlantIdQuery = Annotated[str | None, Query(description="Filter by plant")]
LineIdQuery = Annotated[str | None, Query(description="Filter by production line")]
DerivedStatusQuery = Annotated[str | None, Query(description="Filter by derived operational status")]
NeedsAttentionQuery = Annotated[bool | None, Query(description="Filter by needs_attention boolean")]
MinAttentionQuery = Annotated[int | None, Query(ge=0, le=4, description="Minimum attention level")]
PageQuery = Annotated[int, Query(ge=1, description="1-based page number")]
PageSizeQuery = Annotated[int, Query(ge=1, le=100, description="Items per page limit")]
AsOfQuery = Annotated[datetime | None, Query(description="Optional point-in-time timestamp (ISO UTC)")]


# ===========================================================================
# 1. Health & Liveness / Readiness Probes (Unauthenticated for Orchestrators)
# ===========================================================================
@router.get(
    "/healthz",
    summary="Liveness check",
    tags=["Health"],
)
def healthz(clock: ClockDep) -> dict[str, Any]:
    """Lightweight liveness probe for load balancers and orchestrators."""
    return {
        "status": "ok",
        "timestamp": clock.now().isoformat(),
        "version": "1.0.0",
    }


@router.get(
    "/readyz",
    summary="Readiness check",
    tags=["Health"],
)
def readyz(session: SessionDep) -> Response:
    """Readiness probe validating database connectivity."""
    try:
        session.execute(text("SELECT 1"))
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ready", "database": "connected"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "database": "unreachable", "error": str(exc)},
        )


# ===========================================================================
# 2. Machine Operational Status Views
# ===========================================================================
def _build_machine_status_response(
    machine: Machine,
    view: MachineStateView,
    as_of: datetime | None = None,
) -> MachineStatusResponse:
    """Helper to merge machine metadata and derived state view into response schema."""
    return MachineStatusResponse(
        machine_id=machine.machine_id,
        plant_id=machine.plant_id,
        line_id=machine.line_id,
        machine_name=machine.machine_id,
        machine_type=machine.machine_type,
        criticality=machine.criticality,
        derived_status=view.derived_status,
        attention_level=view.attention_level,
        needs_attention=view.needs_attention,
        reason_codes=view.reason_codes,
        explanation=[
            e if isinstance(e, ContributingReasonExplanation) else ContributingReasonExplanation(**e)
            for e in (view.explanation or [])
        ],
        latest_relevant_event_time=view.latest_relevant_event_time,
        last_processed_at=view.last_processed_at,
        processing_status=view.processing_status,
        source_event_refs=[
            s if isinstance(s, SourceEventRef) else SourceEventRef(**s)
            for s in (view.source_event_refs or [])
        ],
        rules_version=view.rules_version,
        as_of=as_of,
    )


def _get_or_compute_state_view(
    machine: Machine,
    session: Session,
    as_of: datetime | None,
    clock: Clock,
) -> MachineStatusResponse:
    """Retrieve existing state view or evaluate pure status engine on the fly."""
    settings = get_settings()
    if as_of is not None:
        # Time-travel dynamic evaluation
        signals = (
            session.query(NormalizedSignal)
            .filter(
                NormalizedSignal.machine_id == machine.machine_id,
                NormalizedSignal.is_effective.is_(True),
                NormalizedSignal.event_time <= as_of,
            )
            .order_by(NormalizedSignal.event_time.asc())
            .all()
        )
        raw_records = (
            session.query(RawRecord)
            .filter(
                RawRecord.resolved_machine_id == machine.machine_id,
                RawRecord.received_at <= as_of,
            )
            .all()
        )
        dead_jobs = (
            session.query(Job)
            .filter(
                Job.status == "DEAD",
                Job.payload["machine_id"].astext == machine.machine_id,
            )
            .all()
        )
        result = evaluate_machine_state(
            machine=machine,
            effective_signals=signals,
            raw_records=raw_records,
            dead_jobs=dead_jobs,
            as_of=as_of,
            settings=settings,
        )
        return MachineStatusResponse(
            machine_id=machine.machine_id,
            plant_id=machine.plant_id,
            line_id=machine.line_id,
            machine_name=machine.machine_id,
            machine_type=machine.machine_type,
            criticality=machine.criticality,
            derived_status=result.derived_status,
            attention_level=result.attention_level,
            needs_attention=result.needs_attention,
            reason_codes=result.reason_codes,
            explanation=[
                e if isinstance(e, ContributingReasonExplanation) else ContributingReasonExplanation(**e)
                for e in (result.explanation or [])
            ],
            latest_relevant_event_time=result.latest_relevant_event_time,
            last_processed_at=clock.now(),
            processing_status=result.processing_status,
            source_event_refs=[
                s if isinstance(s, SourceEventRef) else SourceEventRef(**s)
                for s in (result.source_event_refs or [])
            ],
            rules_version=result.rules_version,
            as_of=as_of,
        )

    # Standard query for persisted view
    view = session.get(MachineStateView, machine.machine_id)
    if not view:
        # Recompute synchronously if missing
        view = recompute_machine_state(machine.machine_id, session, as_of=clock.now())

    return _build_machine_status_response(machine, view, as_of=clock.now())


@router.get(
    "/v1/machines/status",
    response_model=PaginatedMachineStatusResponse,
    dependencies=[require_read_auth],
    summary="List machine operational status views with filtering and pagination",
)
def list_machines_status(
    session: SessionDep,
    clock: ClockDep,
    plant_id: PlantIdQuery = None,
    line_id: LineIdQuery = None,
    derived_status: DerivedStatusQuery = None,
    needs_attention: NeedsAttentionQuery = None,
    min_attention_level: MinAttentionQuery = None,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
    as_of: AsOfQuery = None,
) -> PaginatedMachineStatusResponse:
    """Retrieve a paginated, sorted list of machine operational statuses.

    Sorted by attention level descending (highest urgency first), then machine_id ascending.
    """
    eval_as_of = as_of or clock.now()
    if as_of is not None:
        # Dynamically compute for all matching machines
        machine_query = session.query(Machine)
        if plant_id:
            machine_query = machine_query.filter(Machine.plant_id == plant_id)
        if line_id:
            machine_query = machine_query.filter(Machine.line_id == line_id)

        all_machines = machine_query.all()
        computed_items: list[MachineStatusResponse] = []
        for m in all_machines:
            item = _get_or_compute_state_view(m, session, as_of, clock)
            if derived_status and item.derived_status != derived_status.upper():
                continue
            if needs_attention is not None and item.needs_attention != needs_attention:
                continue
            if min_attention_level is not None and item.attention_level < min_attention_level:
                continue
            computed_items.append(item)

        # Sort: attention_level desc, machine_id asc
        computed_items.sort(key=lambda x: (-x.attention_level, x.machine_id))
        total = len(computed_items)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        return PaginatedMachineStatusResponse(
            total=total,
            page=page,
            page_size=page_size,
            items=computed_items[start_idx:end_idx],
            as_of=eval_as_of,
        )

    # Database query joining Machine and MachineStateView
    query = session.query(MachineStateView, Machine).join(
        Machine, Machine.machine_id == MachineStateView.machine_id
    )

    if plant_id:
        query = query.filter(MachineStateView.plant_id == plant_id)
    if line_id:
        query = query.filter(MachineStateView.line_id == line_id)
    if derived_status:
        query = query.filter(MachineStateView.derived_status == derived_status.upper())
    if needs_attention is not None:
        query = query.filter(MachineStateView.needs_attention == needs_attention)
    if min_attention_level is not None:
        query = query.filter(MachineStateView.attention_level >= min_attention_level)

    total = query.count()
    rows = (
        query.order_by(MachineStateView.attention_level.desc(), Machine.machine_id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    items = [_build_machine_status_response(mach, view, as_of=eval_as_of) for view, mach in rows]
    return PaginatedMachineStatusResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
        as_of=eval_as_of,
    )


@router.get(
    "/v1/machines/{machine_id}/status",
    response_model=MachineStatusResponse,
    dependencies=[require_read_auth],
    summary="Get single machine operational status view",
)
def get_machine_status(
    machine_id: str,
    session: SessionDep,
    clock: ClockDep,
    as_of: AsOfQuery = None,
) -> MachineStatusResponse:
    """Retrieve operational state for a specific machine."""
    machine = session.get(Machine, machine_id)
    if not machine:
        raise NotFoundError(f"Machine '{machine_id}' does not exist.")

    return _get_or_compute_state_view(machine, session, as_of, clock)


# ===========================================================================
# 3. Plant & Line Operational Summaries
# ===========================================================================
def _compute_line_attention_summary(
    line: Line,
    views: list[MachineStatusResponse],
) -> LineAttentionSummary:
    """Aggregate machine views into a LineAttentionSummary."""
    status_counts = {"HEALTHY": 0, "WATCH": 0, "DEGRADED": 0, "CRITICAL": 0, "UNDER_MAINTENANCE": 0}
    critical_machines: list[str] = []
    needing_attention = 0

    for v in views:
        status_counts[v.derived_status] = status_counts.get(v.derived_status, 0) + 1
        if v.needs_attention:
            needing_attention += 1
        if v.derived_status == "CRITICAL" or v.attention_level == 4:
            critical_machines.append(v.machine_id)

    return LineAttentionSummary(
        line_id=line.line_id,
        line_name=line.line_name,
        plant_id=line.plant_id,
        total_machines=len(views),
        machines_needing_attention=needing_attention,
        critical_machines=critical_machines,
        status_counts=status_counts,
    )


@router.get(
    "/v1/plants/{plant_id}/summary",
    response_model=PlantSummaryResponse,
    dependencies=[require_read_auth],
    summary="Get plant-level operational summary",
)
def get_plant_summary(
    plant_id: str,
    session: SessionDep,
    clock: ClockDep,
    as_of: AsOfQuery = None,
) -> PlantSummaryResponse:
    """Retrieve operational summary and worst lines for a plant."""
    plant = session.get(Plant, plant_id)
    if not plant:
        raise NotFoundError(f"Plant '{plant_id}' not found.")

    lines = session.query(Line).filter(Line.plant_id == plant_id).all()
    machines = session.query(Machine).filter(Machine.plant_id == plant_id).all()

    machine_views: list[MachineStatusResponse] = [
        _get_or_compute_state_view(m, session, as_of, clock) for m in machines
    ]

    status_counts = {"HEALTHY": 0, "WATCH": 0, "DEGRADED": 0, "CRITICAL": 0, "UNDER_MAINTENANCE": 0}
    proc_counts = {"FRESH": 0, "STALE": 0, "PARTIAL": 0, "FAILED": 0}
    critical_machines: list[str] = []
    needing_attention = 0

    machines_by_line: dict[str, list[MachineStatusResponse]] = {line.line_id: [] for line in lines}
    for v in machine_views:
        status_counts[v.derived_status] = status_counts.get(v.derived_status, 0) + 1
        proc_counts[v.processing_status] = proc_counts.get(v.processing_status, 0) + 1
        if v.needs_attention:
            needing_attention += 1
        if v.derived_status == "CRITICAL" or v.attention_level == 4:
            critical_machines.append(v.machine_id)
        if v.line_id in machines_by_line:
            machines_by_line[v.line_id].append(v)

    line_summaries: list[LineAttentionSummary] = []
    for line in lines:
        line_views = machines_by_line.get(line.line_id, [])
        line_summaries.append(_compute_line_attention_summary(line, line_views))

    # Sort lines by machines needing attention descending (ties by critical count then line_id)
    line_summaries.sort(
        key=lambda line_sum: (
            -line_sum.machines_needing_attention,
            -len(line_sum.critical_machines),
            line_sum.line_id,
        )
    )

    return PlantSummaryResponse(
        plant_id=plant.plant_id,
        plant_name=plant.name or plant.plant_id,
        total_machines=len(machines),
        machines_needing_attention=needing_attention,
        critical_machines=critical_machines,
        status_counts=status_counts,
        processing_status_counts=proc_counts,
        lines_needing_attention=line_summaries,
    )


@router.get(
    "/v1/lines/{line_id}/summary",
    response_model=LineSummaryResponse,
    dependencies=[require_read_auth],
    summary="Get line-level operational summary with all machines",
)
def get_line_summary(
    line_id: str,
    session: SessionDep,
    clock: ClockDep,
    as_of: AsOfQuery = None,
) -> LineSummaryResponse:
    """Retrieve operational summary and machine-by-machine attention view for a line."""
    line = session.get(Line, line_id)
    if not line:
        raise NotFoundError(f"Line '{line_id}' not found.")

    machines = (
        session.query(Machine)
        .filter(Machine.line_id == line_id)
        .order_by(Machine.machine_id.asc())
        .all()
    )
    machine_views = [_get_or_compute_state_view(m, session, as_of, clock) for m in machines]

    status_counts = {"HEALTHY": 0, "WATCH": 0, "DEGRADED": 0, "CRITICAL": 0, "UNDER_MAINTENANCE": 0}
    proc_counts = {"FRESH": 0, "STALE": 0, "PARTIAL": 0, "FAILED": 0}
    critical_machines: list[str] = []
    needing_attention = 0

    for v in machine_views:
        status_counts[v.derived_status] = status_counts.get(v.derived_status, 0) + 1
        proc_counts[v.processing_status] = proc_counts.get(v.processing_status, 0) + 1
        if v.needs_attention:
            needing_attention += 1
        if v.derived_status == "CRITICAL" or v.attention_level == 4:
            critical_machines.append(v.machine_id)

    # Sort machines: attention_level desc, machine_id asc
    machine_views.sort(key=lambda m: (-m.attention_level, m.machine_id))

    return LineSummaryResponse(
        line_id=line.line_id,
        line_name=line.line_name,
        plant_id=line.plant_id,
        total_machines=len(machines),
        machines_needing_attention=needing_attention,
        critical_machines=critical_machines,
        status_counts=status_counts,
        processing_status_counts=proc_counts,
        machines=machine_views,
    )


@router.get(
    "/v1/summary",
    response_model=GlobalSummaryResponse,
    dependencies=[require_read_auth],
    summary="Get global system-wide operational summary across all plants and lines",
)
def get_global_summary(
    session: SessionDep,
    clock: ClockDep,
    as_of: AsOfQuery = None,
) -> GlobalSummaryResponse:
    """Retrieve global system-wide operational attention overview."""
    plants = session.query(Plant).order_by(Plant.plant_id.asc()).all()
    lines = session.query(Line).order_by(Line.line_id.asc()).all()
    machines = session.query(Machine).order_by(Machine.machine_id.asc()).all()

    machine_views = [_get_or_compute_state_view(m, session, as_of, clock) for m in machines]

    status_counts = {"HEALTHY": 0, "WATCH": 0, "DEGRADED": 0, "CRITICAL": 0, "UNDER_MAINTENANCE": 0}
    proc_counts = {"FRESH": 0, "STALE": 0, "PARTIAL": 0, "FAILED": 0}
    critical_machines: list[str] = []
    needing_attention = 0

    views_by_machine_id = {v.machine_id: v for v in machine_views}

    for v in machine_views:
        status_counts[v.derived_status] = status_counts.get(v.derived_status, 0) + 1
        proc_counts[v.processing_status] = proc_counts.get(v.processing_status, 0) + 1
        if v.needs_attention:
            needing_attention += 1
        if v.derived_status == "CRITICAL" or v.attention_level == 4:
            critical_machines.append(v.machine_id)

    # Line summaries
    all_line_summaries: list[LineAttentionSummary] = []
    for line in lines:
        line_machines = [m for m in machines if m.line_id == line.line_id]
        line_views = [views_by_machine_id[m.machine_id] for m in line_machines if m.machine_id in views_by_machine_id]
        all_line_summaries.append(_compute_line_attention_summary(line, line_views))

    # Worst lines ranked by machines needing attention desc (ties broken by critical count then line_id)
    worst_lines = sorted(
        all_line_summaries,
        key=lambda line_sum: (
            -line_sum.machines_needing_attention,
            -len(line_sum.critical_machines),
            line_sum.line_id,
        ),
    )

    # Plant summaries
    plant_summaries: list[PlantSummaryResponse] = []
    for plant in plants:
        p_lines = [line_sum for line_sum in all_line_summaries if line_sum.plant_id == plant.plant_id]
        p_machines = [m for m in machines if m.plant_id == plant.plant_id]
        p_views = [views_by_machine_id[m.machine_id] for m in p_machines if m.machine_id in views_by_machine_id]

        p_status = {"HEALTHY": 0, "WATCH": 0, "DEGRADED": 0, "CRITICAL": 0, "UNDER_MAINTENANCE": 0}
        p_proc = {"FRESH": 0, "STALE": 0, "PARTIAL": 0, "FAILED": 0}
        p_crit: list[str] = []
        p_need = 0

        for pv in p_views:
            p_status[pv.derived_status] = p_status.get(pv.derived_status, 0) + 1
            p_proc[pv.processing_status] = p_proc.get(pv.processing_status, 0) + 1
            if pv.needs_attention:
                p_need += 1
            if pv.derived_status == "CRITICAL" or pv.attention_level == 4:
                p_crit.append(pv.machine_id)

        plant_summaries.append(
            PlantSummaryResponse(
                plant_id=plant.plant_id,
                plant_name=plant.name or plant.plant_id,
                total_machines=len(p_machines),
                machines_needing_attention=p_need,
                critical_machines=p_crit,
                status_counts=p_status,
                processing_status_counts=p_proc,
                lines_needing_attention=sorted(
                    p_lines,
                    key=lambda line_sum: (
                        -line_sum.machines_needing_attention,
                        -len(line_sum.critical_machines),
                        line_sum.line_id,
                    ),
                ),
            )
        )

    return GlobalSummaryResponse(
        total_machines=len(machines),
        machines_needing_attention=needing_attention,
        critical_machines=critical_machines,
        status_counts=status_counts,
        processing_status_counts=proc_counts,
        plants=plant_summaries,
        worst_lines=worst_lines,
    )


# ===========================================================================
# 4. Ingestion Batch & Record Deep Audit Lookups
# ===========================================================================
@router.get(
    "/v1/ingestion/batches/{batch_id}",
    response_model=IngestionBatchDetailResponse,
    dependencies=[require_read_auth],
    summary="Get ingestion batch audit details",
)
def get_batch_details(
    batch_id: str,
    session: SessionDep,
) -> IngestionBatchDetailResponse:
    """Retrieve audit record for an ingestion batch and all associated records."""
    batch = session.get(IngestionBatch, batch_id)
    if not batch:
        raise NotFoundError(f"Ingestion batch '{batch_id}' not found.")

    records = (
        session.query(RawRecord)
        .filter(RawRecord.batch_id == batch_id)
        .order_by(RawRecord.received_at.asc())
        .all()
    )

    record_items = [
        BatchDetailRecordItem(
            raw_record_id=r.id,
            record_id=r.record_id,
            revision=r.revision,
            processing_state=r.processing_state,
            received_at=r.received_at,
            rejection_reasons=r.rejection_reasons,
        )
        for r in records
    ]

    return IngestionBatchDetailResponse(
        batch_id=batch.id,
        vendor=batch.vendor,
        source_filename=batch.counts.get("source_filename") if isinstance(batch.counts, dict) else None,
        received_at=batch.received_at,
        status=batch.status,
        counts=batch.counts,
        records=record_items,
    )


@router.get(
    "/v1/ingestion/records/{vendor}/{record_id}",
    response_model=RawRecordDetailResponse,
    dependencies=[require_read_auth],
    summary="Get raw record audit details with generated signals",
)
def get_record_details(
    vendor: str,
    record_id: str,
    session: SessionDep,
) -> RawRecordDetailResponse:
    """Retrieve raw record and its generated normalized signals."""
    raw_record = (
        session.query(RawRecord)
        .filter(RawRecord.vendor == vendor, RawRecord.record_id == record_id)
        .order_by(RawRecord.revision.desc())
        .first()
    )
    if not raw_record:
        raise NotFoundError(f"Raw record '{record_id}' for vendor '{vendor}' not found.")

    signals = (
        session.query(NormalizedSignal)
        .filter(NormalizedSignal.raw_record_id == raw_record.id)
        .order_by(NormalizedSignal.event_time.asc())
        .all()
    )

    signal_dicts = [
        {
            "id": s.id,
            "machine_id": s.machine_id,
            "canonical_event_type": s.canonical_event_type,
            "dimension": s.dimension,
            "signal_kind": s.signal_kind,
            "severity": s.severity,
            "event_time": s.event_time.isoformat(),
            "is_effective": s.is_effective,
            "duplicate_of": s.duplicate_of,
            "data_quality_flags": s.data_quality_flags,
            "details": s.details,
        }
        for s in signals
    ]

    return RawRecordDetailResponse(
        id=raw_record.id,
        batch_id=raw_record.batch_id,
        vendor=raw_record.vendor,
        record_id=raw_record.record_id,
        revision=raw_record.revision,
        payload_hash=raw_record.payload_sha256,
        processing_state=raw_record.processing_state,
        received_at=raw_record.received_at,
        rejection_reasons=raw_record.rejection_reasons,
        payload=raw_record.payload,
        signals=signal_dicts,
    )


# ===========================================================================
# 5. Prometheus Metrics
# ===========================================================================
@router.get(
    "/metrics",
    summary="Prometheus text metrics",
    tags=["Metrics"],
)
def get_prometheus_metrics(session: SessionDep) -> Response:
    """Export Prometheus text metrics for queue depths and equipment operational statuses."""
    # 1. Queue depth by status
    status_counts = session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    job_counts = dict(status_counts)

    # 2. Machine states
    mach_counts = (
        session.query(MachineStateView.derived_status, func.count(MachineStateView.machine_id))
        .group_by(MachineStateView.derived_status)
        .all()
    )
    machine_status_counts = dict(mach_counts)

    lines: list[str] = [
        "# HELP aurik_queue_jobs_total Number of queue jobs by status",
        "# TYPE aurik_queue_jobs_total gauge",
    ]
    for st in ["PENDING", "RUNNING", "SUCCEEDED", "FAILED_RETRYABLE", "DEAD"]:
        lines.append(f'aurik_queue_jobs_total{{status="{st}"}} {job_counts.get(st, 0)}')

    lines.extend([
        "# HELP aurik_machine_status_total Number of machines by derived operational status",
        "# TYPE aurik_machine_status_total gauge",
    ])
    for st in ["HEALTHY", "WATCH", "DEGRADED", "CRITICAL", "UNDER_MAINTENANCE"]:
        lines.append(f'aurik_machine_status_total{{status="{st}"}} {machine_status_counts.get(st, 0)}')

    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
