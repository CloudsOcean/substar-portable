"""Restore selected fields without replacing unrelated work or changing the schema."""
from dataclasses import replace

from substar_core.domain import ChangeKind, ChangeProvenance, EntityState
from substar_core.document_operations import synchronize_translation_status


def restore_fields(current, historical, cue_ids, scope):
    if scope not in {"text", "timing", "translation", "style"}:
        raise ValueError("不支持的恢复范围")
    ids = set(cue_ids)
    if not ids:
        raise ValueError("请先选择字幕")
    now = {c.cue_id: c for c in current.cues}
    old = {c.cue_id: c for c in historical.cues}
    if scope == "style":
        from substar_core.ass_profiles import configuration, effective_profile, apply_profile
        historical_styles = configuration(historical)
        result = current
        for cue_id in ids:
            if cue_id not in now or cue_id not in old or any(c.state is not EntityState.ACTIVE for c in (now[cue_id], old[cue_id])):
                raise ValueError("只能恢复两个版本中均存在的有效字幕")
            result = apply_profile(result, effective_profile(historical_styles, cue_id), [cue_id])
        return result
    tokens = {t.token_id: t for t in current.display_tokens}
    old_tokens = {t.token_id: t for t in historical.display_tokens}
    changed = set()
    provenance = ChangeProvenance(kind=ChangeKind.MANUAL, operation="selective_restore", actor="editor",
                                  metadata={"scope": scope, "cue_ids": sorted(ids)})
    for cue_id in ids:
        if cue_id not in now or cue_id not in old:
            raise ValueError("字幕结构已经变化，无法选择性恢复；可打开完整历史版本")
        cue, previous = now[cue_id], old[cue_id]
        if cue.state is not EntityState.ACTIVE or previous.state is not EntityState.ACTIVE:
            raise ValueError("只能选择性恢复两个版本中均未隐藏的字幕")
        if scope == "text":
            if cue.display_token_ids != previous.display_token_ids or (cue.source_text is None) != (previous.source_text is None):
                raise ValueError("词元已拆分或合并，无法安全恢复文字；可打开完整历史版本")
            if cue.source_text is not None:
                target = cue.target
                if target is not None and cue.source_text != previous.source_text and target.translation_status == "translated":
                    target = replace(target, translation_status="needs_review", issue_code="source_changed")
                now[cue_id] = replace(cue, source_text=previous.source_text, target=target)
                changed.update(cue.display_token_ids)
            for token_id in cue.display_token_ids:
                token, prior = tokens[token_id], old_tokens[token_id]
                if token.state != prior.state or token.source_token_ids != prior.source_token_ids:
                    raise ValueError("词元来源或隐藏状态已经变化，无法安全恢复文字")
                if token.text != prior.text:
                    tokens[token_id] = replace(token, text=prior.text, provenance=provenance)
                    changed.add(token_id)
        elif scope == "timing":
            now[cue_id] = replace(cue, start=previous.start, end=previous.end)
        else:
            restored = previous.target
            if restored is not None and (cue.display_token_ids != previous.display_token_ids or
                cue.source_text != previous.source_text or any(tokens[t].text != old_tokens[t].text
                    for t in cue.display_token_ids if t in old_tokens)):
                restored = replace(restored, translation_status="needs_review", issue_code="source_changed")
            now[cue_id] = replace(cue, target=restored)
    result = replace(current, cues=tuple(now[c.cue_id] for c in current.cues),
                     display_tokens=tuple(tokens[t.token_id] for t in current.display_tokens),
                     changes=(*current.changes, provenance))
    if scope in {"translation", "timing"}:
        from substar_core.translation_units import all_units, install_units
        current_units, prior_units = all_units(current), all_units(historical)
        affected = [u for u in current_units + prior_units if ids.intersection(u["source_cue_ids"])]
        if any(not set(u["source_cue_ids"]) <= ids for u in affected):
            raise ValueError("请同时选择共享译文关联的全部字幕")
        if affected:
            keep = [u for u in current_units if not ids.intersection(u["source_cue_ids"])]
            replacements = [u for u in prior_units if ids.intersection(u["source_cue_ids"])]
            if scope == "translation":
                current_by_id = {u["unit_id"]: u for u in current_units}
                restored_units = []
                for prior in replacements:
                    existing = current_by_id.get(prior["unit_id"])
                    evidence = prior["source_cue_ids"]
                    if existing and existing["source_cue_ids"] == evidence:
                        start, end = existing["start"], existing["end"]
                    else:
                        old_start, old_end = min(old[c].start for c in evidence), max(old[c].end for c in evidence)
                        start_now, end_now = min(now[c].start for c in evidence), max(now[c].end for c in evidence)
                        scale = (end_now - start_now) / (old_end - old_start)
                        start = start_now + (prior["start"] - old_start) * scale
                        end = start_now + (prior["end"] - old_start) * scale
                    unit = dict(prior, start=start, end=end)
                    if any(now[c].target and now[c].target.issue_code == "source_changed" for c in evidence):
                        unit["needs_review"] = True
                    restored_units.append(unit)
                replacements = restored_units
            if scope == "timing":
                old_by_id = {u["unit_id"]: u for u in replacements}
                replacements = []
                for unit in current_units:
                    if not ids.intersection(unit["source_cue_ids"]):
                        continue
                    prior = old_by_id.get(unit["unit_id"])
                    if prior is None:
                        raise ValueError("译文结构已变化，无法只恢复时间")
                    replacements.append(dict(unit, start=prior["start"], end=prior["end"]))
            result = install_units(result, keep + replacements, provenance)
            if scope == "translation":
                no_units = ids - {cid for u in replacements for cid in u["source_cue_ids"]}
                result = replace(result, cues=tuple(replace(c, target=now[c.cue_id].target)
                    if c.cue_id in no_units else c for c in result.cues))
    return synchronize_translation_status(result, changed)


def split_token(document, token_id, offset):
    """Split display timing while carrying the parent evidence in the snapshot."""
    from substar_core.domain import SourceToken, DisplayToken, stable_id
    token = next((t for t in document.display_tokens if t.token_id == token_id), None)
    cue = next((c for c in document.cues if token_id in c.display_token_ids), None)
    if token is None or cue is None or token.state is not EntityState.ACTIVE or cue.state is not EntityState.ACTIVE:
        raise ValueError("请选择未隐藏的词元")
    if any(c.state is not EntityState.ACTIVE for c in document.cues if token_id in c.display_token_ids):
        raise ValueError("共享词元包含隐藏字幕，请先恢复字幕")
    if not 0 < offset < len(token.text) or not token.text[:offset].strip() or not token.text[offset:].strip():
        raise ValueError("请在词元内部两个字符之间切分")
    source = [s for s in document.source_tokens if s.token_id in token.source_token_ids]
    if not source:
        raise ValueError("该词元没有独立时间，不能均分词级时间")
    start, end = min(s.start for s in source), max(s.end for s in source)
    if end <= start:
        raise ValueError("该词元没有可均分的有效时长")
    midpoint = (start + end) / 2
    provenance = ChangeProvenance(kind=ChangeKind.MANUAL, operation="split_token", actor="editor",
        metadata={"original_token_id": token_id, "offset": offset, "timing": "subdivided"})
    sources, displays = [], []
    for index, (text, left, right) in enumerate(((token.text[:offset].strip(), start, midpoint),
                                               (token.text[offset:].strip(), midpoint, end))):
        item = SourceToken.create(index=source[0].index + index, text=text, start=left, end=right,
                                  speaker=source[0].speaker)
        item = replace(item, timing={"kind": "subdivided", "method": "equal_split",
                                    "parent_source_ids": list(token.source_token_ids),
                                    "parent_sources": [s.to_dict() for s in source]})
        sources.append(item)
        displays.append(DisplayToken(stable_id("dsp", {"parent":token_id, "offset":offset, "part":index}),
                                     text, text, (item.token_id,), provenance))
    first = min(i for i, s in enumerate(document.source_tokens) if s.token_id in token.source_token_ids)
    remaining = [s for s in document.source_tokens if s.token_id not in token.source_token_ids]
    remaining[first:first] = sources
    remaining.sort(key=lambda item: item.start)
    source_tokens = tuple(replace(s, index=i) for i, s in enumerate(remaining))
    display_tokens = tuple(part for t in document.display_tokens for part in (displays if t.token_id == token_id else [t]))
    def update_owner(owner):
        return replace(owner, display_token_ids=tuple(part for tid in owner.display_token_ids
            for part in ([t.token_id for t in displays] if tid == token_id else [tid])))
    result = replace(document, source_tokens=source_tokens, display_tokens=display_tokens,
                     cues=tuple(update_owner(c) if token_id in c.display_token_ids else c for c in document.cues),
                     changes=(*document.changes, provenance))
    return synchronize_translation_status(result, {t.token_id for t in displays})
