from __future__ import annotations

from typing import Any, Mapping


def validate_presentation_plan(
    group: Mapping[str, Any], row: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Validate the structure of a model-authored N:M presentation plan.

    This module intentionally has no Substar runtime dependencies.  The exact
    same validator is shipped in external-AI generation packages so imported
    results and built-in provider results obey one executable contract.

    Display limits are deliberately not structural validity constraints.  A
    non-empty, unambiguous translation remains useful to an editor even when
    it exceeds a language-specific character limit.  The caller records those
    soft failures as review warnings instead of discarding the target text.
    """
    if not row:
        return None
    expected = [str(cue["cue_id"]) for cue in group["cues"]]
    expected_set = set(expected)
    raw_units = row.get("meaning_units")
    raw_assignments = row.get("cue_assignments")
    if not isinstance(raw_units, list) or not raw_units or not isinstance(raw_assignments, list):
        return None

    units: list[dict[str, Any]] = []
    seen_unit_ids: set[str] = set()
    for number, item in enumerate(raw_units, start=1):
        if not isinstance(item, Mapping):
            return None
        raw_evidence = item.get("source_evidence_cue_ids", [])
        if not isinstance(raw_evidence, list):
            return None
        evidence_ids = list(dict.fromkeys(
            str(value).strip() for value in raw_evidence if str(value).strip()
        ))
        unit_id = str(item.get("meaning_unit_id") or f"unit_{number:04d}").strip()
        target_text = str(item.get("target_text") or "")
        if (
            not evidence_ids
            or not unit_id
            or unit_id in seen_unit_ids
            or not target_text.strip()
            or not set(evidence_ids) <= expected_set
        ):
            return None
        seen_unit_ids.add(unit_id)
        units.append({
            "meaning_unit_id": unit_id,
            "target_text": target_text,
            "source_evidence_cue_ids": evidence_ids,
        })
    if {cue_id for item in units for cue_id in item["source_evidence_cue_ids"]} != expected_set:
        return None

    unit_by_id = {item["meaning_unit_id"]: item for item in units}
    assignments: list[dict[str, Any]] = []
    for item in raw_assignments:
        if not isinstance(item, Mapping):
            return None
        cue_id = str(item.get("cue_id") or "").strip()
        unit_id = str(item.get("meaning_unit_id") or "").strip()
        if not cue_id or unit_id not in unit_by_id:
            return None
        assignments.append({"cue_id": cue_id, "meaning_unit_id": unit_id})
    if [item["cue_id"] for item in assignments] != expected:
        return None
    if {item["meaning_unit_id"] for item in assignments} != set(unit_by_id):
        return None

    return {
        "group_id": str(group["group_id"]),
        "source_cue_ids": expected,
        "meaning_units": units,
        "cue_assignments": assignments,
    }
