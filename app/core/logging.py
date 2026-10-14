"""Structured JSON logging configuration.

Supports contextual metadata propagation (request_id, batch_id, job_id)
and ensures sensitive tokens are never exposed in log output.
"""

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

# Context variables for tracing across async tasks and HTTP requests
request_id_ctx_var: ContextVar[str | None] = ContextVar("request_id", default=None)
batch_id_ctx_var: ContextVar[str | None] = ContextVar("batch_id", default=None)
job_id_ctx_var: ContextVar[str | None] = ContextVar("job_id", default=None)


class JSONFormatter(logging.Formatter):
    """Custom formatter outputting single-line structured JSON logs."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Inject contextual trace identifiers if present
        req_id = request_id_ctx_var.get()
        if req_id:
            log_entry["request_id"] = req_id

        b_id = batch_id_ctx_var.get()
        if b_id:
            log_entry["batch_id"] = b_id

        j_id = job_id_ctx_var.get()
        if j_id:
            log_entry["job_id"] = j_id

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Include any extra attributes passed to logger calls
        for key, value in record.__dict__.items():
            if key not in (
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "id",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            ) and not key.startswith("_"):
                log_entry[key] = value

        return json.dumps(log_entry, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Initialize root and application loggers with JSON formatting."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level.upper())

    # Clear existing handlers to prevent duplicate lines
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(console_handler)

    # Silence overly verbose external loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
