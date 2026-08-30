from __future__ import annotations

from enum import Enum


CALIBRATION_RESULT_SCHEMA = "substar.calibration-result.v2"


class CalibrationActionKind(str, Enum):
    SET_CASE = "set_case"
    SET_PUNCTUATION = "set_punctuation"
    REPLACE_TOKEN = "replace_token"
    REPLACE_SPAN = "replace_span"
    MERGE_SPAN = "merge_span"


class CalibrationDisposition(str, Enum):
    APPLY = "apply"
    REVIEW = "review"


class CalibrationEvidenceKind(str, Enum):
    GLOSSARY = "glossary"
    REFERENCE_DOCUMENT = "reference_document"
    DOCUMENT_CONSISTENCY = "document_consistency"
    CONTEXT = "context"
    USER_INSTRUCTION = "user_instruction"

