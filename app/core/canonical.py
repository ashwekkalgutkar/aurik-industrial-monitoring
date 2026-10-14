"""Canonical JSON hashing for Layer 1 ingestion idempotency.

Normalizes records so that numeric representations (18 == 18.0),
empty strings for nullables, and key orderings result in deterministic,
identical SHA-256 hashes across CSV-derived and JSON payloads.
"""

import hashlib
import json
from typing import Any


def canonicalize_value(val: Any) -> Any:
    """Recursively normalize data structures for deterministic hashing."""
    if val is None:
        return None
    if isinstance(val, dict):
        return {str(k): canonicalize_value(v) for k, v in sorted(val.items())}
    if isinstance(val, list):
        return [canonicalize_value(item) for item in val]
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        # Normalize 18.0 to 18 for integer values
        float_val = float(val)
        if float_val.is_integer():
            return int(float_val)
        return round(float_val, 6)
    if isinstance(val, str):
        trimmed = val.strip()
        # Coerce empty strings to null for canonical equality with missing/null CSV values
        if trimmed == "":
            return None
        # Coerce string booleans from CSV to canonical booleans
        if trimmed.lower() == "true":
            return True
        if trimmed.lower() == "false":
            return False
        # Coerce float-like integer strings (e.g. "18.0") to integer
        try:
            num = float(trimmed)
            if num.is_integer() and "." in trimmed:
                return int(num)
        except ValueError:
            pass
        return trimmed
    return val


def compute_canonical_record_hash(record_dict: dict[str, Any]) -> str:
    """Compute SHA-256 digest of canonicalized record dictionary."""
    normalized = canonicalize_value(record_dict)
    canonical_json = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
