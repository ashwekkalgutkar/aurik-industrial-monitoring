"""Authoritative entity resolution for machines, plants, and production lines."""

from dataclasses import dataclass

from app.vendors.enums import (
    FLAG_LINE_MISMATCH_RESOLVED,
    FLAG_MISSING_LINE_RESOLVED,
    FLAG_PLANT_MISMATCH_RESOLVED,
)


@dataclass(slots=True)
class MachineCatalogEntry:
    """Authoritative reference metadata for a machine."""

    machine_id: str
    plant_id: str
    line_id: str


@dataclass(slots=True)
class ResolutionResult:
    """Outcome of resolving entity identifiers against the authoritative reference."""

    resolved_machine_id: str | None
    resolved_plant_id: str | None
    resolved_line_id: str | None
    flags: list[str]
    rejection_reasons: list[str]

    @property
    def is_valid(self) -> bool:
        return len(self.rejection_reasons) == 0 and self.resolved_machine_id is not None


def resolve_entities(
    raw_machine_ref: str | None,
    envelope_plant_id: str | None,
    record_line_id: str | None,
    catalog: dict[str, MachineCatalogEntry],
) -> ResolutionResult:
    """Resolve machine, plant, and line against authoritative reference data.

    Rules:
    - Missing or empty machine ID -> REJECTED_INVALID.
    - Unknown machine ID -> REJECTED_INVALID (resolved_machine_id preserved for tracking).
    - Plant mismatch between envelope and machine reference -> use reference plant, flag PLANT_MISMATCH_RESOLVED.
    - Line mismatch between record and machine reference -> use reference line, flag LINE_MISMATCH_RESOLVED.
    - Missing/blank line -> use reference line, flag MISSING_LINE_RESOLVED.
    """
    flags: list[str] = []
    rejections: list[str] = []

    if raw_machine_ref is None:
        return ResolutionResult(
            resolved_machine_id=None,
            resolved_plant_id=None,
            resolved_line_id=None,
            flags=flags,
            rejection_reasons=["Missing machine identifier."],
        )

    machine_id_str = str(raw_machine_ref).strip()
    if not machine_id_str:
        return ResolutionResult(
            resolved_machine_id=None,
            resolved_plant_id=None,
            resolved_line_id=None,
            flags=flags,
            rejection_reasons=["Empty machine identifier."],
        )

    # Machine exists in reference?
    if machine_id_str not in catalog:
        return ResolutionResult(
            resolved_machine_id=machine_id_str,
            resolved_plant_id=None,
            resolved_line_id=None,
            flags=flags,
            rejection_reasons=[f"Unknown machine ID '{machine_id_str}' not in reference catalog."],
        )

    ref = catalog[machine_id_str]

    # Plant verification
    if envelope_plant_id:
        env_plant_clean = str(envelope_plant_id).strip()
        if env_plant_clean and env_plant_clean != ref.plant_id:
            flags.append(FLAG_PLANT_MISMATCH_RESOLVED)

    # Line verification
    if record_line_id is None:
        flags.append(FLAG_MISSING_LINE_RESOLVED)
    else:
        line_clean = str(record_line_id).strip()
        if not line_clean:
            flags.append(FLAG_MISSING_LINE_RESOLVED)
        else:
            # Check direct match (e.g. "LINE-A") or short-code expansion (e.g. "A" -> "LINE-A")
            is_match = line_clean == ref.line_id or f"LINE-{line_clean}" == ref.line_id
            if not is_match:
                flags.append(FLAG_LINE_MISMATCH_RESOLVED)

    return ResolutionResult(
        resolved_machine_id=ref.machine_id,
        resolved_plant_id=ref.plant_id,
        resolved_line_id=ref.line_id,
        flags=flags,
        rejection_reasons=rejections,
    )
