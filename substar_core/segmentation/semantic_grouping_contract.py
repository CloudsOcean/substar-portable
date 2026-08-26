from __future__ import annotations

from enum import Enum


SEMANTIC_GROUPING_RESULT_SCHEMA = "substar.semantic-grouping-result.v1"


class SemanticGroupingExceptionCode(str, Enum):
    INDIVISIBLE_OVERFLOW = "indivisible_overflow"
    SOURCE_TIMING_CONFLICT = "source_timing_conflict"
    SPEAKER_BOUNDARY_CONFLICT = "speaker_boundary_conflict"


SEMANTIC_GROUPING_EXCEPTION_CODES = frozenset(
    item.value for item in SemanticGroupingExceptionCode
)

