from enum import StrEnum


class SignalKind(StrEnum):
    """Broad classification of the normalized signal."""

    ALERT = "ALERT"
    TELEMETRY = "TELEMETRY"
    INSPECTION = "INSPECTION"
    MAINTENANCE = "MAINTENANCE"
    CALIBRATION = "CALIBRATION"
    OPERATOR_NOTE = "OPERATOR_NOTE"
    WORK_ORDER = "WORK_ORDER"


class CanonicalEventType(StrEnum):
    """Canonical event taxonomy normalized across all vendor dialects."""

    VIBRATION_HIGH = "VIBRATION_HIGH"
    TEMPERATURE_HIGH = "TEMPERATURE_HIGH"
    TEMPERATURE_CRITICAL = "TEMPERATURE_CRITICAL"
    POWER_ANOMALY = "POWER_ANOMALY"
    SENSOR_DEGRADED = "SENSOR_DEGRADED"
    NOMINAL = "NOMINAL"
    UNKNOWN_EVENT = "UNKNOWN_EVENT"
    INSPECTION = "INSPECTION"
    MAINTENANCE = "MAINTENANCE"
    OPERATOR_NOTE = "OPERATOR_NOTE"
    CALIBRATION = "CALIBRATION"
    WORK_ORDER = "WORK_ORDER"


class Dimension(StrEnum):
    """Physical or operational telemetry dimension monitored by the signal."""

    VIBRATION = "VIBRATION"
    TEMPERATURE = "TEMPERATURE"
    POWER = "POWER"
    SENSOR = "SENSOR"
    MAINTENANCE = "MAINTENANCE"
    NONE = "NONE"


# ---------------------------------------------------------------------------
# Named Unit Conversion & Confidence Constants
# ---------------------------------------------------------------------------
FAHRENHEIT_FREEZING_POINT: float = 32.0
FAHRENHEIT_TO_CELSIUS_MULTIPLIER: float = 5.0 / 9.0


def fahrenheit_to_celsius(fahrenheit: float) -> float:
    """Deterministically convert Fahrenheit to Celsius rounded to 2 decimal places."""
    return round((fahrenheit - FAHRENHEIT_FREEZING_POINT) * FAHRENHEIT_TO_CELSIUS_MULTIPLIER, 2)


# MaintaFlow qualitative confidence scale mapped to floating point confidence
MAINTAFLOW_CONFIDENCE_LOW: float = 0.4
MAINTAFLOW_CONFIDENCE_MEDIUM: float = 0.7
MAINTAFLOW_CONFIDENCE_HIGH: float = 0.9

MAINTAFLOW_CONFIDENCE_MAP: dict[str, float] = {
    "low": MAINTAFLOW_CONFIDENCE_LOW,
    "medium": MAINTAFLOW_CONFIDENCE_MEDIUM,
    "high": MAINTAFLOW_CONFIDENCE_HIGH,
}

# ThermexWatch signal quality ratings mapped to 0.0 - 1.0 sensor quality
THERMEXWATCH_QUALITY_GOOD: float = 1.0
THERMEXWATCH_QUALITY_FAIR: float = 0.6
THERMEXWATCH_QUALITY_POOR: float = 0.3

THERMEXWATCH_QUALITY_MAP: dict[str, float] = {
    "GOOD": THERMEXWATCH_QUALITY_GOOD,
    "FAIR": THERMEXWATCH_QUALITY_FAIR,
    "POOR": THERMEXWATCH_QUALITY_POOR,
}

# PulseForge severity scale
PULSEFORGE_SEVERITY_MAP: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

# ---------------------------------------------------------------------------
# Data Quality & Entity Resolution Flags
# ---------------------------------------------------------------------------
FLAG_PLANT_MISMATCH_RESOLVED = "PLANT_MISMATCH_RESOLVED"
FLAG_LINE_MISMATCH_RESOLVED = "LINE_MISMATCH_RESOLVED"
FLAG_MISSING_LINE_RESOLVED = "MISSING_LINE_RESOLVED"
FLAG_ASSUMED_TIMEZONE = "ASSUMED_TIMEZONE"
FLAG_UNMAPPED_ALERT_CODE = "UNMAPPED_ALERT_CODE"
FLAG_INVALID_SEVERITY = "INVALID_SEVERITY"
FLAG_INVALID_VALUE = "INVALID_VALUE"
