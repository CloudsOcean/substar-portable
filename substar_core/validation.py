from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from substar_core.domain import EditorDocument, EntityState
from substar_core.language_layout import layout_tokens
from substar_core.policy import count_visible_characters
from substar_core.presentation import project_cue_lines


VALIDATION_REPORT_SCHEMA = "substar.validation-report.v2"


class ValidationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HARD = "hard"


class ValidationTrack(str, Enum):
    SOURCE = "source"
    TARGET = "target"
    DOCUMENT = "document"


@dataclass(frozen=True)
class ValidationPolicy:
    source_hard_limit: int = 55
    target_hard_limit: int = 24
    count_spaces: bool = True
    count_punctuation: bool = True

    def __post_init__(self) -> None:
        if self.source_hard_limit < 1 or self.target_hard_limit < 1:
            raise ValueError("hard limits must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_hard_limit": self.source_hard_limit,
            "target_hard_limit": self.target_hard_limit,
            "count_spaces": self.count_spaces,
            "count_punctuation": self.count_punctuation,
        }


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: ValidationSeverity
    track: ValidationTrack
    message: str
    cue_id: str | None = None
    measured: int | None = None
    limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "track": self.track.value,
            "message": self.message,
            "cue_id": self.cue_id,
            "measured": self.measured,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class ValidationReport:
    document_id: str
    revision_id: str
    document_hash: str
    policy: ValidationPolicy
    issues: tuple[ValidationIssue, ...]
    schema_version: str = VALIDATION_REPORT_SCHEMA

    @property
    def hard_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity is ValidationSeverity.HARD
        )

    @property
    def passes_hard_validation(self) -> bool:
        return not self.hard_issues

    def applies_to(
        self, *, document_id: str, revision_id: str, document_hash: str
    ) -> bool:
        """A report is usable only for the exact immutable revision it inspected."""

        return (
            self.document_id == document_id
            and self.revision_id == revision_id
            and self.document_hash == document_hash
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "revision_id": self.revision_id,
            "document_hash": self.document_hash,
            "policy": self.policy.to_dict(),
            "passes_hard_validation": self.passes_hard_validation,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _character_count(
    text: str,
    *,
    count_spaces: bool,
    count_punctuation: bool,
) -> int:
    return count_visible_characters(
        text,
        count_spaces=count_spaces,
        count_punctuation=count_punctuation,
    )


def source_cue_text(document: EditorDocument, cue_id: str) -> str:
    tokens = {token.token_id: token for token in document.display_tokens}
    cue = next(cue for cue in document.cues if cue.cue_id == cue_id)
    return layout_tokens(
        [
            tokens[token_id].text
            for token_id in cue.display_token_ids
            if tokens[token_id].state is EntityState.ACTIVE
        ]
    )


def _is_reference_script(document: EditorDocument) -> bool:
    return any(
        change.operation == "build_from_segmentation"
        and str(change.metadata.get("generation_mode", "")) == "reference_script"
        for change in document.changes
    )


def validate_revision(
    document: EditorDocument,
    *,
    revision_id: str,
    policy: ValidationPolicy | None = None,
) -> ValidationReport:
    """Validate presentation limits without mutating document lifecycle state.

    Structural invalidity cannot reach this function because ``EditorDocument``
    validates at construction/deserialization time.  This report therefore holds
    editor-addressable issues only and is never stored as a job status.
    """

    effective_policy = policy or ValidationPolicy()
    reference_script = _is_reference_script(document)
    issues: list[ValidationIssue] = []
    for cue in document.cues:
        if cue.state is EntityState.DELETED:
            continue
        source, target = project_cue_lines(
            document,
            source=source_cue_text(document, cue.cue_id),
            target=cue.target.target_text if cue.target is not None else "",
        )
        source_length = _character_count(
            source,
            count_spaces=effective_policy.count_spaces,
            count_punctuation=effective_policy.count_punctuation,
        )
        if source_length > effective_policy.source_hard_limit:
            issues.append(
                ValidationIssue(
                    code=(
                        "source_length_warning"
                        if reference_script
                        else "source_hard_limit"
                    ),
                    severity=(
                        ValidationSeverity.WARNING
                        if reference_script
                        else ValidationSeverity.HARD
                    ),
                    track=ValidationTrack.SOURCE,
                    message=(
                        f"源语字幕 {source_length} 字符，超过"
                        + (
                            "建议长度 "
                            if reference_script
                            else "硬上限 "
                        )
                        + f"{effective_policy.source_hard_limit}"
                    ),
                    cue_id=cue.cue_id,
                    measured=source_length,
                    limit=effective_policy.source_hard_limit,
                )
            )
        if cue.target is None:
            continue
        target_length = _character_count(
            target,
            count_spaces=effective_policy.count_spaces,
            count_punctuation=effective_policy.count_punctuation,
        )
        if target_length > effective_policy.target_hard_limit:
            issues.append(
                ValidationIssue(
                    code="target_hard_limit",
                    severity=ValidationSeverity.HARD,
                    track=ValidationTrack.TARGET,
                    message=(
                        f"译文字幕 {target_length} 字符，超过硬上限 "
                        f"{effective_policy.target_hard_limit}"
                    ),
                    cue_id=cue.cue_id,
                    measured=target_length,
                    limit=effective_policy.target_hard_limit,
                )
            )
    return ValidationReport(
        document_id=document.document_id,
        revision_id=revision_id,
        document_hash=document.content_hash(),
        policy=effective_policy,
        issues=tuple(issues),
    )


def report_fingerprint(report: ValidationReport) -> str:
    encoded = json.dumps(
        report.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
