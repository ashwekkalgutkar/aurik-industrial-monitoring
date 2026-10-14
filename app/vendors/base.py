"""Base data structures and validation primitives for vendor adapters."""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.vendors.enums import (
    CanonicalEventType,
    Dimension,
    SignalKind,
)


@dataclass(slots=True)
class NormalizedSignalData:
    """Canonical representation of an extracted machine signal."""

    source_vendor: str
    source_record_id: str
    machine_id: str
    plant_id: str
    line_id: str
    signal_kind: SignalKind
    canonical_event_type: CanonicalEventType
    dimension: Dimension
    severity: int | None
    event_time: datetime
    ingested_at: datetime
    vibration_mm_s: float | None = None
    vibration_g_raw: float | None = None
    temperature_c: float | None = None
    power_kw: float | None = None
    machine_state: str | None = None
    sensor_quality: float | None = None
    vendor_confidence: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    data_quality_flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NormalizationResult:
    """Outcome of attempting to normalize a raw vendor record."""

    success: bool
    signal: NormalizedSignalData | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    resolved_machine_id: str | None = None


def sanitize_float(
    value: Any,
    min_val: float | None = None,
    max_val: float | None = None,
    sentinels: set[float] | None = None,
) -> tuple[float | None, bool]:
    """Sanitize float inputs. Returns (cleaned_float, was_invalid_flag).

    Sentinels, NaNs, infinities, or out-of-bound numerics return (None, True).
    None or empty strings return (None, False).
    """
    if value is None:
        return None, False
    if isinstance(value, str):
        val_str = value.strip()
        if not val_str:
            return None, False
        try:
            val_float = float(val_str)
        except (ValueError, TypeError):
            return None, True
    elif isinstance(value, (int, float)):
        val_float = float(value)
    else:
        return None, True

    if math.isnan(val_float) or math.isinf(val_float):
        return None, True

    if sentinels and any(math.isclose(val_float, s, rel_tol=1e-5) for s in sentinels):
        return None, True

    if min_val is not None and val_float < min_val:
        return None, True

    if max_val is not None and val_float > max_val:
        return None, True

    return round(val_float, 4), False
