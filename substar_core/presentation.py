from __future__ import annotations

from unicodedata import category

from substar_core.domain import EditorDocument
from substar_core.language_layout import format_text, remove_grouping_commas
from substar_core.translation_layout import source_is_upper


def punctuation_tail_start(text: str, symbols: str) -> int:
    index = len(text)
    while index:
        character = text[index - 1]
        if not (character.isspace() or character in symbols or category(character).startswith("P")):
            break
        index -= 1
    return index


def project_line(
    text: str, *, remove: str = "", space: str = "",
    filter_inline: bool = True, at_line_end: bool = True,
) -> str:
    remove_chars = set(remove)
    space_chars = set(space) - remove_chars
    value = str(text or "")
    if filter_inline:
        value = remove_grouping_commas(value, remove + space)
        start = 0
    elif at_line_end:
        start = punctuation_tail_start(value, remove + space)
    else:
        return format_text(value)
    digits = "0123456789０１２３４５６７８９"
    projected = "".join(
        character if index < start or (
            character == "." and 0 < index < len(value) - 1
            and value[index - 1] in digits and value[index + 1] in digits
        ) else "" if character in remove_chars else " " if character in space_chars else character
        for index, character in enumerate(value)
    )
    return format_text(projected)


def line_punctuation_options(document: EditorDocument, upper: bool) -> dict:
    side = "upper" if upper else "lower"
    return {
        "remove": getattr(document.presentation, f"{side}_remove"),
        "space": getattr(document.presentation, f"{side}_space"),
        "filter_inline": getattr(document.presentation, f"{side}_filter_inline"),
    }


def project_cue_lines(
    document: EditorDocument, *, source: str, target: str, source_upper: bool | None = None
) -> tuple[str, str]:
    if source_upper is None:
        source_upper = source_is_upper(document, source)
    return (
        project_line(source, **line_punctuation_options(document, source_upper)),
        project_line(target, **line_punctuation_options(document, not source_upper)),
    )
