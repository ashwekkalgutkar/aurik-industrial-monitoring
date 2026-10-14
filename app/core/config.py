"""Application configuration and named threshold constants.

All domain thresholds, worker timeouts, and operational settings are defined
here using pydantic-settings with documented defaults.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Environment & Server
    # -------------------------------------------------------------------------
    ENVIRONMENT: Literal["development", "testing", "production"] = Field(
        default="development",
        description="Deployment environment name.",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    MAX_PAYLOAD_BYTES: int = Field(
        default=5_242_880,  # 5 MB
        description="Maximum accepted request body size in bytes (HTTP 413 limit).",
    )

    # -------------------------------------------------------------------------
    # Database
    # -------------------------------------------------------------------------
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://postgres:postgrespassword@localhost:5432/aurik_monitoring",
        description="SQLAlchemy PostgreSQL connection string using psycopg 3.",
    )
    DB_POOL_SIZE: int = Field(default=10, description="Database connection pool size.")
    DB_MAX_OVERFLOW: int = Field(default=20, description="Database pool max overflow.")

    # -------------------------------------------------------------------------
    # Authentication & API Keys (Strict Scopes)
    # -------------------------------------------------------------------------
    API_KEY_INGEST: str = Field(
        default="aurik-ingest-secret-key-prod-001",
        description="Secret key required for vendor ingest endpoints.",
    )
    API_KEY_READ: str = Field(
        default="aurik-read-secret-key-prod-002",
        description="Secret key required for read/query endpoints.",
    )
    API_KEY_ADMIN: str = Field(
        default="aurik-admin-secret-key-prod-003",
        description="Secret key required for administrative and queue maintenance endpoints.",
    )

    # -------------------------------------------------------------------------
    # Domain Engine Named Thresholds & Rules
    # -------------------------------------------------------------------------
    RULESET_VERSION: str = Field(
        default="1.0.0",
        description="Semantic version of the deterministic derived status engine rules.",
    )
    ALERT_TTL_MINUTES: int = Field(
        default=120,
        description="Time-to-live in minutes for telemetry and dimension alert signals.",
    )
    FRESHNESS_MINUTES: int = Field(
        default=30,
        description="Maximum signal age before a machine's processing status is marked STALE.",
    )
    VENDOR_CONFLICT_WINDOW_MINUTES: int = Field(
        default=5,
        description="Time window in minutes within which contradictory signals from different vendors cause VENDOR_CONFLICT.",
    )
    TEMP_NEAR_LIMIT_RATIO: float = Field(
        default=0.90,
        description="Ratio of observed temperature to rated max that triggers escalation to attention 2.",
    )
    VIB_NEAR_LIMIT_RATIO: float = Field(
        default=0.85,
        description="Ratio of observed vibration to rated max that triggers escalation to attention 2.",
    )
    SENSOR_QUALITY_MIN: float = Field(
        default=0.50,
        description="Minimum sensor quality threshold below which LOW_SIGNAL_TRUST is flagged.",
    )
    POWER_DEVIATION_PCT: float = Field(
        default=20.0,
        description="Percentage deviation from baseline power considered anomalous.",
    )
    DEFAULT_ASSUMED_TIMEZONE: str = Field(
        default="UTC",
        description="Assumed fallback timezone for naive timestamp inputs (e.g. MaintaFlow).",
    )
    OPERATOR_NOTE_CONCERN_KEYWORDS: tuple[str, ...] = Field(
        default=(
            "noise",
            "stall",
            "stalled",
            "smoke",
            "leak",
            "vibration",
            "spark",
            "loose",
            "wear",
            "failed",
            "failure",
            "unstable",
        ),
        description="Deterministic keywords in operator notes that flag OPERATOR_NOTE_CONCERN.",
    )

    # -------------------------------------------------------------------------
    # Background Worker & Queue Configuration
    # -------------------------------------------------------------------------
    JOB_LEASE_SECONDS: int = Field(
        default=60,
        description="Lease duration in seconds when claiming jobs with SKIP LOCKED.",
    )
    JOB_MAX_ATTEMPTS: int = Field(
        default=3,
        description="Maximum retry attempts for transient worker errors before dead-lettering.",
    )
    WORKER_POLL_INTERVAL_SECONDS: float = Field(
        default=1.0,
        description="Sleep interval in seconds for the background worker loop when queue is empty.",
    )
    BACKOFF_BASE_SECONDS: float = Field(
        default=2.0,
        description="Base multiplier for exponential backoff calculations.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
