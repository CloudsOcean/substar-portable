from __future__ import annotations

from enum import Enum

from substar_core.chinese_script import convert_chinese_script
from substar_core.domain import DisplayOrder, EditorDocument, EntityState
from substar_core.translation_layout import source_is_upper
from substar_core.presentation import project_cue_lines
from substar_core.language_layout import layout_tokens


class SubtitleExportMode(str, Enum):
    SOURCE = "source"
    TARGET = "target"
    AB_SINGLE = "ab-single"
    AB_DOUBLE = "ab-double"


def _stamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def _subtitle_records(document, mode, language_rows):
    from substar_core.translation_units import all_units
    if all_units(document):
        from types import SimpleNamespace
        for start, end, text in _independent_records(document, mode, language_rows):
            yield SimpleNamespace(start=start, end=end), text
        return
    if mode is SubtitleExportMode.AB_SINGLE:
        for track in (SubtitleExportMode.SOURCE, SubtitleExportMode.TARGET):
            yield from _subtitle_records(document, track, language_rows)
        return
    tokens = {token.token_id: token for token in document.display_tokens}
    for cue in sorted(document.cues, key=lambda item: item.index):
        if cue.state is not EntityState.ACTIVE:
            continue
        source = layout_tokens(
            tokens[token_id].text
            for token_id in cue.display_token_ids
            if tokens[token_id].state is EntityState.ACTIVE
        )
        if cue.source_text is not None:
            source = cue.source_text
        target = cue.target.target_text.strip() if cue.target is not None else ""
        source_upper = source_is_upper(document, source)
        source, target = project_cue_lines(document, source=source, target=target)
        projection = document.properties.script_projection
        if projection != "original":
            source = convert_chinese_script(source, projection)
            target = convert_chinese_script(target, projection)
        if mode is SubtitleExportMode.SOURCE:
            lines = [source if source_upper or not document.presentation.translation_layout or not language_rows else target]
        elif mode is SubtitleExportMode.TARGET:
            lines = [target if source_upper or not document.presentation.translation_layout or not language_rows else source]
        elif source_upper:
            lines = [value for value in (source, target) if value]
        else:
            lines = [value for value in (target, source) if value]
        if not any(lines):
            continue
        yield cue, "\n".join(lines)


def render_document_srt(
    document: EditorDocument, mode: SubtitleExportMode | str, *, language_rows: bool = True
) -> str:
    """Render the current revision only; completion is never an export gate."""
    records = _subtitle_records(document, SubtitleExportMode(mode), language_rows)
    blocks = [
        f"{index}\n{_stamp(cue.start)} --> {_stamp(cue.end)}\n{text}"
        for index, (cue, text) in enumerate(records, 1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_document_txt(document: EditorDocument, mode: SubtitleExportMode | str) -> str:
    """Keep the selected subtitle layout as unnumbered UTF-8 text."""
    records = _subtitle_records(document, SubtitleExportMode(mode), True)
    separator = "\n\n" if SubtitleExportMode(mode) is SubtitleExportMode.AB_DOUBLE else "\n"
    blocks = [text for _, text in records]
    return separator.join(blocks) + ("\n" if blocks else "")


def _independent_records(document, mode, language_rows):
    from substar_core.translation_units import target_events
    tokens = {token.token_id: token for token in document.display_tokens}
    source = []
    upper = []
    lower = []
    cues = {cue.cue_id: cue for cue in document.cues}
    source_text = {}
    for cue in document.cues:
        if cue.state is not EntityState.ACTIVE:
            continue
        text = cue.source_text if cue.source_text is not None else layout_tokens(
            tokens[tid].text for tid in cue.display_token_ids if tokens[tid].state is EntityState.ACTIVE)
        source_text[cue.cue_id] = text
        shown, _ = project_cue_lines(document, source=text, target="")
        if document.properties.script_projection != "original":
            shown = convert_chinese_script(shown, document.properties.script_projection)
        if shown:
            item = (cue.start, cue.end, shown)
            source.append(item)
            (upper if source_is_upper(document, text) else lower).append(item)
    target = []
    for unit in target_events(document):
        raw_source = source_text.get(unit["cue_id"], "")
        _, shown = project_cue_lines(document, source=raw_source, target=unit["text"])
        if document.properties.script_projection != "original":
            shown = convert_chinese_script(shown, document.properties.script_projection)
        if shown:
            item = (unit["start"], unit["end"], shown)
            target.append(item)
            (lower if source_is_upper(document, raw_source) else upper).append(item)

    if mode is SubtitleExportMode.AB_SINGLE:
        tracks = [upper, lower] if document.presentation.translation_layout and language_rows else [source, target]
        records = sorted(tracks[0]) + sorted(tracks[1])
    elif mode is SubtitleExportMode.AB_DOUBLE:
        edges = sorted({time for start, end, _ in upper + lower for time in (start, end)})
        records = []
        for start, end in zip(edges, edges[1:]):
            lines = list(dict.fromkeys(text for a, b, text in sorted(upper) if a <= start < b))
            lines += list(dict.fromkeys(text for a, b, text in sorted(lower) if a <= start < b))
            if not lines:
                continue
            text = "\n".join(lines)
            if records and records[-1][1] == start and records[-1][2] == text:
                records[-1] = (records[-1][0], end, text)
            else:
                records.append((start, end, text))
    else:
        if document.presentation.translation_layout and language_rows:
            records = sorted(upper if mode is SubtitleExportMode.SOURCE else lower)
        else:
            records = sorted(source if mode is SubtitleExportMode.SOURCE else target)
    return records
