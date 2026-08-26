from __future__ import annotations

from enum import Enum

from substar_core.chinese_script import convert_chinese_script
from substar_core.domain import DisplayOrder, EditorDocument, EntityState
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


def render_document_srt(
    document: EditorDocument, mode: SubtitleExportMode | str
) -> str:
    """Render the current revision only; completion is never an export gate."""

    mode = SubtitleExportMode(mode)
    tokens = {token.token_id: token for token in document.display_tokens}
    blocks: list[str] = []
    output_index = 0
    for cue in sorted(document.cues, key=lambda item: item.index):
        if cue.state is not EntityState.ACTIVE:
            continue
        source = layout_tokens(
            tokens[token_id].text
            for token_id in cue.display_token_ids
            if tokens[token_id].state is EntityState.ACTIVE
        )
        target = cue.target.target_text.strip() if cue.target is not None else ""
        source, target = project_cue_lines(document, source=source, target=target)
        projection = document.properties.script_projection
        if projection != "original":
            source = convert_chinese_script(source, projection)
            target = convert_chinese_script(target, projection)
        if mode is SubtitleExportMode.SOURCE:
            lines = [source]
        elif mode is SubtitleExportMode.TARGET:
            lines = [target]
        elif mode is SubtitleExportMode.AB_SINGLE:
            lines = [" ".join(value for value in (source, target) if value).strip()]
        elif document.presentation.display_order is DisplayOrder.SOURCE_ABOVE_TARGET:
            lines = [value for value in (source, target) if value]
        else:
            lines = [value for value in (target, source) if value]
        if not any(lines):
            continue
        output_index += 1
        blocks.append(
            "\n".join(
                [
                    str(output_index),
                    f"{_stamp(cue.start)} --> {_stamp(cue.end)}",
                    *lines,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")
