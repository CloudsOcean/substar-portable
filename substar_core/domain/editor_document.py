from __future__ import annotations

import hashlib
import json
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping

try:
    import orjson
except ImportError:  # pragma: no cover - the packaged runtime installs orjson
    orjson = None


EDITOR_DOCUMENT_SCHEMA = "substar.editor-document.v1"
EDITOR_REVISION_SCHEMA = "substar.editor-revision.v1"


class DocumentValidationError(ValueError):
    """Raised when an editor document violates its structural contract."""


class ChangeKind(str, Enum):
    SOURCE = "source"
    IMPORT = "import"
    MANUAL = "manual"
    AI = "ai"
    NORMALIZATION = "normalization"


class EntityState(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"


class PunctuationPresentation(str, Enum):
    REMOVE = "remove"
    SPACE = "space"


class DisplayOrder(str, Enum):
    SOURCE_ABOVE_TARGET = "source_above_target"
    TARGET_ABOVE_SOURCE = "target_above_source"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical_bytes(value: Any) -> bytes:
    if orjson is not None:
        return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def stable_id(namespace: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_bytes(value)).hexdigest()[:24]
    return f"{namespace}_{digest}"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DocumentValidationError(f"{field_name} must be non-empty text")


def _require_time_range(start: float, end: float, field_name: str) -> None:
    if start < 0 or end <= start:
        raise DocumentValidationError(f"{field_name} must satisfy 0 <= start < end")


def _require_source_time_range(start: float, end: float) -> None:
    """Preserve provider timing evidence, including zero-width ASR tokens."""
    if start < 0 or end < start:
        raise DocumentValidationError(
            "source token time range must satisfy 0 <= start <= end"
        )


@dataclass(frozen=True)
class ChangeProvenance:
    kind: ChangeKind
    operation: str
    actor: str = "system"
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ChangeKind(self.kind))
        _require_text(self.operation, "operation")
        _require_text(self.actor, "actor")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "operation": self.operation,
            "actor": self.actor,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChangeProvenance":
        return cls(
            kind=ChangeKind(value["kind"]),
            operation=str(value["operation"]),
            actor=str(value.get("actor", "system")),
            created_at=str(value["created_at"]),
            metadata=dict(value.get("metadata", {})),
        )


@dataclass(frozen=True)
class SourceToken:
    token_id: str
    index: int
    text: str
    start: float
    end: float
    speaker: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "index", int(self.index))
        object.__setattr__(self, "start", float(self.start))
        object.__setattr__(self, "end", float(self.end))
        _require_text(self.token_id, "source token_id")
        if self.index < 0:
            raise DocumentValidationError("source token index must be non-negative")
        _require_text(self.text, "source token text")
        _require_source_time_range(self.start, self.end)

    @classmethod
    def create(
        cls, *, index: int, text: str, start: float, end: float, speaker: str | None = None
    ) -> "SourceToken":
        index = int(index)
        start = float(start)
        end = float(end)
        token_id = stable_id(
            "src",
            {"index": index, "text": text, "start": start, "end": end, "speaker": speaker},
        )
        return cls(token_id, index, text, start, end, speaker)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "index": self.index,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "speaker": self.speaker,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceToken":
        return cls(
            token_id=str(value["token_id"]),
            index=int(value["index"]),
            text=str(value["text"]),
            start=float(value["start"]),
            end=float(value["end"]),
            speaker=value.get("speaker"),
        )


@dataclass(frozen=True)
class DisplayToken:
    token_id: str
    text: str
    original_text: str
    source_token_ids: tuple[str, ...]
    provenance: ChangeProvenance
    state: EntityState = EntityState.ACTIVE

    def __post_init__(self) -> None:
        _require_text(self.token_id, "display token_id")
        _require_text(self.text, "display token text")
        _require_text(self.original_text, "display token original_text")
        object.__setattr__(self, "state", EntityState(self.state))
        object.__setattr__(self, "source_token_ids", tuple(self.source_token_ids))
        if len(set(self.source_token_ids)) != len(self.source_token_ids):
            raise DocumentValidationError("a display token cannot repeat a source token")
        if not self.source_token_ids and self.provenance.kind is not ChangeKind.MANUAL:
            raise DocumentValidationError(
                "only manually created display tokens may omit source lineage"
            )

    @classmethod
    def create(
        cls,
        *,
        position: int,
        text: str,
        source_token_ids: Iterable[str] = (),
        provenance: ChangeProvenance,
        original_text: str | None = None,
        state: EntityState = EntityState.ACTIVE,
    ) -> "DisplayToken":
        lineage = tuple(source_token_ids)
        token_id = stable_id(
            "dsp", {"position": position, "text": text, "source_token_ids": lineage}
        )
        return cls(token_id, text, original_text or text, lineage, provenance, state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "text": self.text,
            "original_text": self.original_text,
            "source_token_ids": list(self.source_token_ids),
            "provenance": self.provenance.to_dict(),
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DisplayToken":
        return cls(
            token_id=str(value["token_id"]),
            text=str(value["text"]),
            original_text=str(value["original_text"]),
            source_token_ids=tuple(str(item) for item in value.get("source_token_ids", [])),
            provenance=ChangeProvenance.from_dict(value["provenance"]),
            state=EntityState(value["state"]),
        )


@dataclass(frozen=True)
class TranslationTrack:
    target_text: str
    provenance: ChangeProvenance
    original_text: str | None = None
    language: str | None = None
    translation_status: str = "translated"
    issue_code: str | None = None
    editable: bool = True

    def __post_init__(self) -> None:
        if self.translation_status not in {"translated", "manual_required"}:
            raise DocumentValidationError(
                f"unsupported translation status: {self.translation_status!r}"
            )
        if self.translation_status == "translated":
            _require_text(self.target_text, "target_text")
        elif not self.editable or self.issue_code != "translation_unresolved":
            raise DocumentValidationError(
                "manual_required translation must be editable and identify translation_unresolved"
            )
        if self.original_text is not None and self.original_text:
            _require_text(self.original_text, "target original_text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_text": self.target_text,
            "original_text": self.original_text,
            "language": self.language,
            "translation_status": self.translation_status,
            "issue_code": self.issue_code,
            "editable": self.editable,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TranslationTrack":
        return cls(
            target_text=str(value["target_text"]),
            original_text=value.get("original_text"),
            language=value.get("language"),
            translation_status=str(value.get("translation_status") or "translated"),
            issue_code=(str(value["issue_code"]) if value.get("issue_code") else None),
            editable=bool(value.get("editable", True)),
            provenance=ChangeProvenance.from_dict(value["provenance"]),
        )


@dataclass(frozen=True)
class SemanticGroup:
    group_id: str
    origin: str
    provenance: ChangeProvenance
    source_group_ids: tuple[str, ...] = ()
    execution_block_ids: tuple[str, ...] = ()
    dirty_flags: tuple[str, ...] = ()
    migration_confidence: str = "native"

    def __post_init__(self) -> None:
        _require_text(self.group_id, "group_id")
        if self.origin not in {"segmentation", "manual", "merged"}:
            raise DocumentValidationError(f"unsupported group origin: {self.origin!r}")
        if self.migration_confidence not in {"native", "high", "low"}:
            raise DocumentValidationError(
                f"unsupported group migration confidence: {self.migration_confidence!r}"
            )
        for name in ("source_group_ids", "execution_block_ids", "dirty_flags"):
            values = tuple(str(value) for value in getattr(self, name))
            if len(values) != len(set(values)):
                raise DocumentValidationError(f"group {name} values must be unique")
            object.__setattr__(self, name, values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "origin": self.origin,
            "source_group_ids": list(self.source_group_ids),
            "execution_block_ids": list(self.execution_block_ids),
            "dirty_flags": list(self.dirty_flags),
            "migration_confidence": self.migration_confidence,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SemanticGroup":
        return cls(
            group_id=str(value["group_id"]),
            origin=str(value["origin"]),
            source_group_ids=tuple(str(item) for item in value.get("source_group_ids", [])),
            execution_block_ids=tuple(
                str(item) for item in value.get("execution_block_ids", [])
            ),
            dirty_flags=tuple(str(item) for item in value.get("dirty_flags", [])),
            migration_confidence=str(value.get("migration_confidence", "native")),
            provenance=ChangeProvenance.from_dict(value["provenance"]),
        )


@dataclass(frozen=True)
class DisplayCue:
    cue_id: str
    index: int
    display_token_ids: tuple[str, ...]
    start: float
    end: float
    target: TranslationTrack | None = None
    speaker: str | None = None
    state: EntityState = EntityState.ACTIVE
    group_id: str | None = None
    mapping: Mapping[str, Any] = field(default_factory=dict)
    translation: InitVar[str | None] = None

    def __post_init__(self, translation: str | None) -> None:
        object.__setattr__(self, "index", int(self.index))
        object.__setattr__(self, "start", float(self.start))
        object.__setattr__(self, "end", float(self.end))
        _require_text(self.cue_id, "cue_id")
        if translation is not None:
            raise DocumentValidationError(
                "bare translation strings are unsupported; use TranslationTrack"
            )
        object.__setattr__(self, "state", EntityState(self.state))
        if self.group_id is not None:
            _require_text(self.group_id, "cue group_id")
        object.__setattr__(self, "mapping", dict(self.mapping))
        if self.index < 0:
            raise DocumentValidationError("cue index must be non-negative")
        object.__setattr__(self, "display_token_ids", tuple(self.display_token_ids))
        if not self.display_token_ids:
            raise DocumentValidationError("a cue must contain at least one display token")
        if len(set(self.display_token_ids)) != len(self.display_token_ids):
            raise DocumentValidationError("a cue cannot repeat a display token")
        _require_time_range(self.start, self.end, "cue time range")

    @classmethod
    def create(
        cls,
        *,
        index: int,
        display_token_ids: Iterable[str],
        start: float,
        end: float,
        target: TranslationTrack | None = None,
        speaker: str | None = None,
        state: EntityState = EntityState.ACTIVE,
        group_id: str | None = None,
        mapping: Mapping[str, Any] | None = None,
    ) -> "DisplayCue":
        index = int(index)
        start = float(start)
        end = float(end)
        token_ids = tuple(display_token_ids)
        cue_id = stable_id(
            "cue", {"index": index, "display_token_ids": token_ids, "start": start, "end": end}
        )
        return cls(
            cue_id=cue_id,
            index=index,
            display_token_ids=token_ids,
            start=start,
            end=end,
            target=target,
            speaker=speaker,
            state=state,
            group_id=group_id,
            mapping=dict(mapping or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "cue_id": self.cue_id,
            "index": self.index,
            "display_token_ids": list(self.display_token_ids),
            "start": self.start,
            "end": self.end,
            "target": self.target.to_dict() if self.target is not None else None,
            "speaker": self.speaker,
            "state": self.state.value,
        }
        if self.group_id is not None:
            value["group_id"] = self.group_id
        if self.mapping:
            value["mapping"] = dict(self.mapping)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DisplayCue":
        return cls(
            cue_id=str(value["cue_id"]),
            index=int(value["index"]),
            display_token_ids=tuple(str(item) for item in value["display_token_ids"]),
            start=float(value["start"]),
            end=float(value["end"]),
            target=(
                TranslationTrack.from_dict(value["target"])
                if value.get("target") is not None
                else None
            ),
            speaker=value.get("speaker"),
            state=EntityState(value["state"]),
            group_id=(str(value["group_id"]) if value.get("group_id") else None),
            mapping=dict(value.get("mapping", {})),
        )


@dataclass(frozen=True)
class PresentationSettings:
    upper_punctuation: PunctuationPresentation = PunctuationPresentation.REMOVE
    lower_punctuation: PunctuationPresentation = PunctuationPresentation.SPACE
    display_order: DisplayOrder = DisplayOrder.SOURCE_ABOVE_TARGET
    upper_remove: str = ""
    upper_space: str = ""
    lower_remove: str = ""
    lower_space: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "upper_punctuation", PunctuationPresentation(self.upper_punctuation)
        )
        object.__setattr__(
            self, "lower_punctuation", PunctuationPresentation(self.lower_punctuation)
        )
        object.__setattr__(self, "display_order", DisplayOrder(self.display_order))
        for name in ("upper_remove", "upper_space", "lower_remove", "lower_space"):
            object.__setattr__(self, name, str(getattr(self, name) or ""))

    def to_dict(self) -> dict[str, str]:
        return {
            "upper_punctuation": self.upper_punctuation.value,
            "lower_punctuation": self.lower_punctuation.value,
            "display_order": self.display_order.value,
            "upper_remove": self.upper_remove,
            "upper_space": self.upper_space,
            "lower_remove": self.lower_remove,
            "lower_space": self.lower_space,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PresentationSettings":
        return cls(
            upper_punctuation=PunctuationPresentation(value["upper_punctuation"]),
            lower_punctuation=PunctuationPresentation(value["lower_punctuation"]),
            display_order=DisplayOrder(value["display_order"]),
            upper_remove=str(value.get("upper_remove", "")),
            upper_space=str(value.get("upper_space", "")),
            lower_remove=str(value.get("lower_remove", "")),
            lower_space=str(value.get("lower_space", "")),
        )


@dataclass(frozen=True)
class DocumentProperties:
    complete: bool = False
    speaker_names: tuple[tuple[str, str], ...] = ()
    script_projection: str = "original"

    def __post_init__(self) -> None:
        if not isinstance(self.complete, bool):
            raise DocumentValidationError("properties.complete must be a boolean attribute")
        normalized = tuple((str(key), str(value).strip()) for key, value in self.speaker_names)
        if len({key for key, _ in normalized}) != len(normalized):
            raise DocumentValidationError("properties.speaker_names keys must be unique")
        object.__setattr__(self, "speaker_names", normalized)
        projection = str(self.script_projection or "original")
        if projection not in {
            "original", "simplified", "traditional", "traditional_tw", "traditional_hk"
        }:
            raise DocumentValidationError("properties.script_projection is unsupported")
        object.__setattr__(self, "script_projection", projection)

    def to_dict(self) -> dict[str, Any]:
        # Omit the default to preserve the content hash of existing projects.
        value: dict[str, Any] = {"complete": self.complete}
        if self.script_projection != "original":
            value["script_projection"] = self.script_projection
        if self.speaker_names:
            value["speaker_names"] = dict(self.speaker_names)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocumentProperties":
        names = value.get("speaker_names", {})
        return cls(
            complete=value["complete"],
            speaker_names=tuple(dict(names).items()),
            script_projection=str(value.get("script_projection", "original")),
        )


@dataclass(frozen=True)
class EditorDocument:
    document_id: str
    source_tokens: tuple[SourceToken, ...]
    display_tokens: tuple[DisplayToken, ...]
    cues: tuple[DisplayCue, ...]
    groups: tuple[SemanticGroup, ...] = ()
    presentation: PresentationSettings = field(default_factory=PresentationSettings)
    properties: DocumentProperties = field(default_factory=DocumentProperties)
    changes: tuple[ChangeProvenance, ...] = ()
    schema_version: str = EDITOR_DOCUMENT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_tokens", tuple(self.source_tokens))
        object.__setattr__(self, "display_tokens", tuple(self.display_tokens))
        object.__setattr__(self, "cues", tuple(self.cues))
        object.__setattr__(self, "groups", tuple(self.groups))
        object.__setattr__(self, "changes", tuple(self.changes))
        if self.schema_version != EDITOR_DOCUMENT_SCHEMA:
            raise DocumentValidationError(
                f"unsupported editor document schema: {self.schema_version!r}"
            )
        _require_text(self.document_id, "document_id")
        self.validate()

    @property
    def complete(self) -> bool:
        """Read-only compatibility view; completion remains document metadata only."""
        return self.properties.complete

    @classmethod
    def create(
        cls,
        *,
        source_tokens: Iterable[SourceToken],
        display_tokens: Iterable[DisplayToken],
        cues: Iterable[DisplayCue],
        groups: Iterable[SemanticGroup] = (),
        document_key: str,
        complete: bool = False,
        presentation: PresentationSettings | None = None,
        properties: DocumentProperties | None = None,
        changes: Iterable[ChangeProvenance] = (),
    ) -> "EditorDocument":
        _require_text(document_key, "document_key")
        return cls(
            document_id=stable_id("doc", {"document_key": document_key}),
            source_tokens=tuple(source_tokens),
            display_tokens=tuple(display_tokens),
            cues=tuple(cues),
            groups=tuple(groups),
            presentation=presentation or PresentationSettings(),
            properties=properties or DocumentProperties(complete=complete),
            changes=tuple(changes),
        )

    def validate(self) -> None:
        source_ids = [item.token_id for item in self.source_tokens]
        display_ids = [item.token_id for item in self.display_tokens]
        cue_ids = [item.cue_id for item in self.cues]
        group_ids = [item.group_id for item in self.groups]
        for label, values in (
            ("source token", source_ids),
            ("display token", display_ids),
            ("cue", cue_ids),
            ("group", group_ids),
        ):
            if len(values) != len(set(values)):
                raise DocumentValidationError(f"duplicate {label} id")

        source_indexes = [item.index for item in self.source_tokens]
        cue_indexes = [item.index for item in self.cues]
        if source_indexes != sorted(source_indexes) or len(source_indexes) != len(set(source_indexes)):
            raise DocumentValidationError("source token indexes must be unique and ordered")
        if any(
            current.start < previous.start
            for previous, current in zip(self.source_tokens, self.source_tokens[1:])
        ):
            raise DocumentValidationError("source tokens must be time ordered")
        if cue_indexes != sorted(cue_indexes) or len(cue_indexes) != len(set(cue_indexes)):
            raise DocumentValidationError("cue indexes must be unique and ordered")

        known_source = set(source_ids)
        lineage: list[str] = []
        for token in self.display_tokens:
            unknown = set(token.source_token_ids) - known_source
            if unknown:
                raise DocumentValidationError(f"unknown source lineage: {sorted(unknown)!r}")
            lineage.extend(token.source_token_ids)
        if len(lineage) != len(set(lineage)):
            raise DocumentValidationError("source lineage must be unique")
        missing_source = known_source - set(lineage)
        if missing_source:
            raise DocumentValidationError(f"source lineage is incomplete: {sorted(missing_source)!r}")

        known_display = set(display_ids)
        known_groups = set(group_ids)
        if self.groups:
            missing_membership = [cue.cue_id for cue in self.cues if cue.group_id is None]
            if missing_membership:
                raise DocumentValidationError(
                    f"cues are missing group membership: {missing_membership!r}"
                )
            unknown_membership = {
                cue.group_id for cue in self.cues if cue.group_id not in known_groups
            }
            if unknown_membership:
                raise DocumentValidationError(
                    f"cues reference unknown groups: {sorted(unknown_membership)!r}"
                )
        elif any(cue.group_id is not None for cue in self.cues):
            raise DocumentValidationError("cue group membership requires document groups")
        display_by_id = {token.token_id: token for token in self.display_tokens}
        cue_members: list[str] = []
        cue_owners: dict[str, list[DisplayCue]] = {}
        previous_active_end = -1.0
        for cue in self.cues:
            unknown = set(cue.display_token_ids) - known_display
            if unknown:
                raise DocumentValidationError(f"cue references unknown display tokens: {sorted(unknown)!r}")
            cue_members.extend(cue.display_token_ids)
            for token_id in cue.display_token_ids:
                cue_owners.setdefault(token_id, []).append(cue)
            # Deleted cues are tombstones. They retain their original time range
            # for restoration/history, but do not occupy the active timeline.
            is_manual_cue = all(
                not display_by_id[token_id].source_token_ids
                for token_id in cue.display_token_ids
            )
            if cue.state is EntityState.ACTIVE and not is_manual_cue:
                if cue.start < previous_active_end:
                    raise DocumentValidationError(
                        "active cues must not overlap and must be time ordered"
                    )
                previous_active_end = cue.end
        repeated_members = {
            token_id: owners for token_id, owners in cue_owners.items() if len(owners) > 1
        }
        for token_id, owners in repeated_members.items():
            repeat_groups = {
                str(cue.mapping.get("source_repeat_group") or "") for cue in owners
            }
            if "" in repeat_groups or len(repeat_groups) != 1:
                raise DocumentValidationError(
                    f"display token {token_id!r} is repeated without one declared source_repeat_group"
                )
        missing_display = known_display - set(cue_members)
        if missing_display:
            raise DocumentValidationError(
                f"display token coverage is incomplete: {sorted(missing_display)!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "document_id": self.document_id,
            "presentation": self.presentation.to_dict(),
            "properties": self.properties.to_dict(),
            "source_tokens": [item.to_dict() for item in self.source_tokens],
            "display_tokens": [item.to_dict() for item in self.display_tokens],
            "cues": [item.to_dict() for item in self.cues],
            "changes": [item.to_dict() for item in self.changes],
        }
        if self.groups:
            value["groups"] = [item.to_dict() for item in self.groups]
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EditorDocument":
        schema = value.get("schema_version")
        if schema != EDITOR_DOCUMENT_SCHEMA:
            raise DocumentValidationError(f"unsupported editor document schema: {schema!r}")
        return cls(
            schema_version=str(schema),
            document_id=str(value["document_id"]),
            presentation=PresentationSettings.from_dict(value["presentation"]),
            properties=DocumentProperties.from_dict(value["properties"]),
            source_tokens=tuple(SourceToken.from_dict(item) for item in value["source_tokens"]),
            display_tokens=tuple(DisplayToken.from_dict(item) for item in value["display_tokens"]),
            cues=tuple(DisplayCue.from_dict(item) for item in value["cues"]),
            groups=tuple(SemanticGroup.from_dict(item) for item in value.get("groups", [])),
            changes=tuple(ChangeProvenance.from_dict(item) for item in value.get("changes", [])),
        )

    def content_hash(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class DocumentRevision:
    revision_id: str
    revision_number: int
    document: EditorDocument
    parent_revision_id: str | None
    provenance: ChangeProvenance
    created_at: str = field(default_factory=utc_now)
    schema_version: str = EDITOR_REVISION_SCHEMA
    document_hash: str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.schema_version != EDITOR_REVISION_SCHEMA:
            raise DocumentValidationError(f"unsupported revision schema: {self.schema_version!r}")
        if self.revision_number < 1:
            raise DocumentValidationError("revision_number must be positive")
        _require_text(self.revision_id, "revision_id")

    @classmethod
    def create(
        cls,
        *,
        revision_number: int,
        document: EditorDocument,
        parent_revision_id: str | None,
        provenance: ChangeProvenance,
        created_at: str | None = None,
    ) -> "DocumentRevision":
        document_hash = document.content_hash()
        revision_id = stable_id(
            "rev",
            {
                "document_id": document.document_id,
                "revision_number": revision_number,
                "parent_revision_id": parent_revision_id,
                "content_hash": document_hash,
            },
        )
        return cls(
            revision_id=revision_id,
            revision_number=revision_number,
            document=document,
            parent_revision_id=parent_revision_id,
            provenance=provenance,
            created_at=created_at or utc_now(),
            document_hash=document_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "parent_revision_id": self.parent_revision_id,
            "created_at": self.created_at,
            "provenance": self.provenance.to_dict(),
            "document": self.document.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocumentRevision":
        if value.get("schema_version") != EDITOR_REVISION_SCHEMA:
            raise DocumentValidationError(
                f"unsupported revision schema: {value.get('schema_version')!r}"
            )
        document = EditorDocument.from_dict(value["document"])
        return cls(
            schema_version=str(value["schema_version"]),
            revision_id=str(value["revision_id"]),
            revision_number=int(value["revision_number"]),
            parent_revision_id=value.get("parent_revision_id"),
            created_at=str(value["created_at"]),
            provenance=ChangeProvenance.from_dict(value["provenance"]),
            document=document,
            document_hash=document.content_hash(),
        )
