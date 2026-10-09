"""Retain unchanged words and subdivide edited spans into addressable tokens."""
from dataclasses import replace
from difflib import SequenceMatcher

from substar_core.domain import DisplayToken, EntityState, SourceToken, stable_id
from substar_core.language_layout import editor_token_fragments, layout_tokens


def edit_timed_text(document, cue, token_ids, text, provenance, operation_id):
    displays = {t.token_id: t for t in document.display_tokens}
    sources = {t.token_id: t for t in document.source_tokens}
    old = [displays[tid] for tid in token_ids if displays[tid].state is EntityState.ACTIVE]
    words = editor_token_fragments(text)
    if not old or not words:
        raise ValueError("编辑内容不能为空")
    bounds = []
    for index, token in enumerate(old):
        lineage = [sources[tid] for tid in token.source_token_ids]
        if lineage:
            bounds.append((max(cue.start, min(s.start for s in lineage)),
                           min(cue.end, max(s.end for s in lineage))))
        else:
            bounds.append((cue.start + (cue.end-cue.start)*index/len(old),
                           cue.start + (cue.end-cue.start)*(index+1)/len(old)))
    anchors = [(i+k, j+k) for i, j, count in
               SequenceMatcher(None, [t.text for t in old], words, autojunk=False).get_matching_blocks()
               for k in range(count)]

    def spans():
        points = [(-1, -1), *anchors, (len(old), len(words))]
        return [(a+1, c, b+1, d) for (a, b), (c, d) in zip(points, points[1:])]

    def interval(i, end):
        if i < end:
            return bounds[i][0], bounds[end-1][1]
        return (bounds[i-1][1] if i else cue.start,
                bounds[i][0] if i < len(old) else cue.end)

    # Insertions without a gap borrow one adjacent word. Degenerate provider
    # timestamps borrow additional neighbors until a positive span is available.
    while True:
        invalid = next(((i, end) for i, end, j, last in spans()
                        if j < last and interval(i, end)[1] <= interval(i, end)[0]), None)
        if invalid is None:
            break
        i, end = invalid
        neighbor = next((a for a in reversed(anchors) if a[0] < i), None)
        if neighbor is None:
            neighbor = next((a for a in anchors if a[0] >= end), None)
        if neighbor is None:
            all_active = [displays[tid] for tid in cue.display_token_ids if displays[tid].state is EntityState.ACTIVE]
            first = all_active.index(old[0])
            last = all_active.index(old[-1])
            if first > 0:
                return edit_timed_text(document, cue, [all_active[first-1].token_id, *token_ids],
                                       layout_tokens([all_active[first-1].text, text]), provenance, operation_id)
            if last+1 < len(all_active):
                return edit_timed_text(document, cue, [*token_ids, all_active[last+1].token_id],
                                       layout_tokens([text, all_active[last+1].text]), provenance, operation_id)
            bounds[0] = (cue.start, bounds[0][1])
            bounds[-1] = (bounds[-1][0], cue.end)
            break
        anchors.remove(neighbor)

    replacements, insertions, removed_sources = {}, {}, set()
    new_displays, new_sources, deleted = [], [], {}
    for i, end, j, last in spans():
        if i == end and j == last:
            continue
        selected = old[i:end]
        if j == last:
            for token in selected:
                deleted[token.token_id] = replace(token, state=EntityState.DELETED, provenance=provenance)
            continue
        if len(selected) == 1 and last - j == 1 and interval(i, end)[1] > interval(i, end)[0]:
            token = selected[0]
            deleted[token.token_id] = replace(token, text=words[j], provenance=provenance)
            continue
        start, finish = interval(i, end)
        if finish <= start:
            raise ValueError("该时间范围无法分配有效词元时间")
        parent_ids = [sid for token in selected for sid in token.source_token_ids]
        removed_sources.update(parent_ids)
        parts = []
        for offset, word in enumerate(words[j:last]):
            left = start + (finish-start)*offset/(last-j)
            right = start + (finish-start)*(offset+1)/(last-j)
            source_id = stable_id("src", {"operation":operation_id, "word":j+offset})
            source = SourceToken(source_id, 0, word, left, right, cue.speaker,
                                 {"kind":"subdivided", "method":"equal_split", "parent_source_ids":parent_ids,
                                  "parent_sources":[sources[sid].to_dict() for sid in dict.fromkeys(parent_ids)]})
            token = DisplayToken(stable_id("dsp", {"operation":operation_id, "word":j+offset}),
                                 word, word, (source_id,), provenance)
            new_sources.append(source); new_displays.append(token); parts.append(token.token_id)
        if selected:
            replacements[selected[0].token_id] = parts
            for token in selected[1:]:
                replacements[token.token_id] = []
        else:
            insertions[i] = parts

    def updated_cue(owner):
        ids = []
        old_positions = {t.token_id:i for i,t in enumerate(old)}
        for tid in owner.display_token_ids:
            position = old_positions.get(tid)
            if position is not None:
                ids.extend(insertions.get(position, []))
            ids.extend(replacements.get(tid, [tid]))
            if position == len(old)-1:
                ids.extend(insertions.get(len(old), []))
        return replace(owner, display_token_ids=tuple(dict.fromkeys(ids)))
    remaining_sources = [s for s in document.source_tokens if s.token_id not in removed_sources]
    ordered_sources = sorted([*remaining_sources, *new_sources], key=lambda s:s.start)
    remaining_displays = [deleted.get(t.token_id, t) for t in document.display_tokens if t.token_id not in replacements]
    return replace(document,
                   source_tokens=tuple(replace(s, index=i) for i,s in enumerate(ordered_sources)),
                   display_tokens=tuple([*remaining_displays, *new_displays]),
                   cues=tuple(updated_cue(c) if set(c.display_token_ids).intersection(token_ids) else c for c in document.cues),
                   changes=(*document.changes, provenance))
