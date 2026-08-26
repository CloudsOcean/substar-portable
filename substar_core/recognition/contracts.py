from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


Progress = Callable[[str, float], None]


@dataclass(frozen=True)
class RecognitionContext:
    media_path: Path
    wav_path: Path
    job_dir: Path
    settings: dict[str, Any]
    progress: Progress


@dataclass
class TranscriptResult:
    text: str
    language: str = ""
    chunks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AlignmentResult:
    units: list[dict[str, Any]]
    chunks: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class TranscriptAdapter(Protocol):
    adapter_id: str

    def transcribe(self, context: RecognitionContext) -> TranscriptResult: ...


class AlignmentAdapter(Protocol):
    adapter_id: str

    def align(
        self,
        context: RecognitionContext,
        transcript: TranscriptResult,
    ) -> AlignmentResult: ...


class DiarizationAdapter(Protocol):
    adapter_id: str

    def assign(
        self,
        context: RecognitionContext,
        transcript: TranscriptResult,
        alignment: AlignmentResult,
    ) -> AlignmentResult: ...
