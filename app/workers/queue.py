"""PostgreSQL-backed job queue implementing SELECT ... FOR UPDATE SKIP LOCKED.

Provides transactional job enlistment, atomic lease claiming, crash-safe
lease recovery, and exponential retry backoff behind a clean Queue interface.
"""

import logging
import random
import uuid
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.config import get_settings
from app.core.logging import job_id_ctx_var
from app.db.models import Job

logger = logging.getLogger(__name__)


class JobQueue(ABC):
    """Abstract job queue interface for transactional message processing."""

    @abstractmethod
    def claim_job(self, lease_seconds: int) -> Job | None:
        """Atomically claim the next eligible job setting a lease lock."""
        raise NotImplementedError

    @abstractmethod
    def complete_job(self, job_id: str) -> None:
        """Mark a job as successfully finished."""
        raise NotImplementedError

    @abstractmethod
    def fail_job(self, job_id: str, error_message: str, is_retryable: bool) -> None:
        """Mark a job as failed, scheduling a backoff retry or routing to dead-letter."""
        raise NotImplementedError

    @abstractmethod
    def requeue_job(self, job_id: str) -> bool:
        """Reset a dead or failed job back to PENDING for immediate execution."""
        raise NotImplementedError

    @abstractmethod
    def enqueue_recompute(self, machine_id: str) -> None:
        """Enqueue a machine status recompute job, deduplicating against PENDING jobs."""
        raise NotImplementedError


class PostgresJobQueue(JobQueue):
    """PostgreSQL implementation of JobQueue using FOR UPDATE SKIP LOCKED."""

    def __init__(self, session: Session, clock: Clock | None = None) -> None:
        self.session = session
        self.clock = clock or get_clock()
        self.settings = get_settings()

    def claim_job(self, lease_seconds: int) -> Job | None:
        """Claim next runnable job. Recovers expired leases automatically."""
        now_utc = self.clock.now()

        # Runnable if:
        # 1. status in ('PENDING', 'FAILED_RETRYABLE') AND run_after <= now
        # OR 2. status == 'RUNNING' AND locked_until < now (expired lease crash recovery)
        stmt = (
            select(Job)
            .where(
                or_(
                    Job.status.in_(["PENDING", "FAILED_RETRYABLE"]),
                    (Job.status == "RUNNING") & (Job.locked_until < now_utc),
                ),
                Job.run_after <= now_utc,
            )
            .order_by(Job.run_after.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        job = self.session.execute(stmt).scalars().first()
        if not job:
            return None

        job.status = "RUNNING"
        job.locked_until = now_utc + timedelta(seconds=lease_seconds)
        job.attempts += 1
        job.updated_at = now_utc
        self.session.commit()
        return job

    def complete_job(self, job_id: str) -> None:
        """Mark job SUCCEEDED and clear lock."""
        now_utc = self.clock.now()
        job = self.session.get(Job, job_id)
        if job:
            job.status = "SUCCEEDED"
            job.locked_until = None
            job.updated_at = now_utc
            self.session.commit()

    def fail_job(self, job_id: str, error_message: str, is_retryable: bool) -> None:
        """Route job to retryable backoff or dead-letter queue."""
        now_utc = self.clock.now()
        job = self.session.get(Job, job_id)
        if not job:
            return

        job.last_error = error_message
        job.locked_until = None
        job.updated_at = now_utc

        if is_retryable and job.attempts < job.max_attempts:
            # Exponential backoff with small random jitter: base^attempts + jitter
            jitter = random.uniform(0.1, 0.5)
            backoff_secs = (self.settings.BACKOFF_BASE_SECONDS**job.attempts) + jitter
            job.status = "FAILED_RETRYABLE"
            job.run_after = now_utc + timedelta(seconds=backoff_secs)
            logger.warning(
                "Job %s failed with retryable error (attempt %d/%d). Retrying in %.2fs: %s",
                job_id,
                job.attempts,
                job.max_attempts,
                backoff_secs,
                error_message,
            )
        else:
            job.status = "DEAD"
            logger.error(
                "Job %s marked DEAD after %d attempts: %s", job_id, job.attempts, error_message
            )

        self.session.commit()

    def requeue_job(self, job_id: str) -> bool:
        """Re-enqueue a dead or failed job for immediate processing."""
        now_utc = self.clock.now()
        job = self.session.get(Job, job_id)
        if not job or job.status not in ("DEAD", "FAILED_RETRYABLE"):
            return False

        job.status = "PENDING"
        job.attempts = 0
        job.run_after = now_utc
        job.locked_until = None
        job.last_error = None
        job.updated_at = now_utc
        self.session.commit()
        return True

    def enqueue_recompute(self, machine_id: str) -> None:
        """Enqueue machine status recompute job, deduplicating against PENDING jobs."""
        now_utc = self.clock.now()
        dedupe_key = f"RECOMPUTE:{machine_id}"

        # Check if an identical recompute job is already PENDING
        existing_pending = (
            self.session.query(Job).filter_by(dedupe_key=dedupe_key, status="PENDING").first()
        )
        if existing_pending:
            return  # Deduped against pending job

        new_job = Job(
            id=str(uuid.uuid4()),
            type="RECOMPUTE_MACHINE",
            payload={"machine_id": machine_id},
            dedupe_key=dedupe_key,
            status="PENDING",
            attempts=0,
            max_attempts=self.settings.JOB_MAX_ATTEMPTS,
            run_after=now_utc,
            created_at=now_utc,
            updated_at=now_utc,
        )
        try:
            self.session.add(new_job)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()  # Caught by partial unique index uq_jobs_dedupe_pending


def execute_job_step(
    queue: JobQueue,
    session: Session,
    handlers: dict[str, Any],
    lease_seconds: int = 60,
) -> bool:
    """Execute a single job step. Returns True if a job was claimed and processed, False if queue was empty."""
    job = queue.claim_job(lease_seconds=lease_seconds)
    if not job:
        return False

    token = job_id_ctx_var.set(job.id)
    try:
        handler = handlers.get(job.type)
        if not handler:
            queue.fail_job(
                job.id,
                f"No handler registered for job type '{job.type}'",
                is_retryable=False,
            )
            return True

        handler(job, session)
        queue.complete_job(job.id)
        return True
    except Exception as exc:
        is_retryable = not isinstance(exc, (ValueError, KeyError))
        queue.fail_job(job.id, str(exc), is_retryable=is_retryable)
        return True
    finally:
        job_id_ctx_var.reset(token)


def drain_queue(
    session: Session,
    handlers: dict[str, Any],
    clock: Clock | None = None,
    max_iterations: int = 2000,
) -> int:
    """Synchronously process all pending jobs until queue is empty.

    Used for deterministic testing, CLI seed runs, and batch processing.
    """
    queue = PostgresJobQueue(session, clock)
    count = 0
    while count < max_iterations:
        processed = execute_job_step(queue, session, handlers)
        if not processed:
            break
        count += 1
    return count
