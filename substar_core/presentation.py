from __future__ import annotations

from substar_core.domain import DisplayOrder, EditorDocument
from substar_core.language_layout import format_text
from substar_core.translation_layout import source_is_upper


def project_line(text: str, *, remove: str = "", space: str = "") -> str:
    remove_chars = set(remove)
    space_chars = set(space) - remove_chars
    projected = "".join(
        "" if character in remove_chars else " " if character in space_chars else character
        for character in str(text or "")
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
