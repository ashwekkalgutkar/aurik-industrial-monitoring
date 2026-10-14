"""Application configuration and named threshold constants.

All domain thresholds, worker timeouts, and operational settings are defined
here using pydantic-settings with documented defaults.
"""

import logging
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Insecure example keys that must never be used in production mode
KNOWN_INSECURE_KEYS = {
    "aurik-ingest-secret-key-prod-001",
    "aurik-read-secret-key-prod-002",
    "aurik-admin-secret-key-prod-003",
    "example",
    "secret",
    "changeme",
    "default",
    "admin",
    "test",
    "password",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Environment & Server
    # -------------------------------------------------------------------------
    APP_ENV: Literal["development", "testing", "production"] = Field(
        default="development",
        description="Application environment mode (development, testing, production).",
    )
    ENVIRONMENT: Literal["development", "testing", "production"] = Field(
        default="development",
        description="Alias for APP_ENV for backward compatibility.",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    MAX_PAYLOAD_BYTES: int = Field(
        default=5_242_880,  # 5 MB
        description="Maximum accepted request body size in bytes (HTTP 413 limit).",
    )
    ENABLE_DOCS: bool | None = Field(
        default=None,
        description="Explicitly toggle OpenAPI /docs, /redoc, /openapi.json. Defaults to False in production.",
    )
    CORS_ALLOW_ORIGINS: str = Field(
        default="",
        description="Comma-separated allowed origins for CORS. Empty disables CORS by default.",
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
    DB_CONNECT_TIMEOUT_SECONDS: int = Field(
        default=10, description="TCP connection timeout in seconds."
    )
    DB_STATEMENT_TIMEOUT_MS: int = Field(
        default=30000, description="PostgreSQL server-side statement timeout in milliseconds."
    )

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

    @model_validator(mode="after")
    def validate_production_security(self) -> Self:
        """Enforce strict security validation when running in production mode."""
        # Harmonize APP_ENV and ENVIRONMENT
        effective_env = self.APP_ENV
        if self.ENVIRONMENT != "development" and self.APP_ENV == "development":
            effective_env = self.ENVIRONMENT
        self.APP_ENV = effective_env
        self.ENVIRONMENT = effective_env

        if self.APP_ENV == "production":
            key_names = [
                ("API_KEY_INGEST", self.API_KEY_INGEST),
                ("API_KEY_READ", self.API_KEY_READ),
                ("API_KEY_ADMIN", self.API_KEY_ADMIN),
            ]
            for name, val in key_names:
                if not val or not val.strip():
                    logger.error("Production startup refused: %s is not set.", name)
                    raise ValueError(
                        f"Production configuration error: {name} must be set to a non-empty secret."
                    )
                clean_val = val.strip()
                if len(clean_val) < 24:
                    logger.error(
                        "Production startup refused: %s is too short (%d chars, min 24 required).",
                        name,
                        len(clean_val),
                    )
                    raise ValueError(
                        f"Production configuration error: {name} must be at least 24 characters long."
                    )
                if clean_val.lower() in KNOWN_INSECURE_KEYS:
                    logger.error(
                        "Production startup refused: %s uses an insecure default/example value.",
                        name,
                    )
                    raise ValueError(
                        f"Production configuration error: {name} cannot equal known example/default keys."
                    )
        return self

    @property
    def docs_enabled(self) -> bool:
        """Determine if Swagger/OpenAPI documentation endpoints should be mounted."""
        if self.ENABLE_DOCS is not None:
            return self.ENABLE_DOCS
        return self.APP_ENV != "production"

    @property
    def cors_origins(self) -> list[str]:
        """Return parsed list of CORS origins."""
        if not self.CORS_ALLOW_ORIGINS:
            return []
        return [origin.strip() for origin in self.CORS_ALLOW_ORIGINS.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
