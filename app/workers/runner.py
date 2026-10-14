"""Background queue worker process with graceful shutdown and expired lease reclamation."""

import logging
import signal
import sys
import time
from types import FrameType

from app.core.clock import get_clock
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.base import SessionLocal
from app.workers.handlers import DEFAULT_JOB_HANDLERS
from app.workers.queue import PostgresJobQueue, execute_job_step

logger = logging.getLogger(__name__)

_RUNNING = True


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    """Signal handler for graceful shutdown on SIGINT and SIGTERM."""
    global _RUNNING
    logger.info("Shutdown signal %d received. Finishing active job and exiting...", signum)
    _RUNNING = False


def run_worker() -> None:
    """Continuously poll the database queue and process asynchronous background jobs."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    clock = get_clock()

    # Register signal handlers for clean SIGTERM / SIGINT shutdown
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    logger.info("Aurik background worker started. Polling jobs via FOR UPDATE SKIP LOCKED...")

    while _RUNNING:
        session = SessionLocal()
        try:
            queue = PostgresJobQueue(session, clock)
            processed = execute_job_step(
                queue=queue,
                session=session,
                handlers=DEFAULT_JOB_HANDLERS,
                lease_seconds=settings.JOB_LEASE_SECONDS,
            )
            if not processed:
                # Sleep when queue is dry
                time.sleep(settings.WORKER_POLL_INTERVAL_SECONDS)
        except Exception as exc:
            logger.exception("Unexpected error in worker loop: %s", exc)
            time.sleep(1.0)
        finally:
            session.close()

    logger.info("Aurik background worker cleanly stopped.")


if __name__ == "__main__":
    run_worker()
    sys.exit(0)
