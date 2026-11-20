from __future__ import annotations

from substar_core.domain import DisplayOrder, EditorDocument
from substar_core.language_layout import format_text, remove_grouping_commas
from substar_core.translation_layout import source_is_upper


def project_line(text: str, *, remove: str = "", space: str = "") -> str:
    remove_chars = set(remove)
    space_chars = set(space) - remove_chars
    value = remove_grouping_commas(str(text or ""), remove + space)
    digits = "0123456789０１２３４５６７８９"
    projected = "".join(
        character if (
            character == "." and 0 < index < len(value) - 1
            and value[index - 1] in digits and value[index + 1] in digits
        ) else "" if character in remove_chars else " " if character in space_chars else character
        for index, character in enumerate(value)
    )
    return format_text(projected)


def project_cue_lines(
    document: EditorDocument, *, source: str, target: str, source_upper: bool | None = None
) -> tuple[str, str]:
    if source_upper is None:
        source_upper = source_is_upper(document, source)
    source_remove = document.presentation.upper_remove if source_upper else document.presentation.lower_remove
    source_space = document.presentation.upper_space if source_upper else document.presentation.lower_space
    target_remove = document.presentation.lower_remove if source_upper else document.presentation.upper_remove
    target_space = document.presentation.lower_space if source_upper else document.presentation.upper_space
    return (
        project_line(source, remove=source_remove, space=source_space),
        project_line(target, remove=target_remove, space=target_space),
    )
