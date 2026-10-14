"""Unit tests for canonical JSON hashing and Layer 1 idempotency."""

from app.core.canonical import canonicalize_value, compute_canonical_record_hash


def test_canonical_number_normalization_int_and_float():
    """Verify that integer 18 and float 18.0 produce identical canonical representations."""
    d1 = {"count": 18, "vendor": "MaintaFlow"}
    d2 = {"count": 18.0, "vendor": "MaintaFlow"}
    assert canonicalize_value(d1) == canonicalize_value(d2)
    assert compute_canonical_record_hash(d1) == compute_canonical_record_hash(d2)


def test_canonical_empty_string_coercion_to_null():
    """Verify that empty string in CSV equates to null/None in JSON."""
    csv_row = {"status": "", "note": "All good"}
    json_record = {"status": None, "note": "All good"}
    assert compute_canonical_record_hash(csv_row) == compute_canonical_record_hash(json_record)


def test_canonical_key_ordering_independence():
    """Verify that dictionary key insertion order does not alter SHA-256 hash."""
    d1 = {"a": 1, "b": 2, "c": 3}
    d2 = {"c": 3, "a": 1, "b": 2}
    assert compute_canonical_record_hash(d1) == compute_canonical_record_hash(d2)


def test_canonical_boolean_normalization():
    """Verify string boolean coercion matches boolean literals."""
    d1 = {"is_active": "True", "val": 10}
    d2 = {"is_active": True, "val": 10.0}
    assert compute_canonical_record_hash(d1) == compute_canonical_record_hash(d2)
