from __future__ import annotations

from substar_core.domain import DisplayOrder, EditorDocument
from substar_core.language_layout import format_text


def project_line(text: str, *, remove: str = "", space: str = "") -> str:
    remove_chars = set(remove)
    space_chars = set(space) - remove_chars
    projected = "".join(
        "" if character in remove_chars else " " if character in space_chars else character
        for character in str(text or "")
    )
    return format_text(projected)


def project_cue_lines(
    document: EditorDocument, *, source: str, target: str
) -> tuple[str, str]:
    source_is_upper = document.presentation.display_order is DisplayOrder.SOURCE_ABOVE_TARGET
    source_remove = document.presentation.upper_remove if source_is_upper else document.presentation.lower_remove
    source_space = document.presentation.upper_space if source_is_upper else document.presentation.lower_space
    target_remove = document.presentation.lower_remove if source_is_upper else document.presentation.upper_remove
    target_space = document.presentation.lower_space if source_is_upper else document.presentation.upper_space
    return (
        project_line(source, remove=source_remove, space=source_space),
        project_line(target, remove=target_remove, space=target_space),
    )
