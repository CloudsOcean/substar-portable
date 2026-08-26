from __future__ import annotations

from enum import Enum


SOURCE_REVIEW_RESULT_SCHEMA = "substar.source-review-result.v1"
TRANSLATION_REVIEW_RESULT_SCHEMA = "substar.translation-review-result.v1"


class ReviewImpact(str, Enum):
    MAJOR = "major"
    MODERATE = "moderate"
    MINOR = "minor"


class ReviewConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewStatus(str, Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    RESOLVED = "resolved"
    STALE = "stale"


class SourceReviewIssueType(str, Enum):
    SUSPECTED_MISRECOGNITION = "suspected_misrecognition"
    SUSPECTED_OMISSION = "suspected_omission"
    SUSPECTED_REPETITION = "suspected_repetition"
    NAMED_ENTITY_OR_TERM = "named_entity_or_term"
    NUMBER_OR_UNIT = "number_or_unit"
    CONTEXT_INCOHERENCE = "context_incoherence"
    SOURCE_CONSISTENCY = "source_consistency"


class TranslationReviewIssueType(str, Enum):
    MISTRANSLATION = "mistranslation"
    OMISSION = "omission"
    ADDITION = "addition"
    FACTUAL_MISMATCH = "factual_mismatch"
    POLARITY_OR_LOGIC = "polarity_or_logic"
    REFERENCE_RESOLUTION = "reference_resolution"
    TERMINOLOGY_CONSISTENCY = "terminology_consistency"
    GRAMMAR_OR_FLUENCY = "grammar_or_fluency"
    SUBTITLE_FLOW = "subtitle_flow"


class SourceReviewAction(str, Enum):
    INSPECT_AUDIO = "inspect_audio"
    REPLACE_SOURCE = "replace_source"
    VERIFY_ENTITY = "verify_entity"
    VERIFY_NUMBER = "verify_number"
    NORMALIZE_OCCURRENCES = "normalize_source_occurrences"
    MANUAL_EDIT = "manual_edit"


class TranslationReviewAction(str, Enum):
    REPLACE_TRANSLATION = "replace_translation"
    RETRANSLATE_CUE = "retranslate_cue"
    VERIFY_FACT = "verify_fact"
    INSPECT_CONTEXT = "inspect_context"
    NORMALIZE_OCCURRENCES = "normalize_translation_occurrences"
    MANUAL_EDIT = "manual_edit"


REVIEW_IMPACT_VALUES = frozenset(item.value for item in ReviewImpact)
REVIEW_CONFIDENCE_VALUES = frozenset(item.value for item in ReviewConfidence)
REVIEW_STATUS_VALUES = frozenset(item.value for item in ReviewStatus)

