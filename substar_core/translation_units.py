"""Independent target display units stored in existing cue mapping records."""
from dataclasses import replace
import math
from substar_core.domain import ChangeKind, ChangeProvenance, EntityState, TranslationTrack


def all_units(document):
    units = {}
    for cue in document.cues:
        for unit in cue.mapping.get("target_units", []):
            uid = unit["unit_id"]
            if uid in units:
                # Splitting a cue temporarily copies its mapping. Rebinding at
                # the operation boundary chooses one canonical owner again.
                if units[uid] == unit:
                    continue
                raise ValueError("conflicting translation unit: " + uid)
            units[uid] = dict(unit)
    return list(units.values())


def validate_units(document, units):
    cues = {cue.cue_id: cue for cue in document.cues}
    ids = set()
    spans = {}
    for unit in units:
        uid = unit.get("unit_id")
        text = unit.get("text")
        evidence = unit.get("source_cue_ids")
        if not isinstance(uid, str) or not uid.strip() or uid in ids:
            raise ValueError("translation unit IDs must be nonempty and unique")
        if uid in cues:
            raise ValueError("translation unit IDs must be distinct from source cue IDs")
        ids.add(uid)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("translation unit text cannot be empty")
        if not isinstance(evidence, list) or not evidence or len(set(evidence)) != len(evidence):
            raise ValueError("translation unit needs unique source cue IDs")
        if any(cid not in cues for cid in evidence):
            raise ValueError("translation unit references an unknown cue")
        start, end = unit.get("start"), unit.get("end")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (start, end)):
            raise ValueError("translation unit times must be finite source-clock seconds")
        if start < 0 or end <= start:
            raise ValueError("translation unit time range is invalid")
        selected = [cues[cid] for cid in evidence]
        speakers = {cue.speaker for cue in selected}
        if len(speakers) != 1:
            raise ValueError("a translation unit cannot join different speakers")
        if start < min(c.start for c in selected) - 1e-6 or end > max(c.end for c in selected) + 1e-6:
            raise ValueError("translation unit must stay within its source evidence")
        speaker = selected[0].speaker
        spans.setdefault((speaker, unit.get("language", "zh-CN")), []).append((start, end))
    for group in spans.values():
        group.sort()
        if any(right[0] < left[1] - 1e-6 for left, right in zip(group, group[1:])):
            raise ValueError("translation units for the same speaker overlap")


def install_units(document, units, provenance):
    validate_units(document, units)
    by_cue = {}
    owners = {}
    active_ids = {cue.cue_id for cue in document.cues if cue.state is EntityState.ACTIVE}
    for unit in units:
        active = [cid for cid in unit["source_cue_ids"] if cid in active_ids]
        owner = active[0] if active else unit["source_cue_ids"][0]
        owners.setdefault(owner, []).append(unit)
        for cid in unit["source_cue_ids"]:
            by_cue.setdefault(cid, []).append(unit)
    cues = []
    for cue in document.cues:
        mapping = dict(cue.mapping)
        was_linked = bool(mapping.pop("target_unit_ids", None))
        mapping.pop("target_units", None)
        linked = sorted(by_cue.get(cue.cue_id, []), key=lambda unit: (unit["start"], unit["unit_id"]))
        target = cue.target
        if linked:
            mapping["target_unit_ids"] = [unit["unit_id"] for unit in linked]
            if cue.cue_id in owners:
                mapping["target_units"] = owners[cue.cue_id]
            original = target.original_text if target else None
            text = "\n".join(unit["text"] for unit in linked)
            status = "needs_review" if any(unit.get("needs_review") for unit in linked) else "translated"
            previous = target
            target = TranslationTrack(
                target_text=text,
                original_text=original,
                language=linked[0].get("language", "zh-CN"), provenance=provenance,
                translation_status=status,
                issue_code="source_changed" if any(unit.get("needs_review") for unit in linked) else None,
            )
            if previous and (previous.target_text, previous.language, previous.translation_status, previous.issue_code) == (
                    target.target_text, target.language, target.translation_status, target.issue_code):
                target = previous
        elif was_linked:
            target = None
        cues.append(replace(cue, mapping=mapping, target=target))
    return replace(document, cues=tuple(cues))


def set_units(document, payload, provenance):
    incoming = payload.get("units")
    if not isinstance(incoming, list) or len(incoming) > 2000:
        raise ValueError("units must be a list of at most 2000 translation units")
    scope = payload.get("replace_cue_ids")
    if not isinstance(scope, list) or not scope:
        raise ValueError("replace_cue_ids is required")
    known = {cue.cue_id for cue in document.cues if cue.state is EntityState.ACTIVE}
    if len(set(scope)) != len(scope) or not set(scope) <= known:
        raise ValueError("replacement scope must contain active cue IDs")
    selected = set(scope)
    for unit in incoming:
        if not isinstance(unit, dict) or not set(unit.get("source_cue_ids", [])) <= selected:
            raise ValueError("translation unit evidence must be inside replacement scope")
    old = all_units(document)
    affected = [unit for unit in old if selected.intersection(unit["source_cue_ids"])]
    if any(not set(unit["source_cue_ids"]) <= selected for unit in affected):
        raise ValueError("replacement scope must include the entire existing translation unit")
    units = [unit for unit in old if unit not in affected]
    units.extend({**unit, "provenance": provenance.to_dict()} for unit in incoming)
    result = install_units(document, units, provenance)
    # Replacing an empty scope explicitly clears its legacy cue-level targets too.
    covered = {cid for unit in incoming for cid in unit["source_cue_ids"]}
    return replace(result, cues=tuple(
        replace(cue, target=None) if cue.cue_id in selected - covered else cue
        for cue in result.cues
    ))


def edit_unit(document, payload, provenance):
    units = all_units(document)
    unit = next((unit for unit in units if unit["unit_id"] == payload.get("unit_id")), None)
    if unit is None:
        raise ValueError("translation unit no longer exists")
    if "expected_text" in payload and unit["text"] != payload["expected_text"]:
        raise ValueError("translation unit was edited concurrently")
    text = str(payload.get("text", unit["text"])).strip()
    if not text:
        return install_units(document, [item for item in units if item is not unit], provenance)
    unit.update(text=text, provenance=provenance.to_dict())
    if "text" in payload:
        unit["needs_review"] = False
    for field in ("start", "end"):
        if field in payload:
            unit[field] = payload[field]
    return install_units(document, units, provenance)


def reconcile_units(before, after):
    """Retain unit identity when source cue topology changes."""
    old = all_units(before)
    if not old:
        return after
    active = [cue for cue in after.cues if cue.state is EntityState.ACTIVE]
    active_ids = {cue.cue_id for cue in active}
    known = {cue.cue_id for cue in after.cues}
    before_tokens = {t.token_id: (t.text, t.state) for t in before.display_tokens}
    after_tokens = {t.token_id: (t.text, t.state) for t in after.display_tokens}
    previous = {cue.cue_id: cue for cue in before.cues}
    changed = before_tokens != after_tokens
    units = []
    for unit in old:
        evidence = [cid for cid in unit["source_cue_ids"] if cid in active_ids]
        live = [cue for cue in active if cue.cue_id in evidence]
        topology_changed = any(cid not in known for cid in unit["source_cue_ids"])
        if not live or topology_changed or any(
                cue.start < unit["start"] - 1e-6 or cue.end < unit["end"] - 1e-6
                for cue in live):
            speakers = {previous[cid].speaker for cid in unit["source_cue_ids"] if cid in previous}
            live = [cue for cue in active if cue.speaker in speakers
                    and cue.end > unit["start"] and cue.start < unit["end"]]
            evidence = [cue.cue_id for cue in live]
        if not evidence:
            continue
        # Keep the original source evidence extent if boundary edits changed the span.
        new_unit = dict(unit, source_cue_ids=evidence)
        if live:
            new_unit["start"] = max(unit["start"], min(c.start for c in live))
            new_unit["end"] = min(unit["end"], max(c.end for c in live))
        if new_unit["end"] <= new_unit["start"]:
            if not live:
                continue
            old_cues = [previous[cid] for cid in unit["source_cue_ids"] if cid in previous]
            old_start, old_end = min(c.start for c in old_cues), max(c.end for c in old_cues)
            new_start, new_end = min(c.start for c in live), max(c.end for c in live)
            scale = (new_end - new_start) / (old_end - old_start)
            new_unit["start"] = new_start + (unit["start"] - old_start) * scale
            new_unit["end"] = new_start + (unit["end"] - old_start) * scale
        source_cues = [previous[cid] for cid in unit["source_cue_ids"] if cid in previous]
        token_ids = {tid for cue in source_cues for tid in cue.display_token_ids}
        text_changed = any(cue.source_text != previous[cue.cue_id].source_text
                           for cue in live if cue.cue_id in previous)
        source_hidden = any(cid not in active_ids and cid in known for cid in unit["source_cue_ids"])
        if source_hidden or text_changed or changed and any(before_tokens.get(tid) != after_tokens.get(tid) for tid in token_ids):
            new_unit["needs_review"] = True
        units.append(new_unit)
    definition_count = sum(len(cue.mapping.get("target_units", [])) for cue in after.cues)
    if units == old and all_units(after) == old and definition_count == len(old):
        return after
    provenance = after.changes[-1] if after.changes else ChangeProvenance(ChangeKind.NORMALIZATION, "translation_units_rebind")
    return install_units(after, units, provenance)


def target_events(document):
    """Return each target once with its independent source-clock display span."""
    units = all_units(document)
    cues = {cue.cue_id: cue for cue in document.cues}
    events = []
    for unit in units:
        evidence = [cues[cid] for cid in unit["source_cue_ids"] if cid in cues and cues[cid].state is EntityState.ACTIVE]
        if evidence:
            events.append(dict(unit, cue_id=evidence[0].cue_id, speaker=evidence[0].speaker))
    for cue in document.cues:
        if cue.state is EntityState.ACTIVE and cue.target and not cue.mapping.get("target_unit_ids"):
            events.append(dict(unit_id=cue.cue_id, cue_id=cue.cue_id, start=cue.start, end=cue.end,
                               text=cue.target.target_text, language=cue.target.language,
                               source_cue_ids=[cue.cue_id], speaker=cue.speaker))
    return sorted(events, key=lambda unit: (unit["start"], unit["end"], unit["unit_id"]))
