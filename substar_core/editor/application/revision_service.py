from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from substar_core.editor.ports import ProjectRepository


@dataclass(frozen=True)
class RevisionMetadataPage:
    latest_revision_id: str | None
    items: tuple[dict[str, Any], ...]
    next_before: int | None


class RevisionService:
    """Read revision metadata without reconstructing document revisions."""

    def __init__(
        self, repository_factory: Callable[[str], ProjectRepository]
    ) -> None:
        self._repository_factory = repository_factory

    def list_metadata(
        self,
        project_id: str,
        *,
        limit: int | None = None,
        before_revision_number: int | None = None,
    ) -> RevisionMetadataPage:
        repository = self._repository_factory(project_id)
        latest = repository.list_revision_metadata(limit=1)
        fetch_limit = limit + 1 if limit is not None else None
        rows = repository.list_revision_metadata(
            limit=fetch_limit,
            before_revision_number=before_revision_number,
        )
        has_more = limit is not None and len(rows) > limit
        if has_more:
            rows = rows[:limit]
        return RevisionMetadataPage(
            latest_revision_id=latest[0]["revision_id"] if latest else None,
            items=tuple(rows),
            next_before=(rows[-1]["revision_number"] if has_more and rows else None),
        )
