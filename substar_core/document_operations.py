from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    DisplayCue,
    DisplayToken,
    DocumentProperties,
    EditorDocument,
    EntityState,
    PresentationSettings,
    SemanticGroup,
    TranslationTrack,
    stable_id,
)
from substar_core.editor.domain.cue_ordering import (
    canonicalize_cue_order,
    canonicalize_document_cues,
)
from substar_core.editor.domain.cue_timing import (
    CueTimeChange,
    CueTimingError,
    apply_cue_time,
    apply_cue_times,
)


class DocumentOperationError(ValueError):
    pass


def _provenance(operation: Mapping[str, Any], name: str) -> ChangeProvenance:
    raw = operation.get("payload", {}).get("provenance", {})
    raw_kind = str(raw.get("kind", "manual"))
    try:
        kind = ChangeKind(raw_kind)
    except ValueError as exc:
        raise DocumentOperationError(f"unsupported provenance kind: {raw_kind}") from exc
    return ChangeProvenance(
        kind=kind,
        operation=str(raw.get("operation") or name),
        actor=str(raw.get("actor") or "editor"),
        metadata={
            **dict(raw.get("metadata", {})),
            "operation_id": str(operation.get("operation_id", "")),
        },
    )


def _find_cue(document: EditorDocument, cue_id: str) -> DisplayCue:
    try:
        return next(cue for cue in document.cues if cue.cue_id == cue_id)
    except StopIteration as exc:
        raise DocumentOperationError(f"unknown cue: {cue_id}") from exc


def _token_map(document: EditorDocument) -> dict[str, DisplayToken]:
    return {token.token_id: token for token in document.display_tokens}


def _is_manual_cue(document: EditorDocument, cue: DisplayCue) -> bool:
    """Manual cues have no ASR lineage and do not constrain the source timeline."""
    tokens = _token_map(document)
    return all(not tokens[token_id].source_token_ids for token_id in cue.display_token_ids)


def _assert_deleted_cues_frozen(
    document: EditorDocument, operation_type: str, payload: Mapping[str, Any]
) -> None:
    """A deleted cue is an immutable tombstone until the cue itself is restored."""
    if operation_type == "restore":
        return
    deleted_cues = {cue.cue_id: cue for cue in document.cues if cue.state is EntityState.DELETED}
    if not deleted_cues:
        return
    deleted_tokens = {
        token_id for cue in deleted_cues.values() for token_id in cue.display_token_ids
    }
    cue_targets = set()
    token_targets = set()
    if payload.get("cue_id") is not None:
        cue_targets.add(str(payload["cue_id"]))
    cue_targets.update(str(value) for value in payload.get("cue_ids", []))
    if payload.get("token_id") is not None:
        token_targets.add(str(payload["token_id"]))
    for key in ("token_ids", "source_token_ids"):
        token_targets.update(str(value) for value in payload.get(key, []))
    for key in ("anchor_token_id", "boundary_after", "insert_after"):
        if payload.get(key) is not None:
            token_targets.add(str(payload[key]))
    for item in payload.get("cues", []):
        if isinstance(item, Mapping) and item.get("cue_id") is not None:
            cue_targets.add(str(item["cue_id"]))
    for item in payload.get("replacements", []):
        if isinstance(item, Mapping) and item.get("token_id") is not None:
            token_targets.add(str(item["token_id"]))
    if cue_targets.intersection(deleted_cues) or token_targets.intersection(deleted_tokens):
        raise DocumentOperationError("deleted cue is frozen; restore the cue before editing it")


def _replace_cue(
    cues: tuple[DisplayCue, ...], cue_id: str, replacement: DisplayCue
) -> tuple[DisplayCue, ...]:
    return tuple(replacement if cue.cue_id == cue_id else cue for cue in cues)


def _source_bounds(
    document: EditorDocument, display_ids: tuple[str, ...]
) -> tuple[float, float] | None:
    displays = _token_map(document)
    sources = {token.token_id: token for token in document.source_tokens}
    lineage = [
        sources[source_id]
        for display_id in display_ids
        for source_id in displays[display_id].source_token_ids
    ]
    if not lineage:
        return None
    return min(token.start for token in lineage), max(token.end for token in lineage)


def _reindex(cues: tuple[DisplayCue, ...]) -> tuple[DisplayCue, ...]:
    return canonicalize_cue_order(cues)


def _mark_groups_dirty(
    groups: tuple[SemanticGroup, ...], group_ids: set[str | None], *flags: str
) -> tuple[SemanticGroup, ...]:
    requested = {value for value in group_ids if value}
    if not requested:
        return groups
    return tuple(
        replace(
            group,
            dirty_flags=tuple(dict.fromkeys((*group.dirty_flags, *flags))),
        )
        if group.group_id in requested
        else group
        for group in groups
    )


def _translation_copy_group(track: TranslationTrack) -> str | None:
    value = track.provenance.metadata.get("translation_copy_group")
    return str(value) if value else None


def _copy_translation_for_split(
    track: TranslationTrack,
    *,
    cue_id: str,
    operation: Mapping[str, Any],
    provenance: ChangeProvenance,
) -> TranslationTrack:
    group = _translation_copy_group(track) or stable_id(
        "translation-copy",
        {
            "operation_id": str(operation.get("operation_id", "")),
            "cue_id": cue_id,
        },
    )
    copy_provenance = replace(
        provenance,
        operation="split_cue_translation_copy",
        metadata={
            **dict(provenance.metadata),
            "translation_copy_group": group,
        },
    )
    return TranslationTrack(
        target_text=track.target_text,
        original_text=track.original_text,
        language=track.language,
        provenance=copy_provenance,
    )


def _translation_join_separator(left: str, right: str) -> str:
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return ""
    if right[0] in "，。！？；：、,.!?;:)]}】》」』":
        return ""
    if left[-1] in "，。！？；：、":
        return ""
    left_ascii_word = left[-1].isascii() and left[-1].isalnum()
    right_ascii_word = right[0].isascii() and right[0].isalnum()
    if left[-1] in ",.!?;:" and right_ascii_word:
        return " "
    if left_ascii_word and right_ascii_word:
        return " "
    if left[-1].isascii() and left[-1].isalpha() and not right[0].isascii():
        return " "
    if not left[-1].isascii() and right[0].isascii() and right[0].isalpha():
        return " "
    return ""


def _join_translation_text(left: str, right: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    return f"{left}{_translation_join_separator(left, right)}{right}"


def _translations_are_unedited_copies(
    left: TranslationTrack, right: TranslationTrack
) -> bool:
    left_group = _translation_copy_group(left)
    return bool(
        left_group
        and left_group == _translation_copy_group(right)
        and left.target_text == right.target_text
        and left.original_text == right.original_text
        and left.language == right.language
    )


def _replace(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    payload = operation["payload"]
    token_id = str(payload["token_id"])
    text = str(payload.get("text", "")).strip()
    if not text:
        raise DocumentOperationError("replacement text cannot be empty")
    tokens = _token_map(document)
    if token_id not in tokens:
        raise DocumentOperationError(f"unknown display token: {token_id}")
    provenance = _provenance(operation, "replace")
    updated = replace(tokens[token_id], text=text, provenance=provenance)
    display_tokens = tuple(
        updated if token.token_id == token_id else token
        for token in document.display_tokens
    )
    return replace(
        document,
        display_tokens=display_tokens,
        changes=(*document.changes, provenance),
    )


def _batch_replace(
    document: EditorDocument, operation: Mapping[str, Any]
) -> EditorDocument:
    """Apply reviewed replacements as one all-or-nothing document operation."""
    payload = operation["payload"]
    raw_replacements = payload.get("replacements")
    if not isinstance(raw_replacements, list) or not raw_replacements:
        raise DocumentOperationError("batch_replace needs at least one replacement")

    tokens = _token_map(document)
    prepared: dict[str, str] = {}
    for raw in raw_replacements:
        if not isinstance(raw, Mapping):
            raise DocumentOperationError("each replacement must be an object")
        token_id = str(raw.get("token_id", "")).strip()
        if not token_id or token_id in prepared:
            raise DocumentOperationError("replacement token IDs must be unique")
        token = tokens.get(token_id)
        if token is None:
            raise DocumentOperationError(f"unknown display token: {token_id}")
        if token.state is EntityState.DELETED:
            raise DocumentOperationError(f"cannot replace deleted display token: {token_id}")
        expected_text = raw.get("expected_text")
        if expected_text is not None and str(expected_text) != token.text:
            raise DocumentOperationError(f"display token text changed: {token_id}")
        text = str(raw.get("text", "")).strip()
        if not text:
            raise DocumentOperationError("replacement text cannot be empty")
        prepared[token_id] = text

    provenance = _provenance(operation, "batch_replace")
    display_tokens = tuple(
        replace(token, text=prepared[token.token_id], provenance=provenance)
        if token.token_id in prepared
        else token
        for token in document.display_tokens
    )
    return replace(
        document,
        display_tokens=display_tokens,
        changes=(*document.changes, provenance),
    )


def _set_ai_calibration(
    document: EditorDocument, operation: Mapping[str, Any]
) -> EditorDocument:
    """Toggle text-only calibrations and reversible same-Cue merge spans."""
    payload = operation["payload"]
    action = str(payload.get("action", "")).strip().lower()
    if action not in {"cancel", "restore"}:
        raise DocumentOperationError("set_ai_calibration action must be cancel or restore")
    raw_ids = payload.get("token_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise DocumentOperationError("set_ai_calibration needs token_ids")
    token_ids = [str(value).strip() for value in raw_ids]
    if not all(token_ids) or len(set(token_ids)) != len(token_ids):
        raise DocumentOperationError("AI calibration token IDs must be unique")

    tokens = _token_map(document)
    expected_texts = payload.get("expected_texts", {})
    if expected_texts is not None and not isinstance(expected_texts, Mapping):
        raise DocumentOperationError("expected_texts must be an object")
    updated_by_id: dict[str, DisplayToken] = {}
    base_provenance = _provenance(operation, "set_ai_calibration")
    topology_records: dict[str, tuple[DisplayToken, dict[str, Any]]] = {}

    for token_id in token_ids:
        token = tokens.get(token_id)
        if token is None:
            raise DocumentOperationError(f"unknown display token: {token_id}")
        if token.state is EntityState.DELETED:
            raise DocumentOperationError(f"cannot toggle deleted display token: {token_id}")
        expected_text = expected_texts.get(token_id) if isinstance(expected_texts, Mapping) else None
        if expected_text is not None and str(expected_text) != token.text:
            raise DocumentOperationError(f"display token text changed: {token_id}")

        metadata = dict(token.provenance.metadata)
        raw_record = metadata.get("ai_calibration")
        record = dict(raw_record) if isinstance(raw_record, Mapping) else {}
        if record.get("topology") == "merge_span":
            topology_records[token_id] = (token, record)
            continue
        if action == "cancel":
            if token.provenance.kind is not ChangeKind.AI:
                raise DocumentOperationError(f"display token is not AI-calibrated: {token_id}")
            record.update({
                "before_text": token.original_text,
                "after_text": token.text,
                "applied": False,
            })
            metadata["ai_calibration"] = record
            next_provenance = replace(
                base_provenance,
                kind=ChangeKind.SOURCE,
                operation="ai_calibration_cancel",
                metadata=metadata,
            )
            next_text = token.original_text
        else:
            if token.provenance.kind is ChangeKind.AI:
                raise DocumentOperationError(f"AI calibration is already applied: {token_id}")
            if record.get("applied") is not False:
                raise DocumentOperationError(f"AI calibration cannot be restored: {token_id}")
            next_text = str(record.get("after_text", "")).strip()
            if not next_text:
                raise DocumentOperationError(f"AI calibration has no retained text: {token_id}")
            record.update({"before_text": token.original_text, "applied": True})
            metadata["ai_calibration"] = record
            next_provenance = replace(
                base_provenance,
                kind=ChangeKind.AI,
                operation="ai_calibration_restore",
                metadata=metadata,
            )
        updated_by_id[token_id] = replace(token, text=next_text, provenance=next_provenance)

    result = replace(
        document,
        display_tokens=tuple(
            updated_by_id.get(token.token_id, token)
            for token in document.display_tokens
        ),
    )

    if action == "cancel":
        for token_id, (merged_token, record) in topology_records.items():
            if (
                merged_token.provenance.kind is not ChangeKind.AI
                or record.get("applied") is not True
            ):
                raise DocumentOperationError(
                    f"AI calibration merge is not applied: {token_id}"
                )
            raw_before_tokens = record.get("before_tokens")
            if not isinstance(raw_before_tokens, list) or len(raw_before_tokens) < 2:
                raise DocumentOperationError(f"AI calibration merge has no source snapshot: {token_id}")
            try:
                before_tokens = [DisplayToken.from_dict(row) for row in raw_before_tokens]
            except (KeyError, TypeError, ValueError) as exc:
                raise DocumentOperationError(
                    f"AI calibration merge source snapshot is invalid: {token_id}"
                ) from exc
            cue_id = str(record.get("cue_id", ""))
            cue = _find_cue(result, cue_id)
            if token_id not in cue.display_token_ids:
                raise DocumentOperationError(
                    f"AI calibration merge is no longer in its Cue: {token_id}"
                )
            restored_record = {
                **record,
                "applied": False,
                "after_token_id": token_id,
            }
            restored_tokens = [
                replace(
                    token,
                    provenance=replace(
                        base_provenance,
                        kind=ChangeKind.SOURCE,
                        operation="ai_calibration_cancel",
                        metadata={"ai_calibration": restored_record},
                    ),
                )
                for token in before_tokens
            ]
            display_position = next(
                index for index, token in enumerate(result.display_tokens)
                if token.token_id == token_id
            )
            display_tokens = list(result.display_tokens)
            display_tokens[display_position : display_position + 1] = restored_tokens
            cue_ids = list(cue.display_token_ids)
            cue_position = cue_ids.index(token_id)
            cue_ids[cue_position : cue_position + 1] = [
                token.token_id for token in restored_tokens
            ]
            result = replace(
                result,
                display_tokens=tuple(display_tokens),
                cues=_replace_cue(
                    result.cues, cue_id, replace(cue, display_token_ids=tuple(cue_ids))
                ),
            )
    else:
        grouped: dict[str, list[tuple[DisplayToken, dict[str, Any]]]] = {}
        for token, record in topology_records.values():
            if token.provenance.kind is ChangeKind.AI or record.get("applied") is not False:
                raise DocumentOperationError(
                    f"AI calibration merge cannot be restored: {token.token_id}"
                )
            group_key = str(record.get("action_id", "")).strip()
            if not group_key:
                raise DocumentOperationError("AI calibration merge has no action ID")
            grouped.setdefault(group_key, []).append((token, record))
        for group_index, (_group_key, members) in enumerate(grouped.items(), start=1):
            record = members[0][1]
            raw_before_tokens = record.get("before_tokens")
            if not isinstance(raw_before_tokens, list) or len(raw_before_tokens) < 2:
                raise DocumentOperationError("AI calibration merge has no source snapshot")
            required_ids = [str(row.get("token_id", "")) for row in raw_before_tokens]
            member_ids = {token.token_id for token, _ in members}
            if not all(required_ids) or member_ids != set(required_ids):
                raise DocumentOperationError(
                    "restore requires every original token from the AI calibration merge"
                )
            cue_id = str(record.get("cue_id", ""))
            restored_record = {**record, "applied": True}
            restore_provenance = replace(
                base_provenance,
                kind=ChangeKind.AI,
                operation="ai_calibration_restore",
                metadata={"ai_calibration": restored_record},
            )
            result = _merge(result, {
                "operation_id": f"{operation.get('operation_id', '')}_merge_{group_index}",
                "type": "merge",
                "payload": {
                    "cue_id": cue_id,
                    "token_ids": required_ids,
                    "text": str(record.get("after_text", "")),
                    "provenance": restore_provenance.to_dict(),
                },
            })

    return replace(result, changes=(*result.changes, base_provenance))


def _set_presentation(
    document: EditorDocument, operation: Mapping[str, Any]
) -> EditorDocument:
    """Change export/preview presentation without rewriting subtitle text."""
    payload = operation["payload"]
    current = document.presentation
    try:
        presentation = PresentationSettings(
            upper_punctuation=payload.get(
                "upper_punctuation", current.upper_punctuation
            ),
            lower_punctuation=payload.get(
                "lower_punctuation", current.lower_punctuation
            ),
            display_order=payload.get("display_order", current.display_order),
            upper_remove=payload.get("upper_remove", current.upper_remove),
            upper_space=payload.get("upper_space", current.upper_space),
            lower_remove=payload.get("lower_remove", current.lower_remove),
            lower_space=payload.get("lower_space", current.lower_space),
        )
    except ValueError as exc:
        raise DocumentOperationError(f"invalid presentation setting: {exc}") from exc
    provenance = _provenance(operation, "set_presentation")
    return replace(
        document,
        presentation=presentation,
        changes=(*document.changes, provenance),
    )


def _set_target(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    """Edit the target track without changing source/cue topology.

    Translation is deliberately a cue-level track in V2.  Keeping this as a
    structured operation means a human edit is versioned just like a token
    edit, while preserving the original machine translation in
    ``original_text`` for review/revert tooling.
    """
    payload = operation["payload"]
    cue = _find_cue(document, str(payload["cue_id"]))
    text = str(payload.get("target_text", "")).strip()
    provenance = _provenance(operation, "set_target")
    target = None
    if text:
        previous = cue.target
        raw_original = payload.get("original_text")
        original = (
            str(raw_original).strip()
            if raw_original not in (None, "")
            else (previous.original_text if previous and previous.original_text else text)
        )
        target = TranslationTrack(
            target_text=text,
            original_text=original,
            language=str(payload.get("language") or (previous.language if previous else "zh-CN")),
            provenance=provenance,
        )
    return replace(
        document,
        cues=_replace_cue(document.cues, cue.cue_id, replace(cue, target=target)),
        changes=(*document.changes, provenance),
    )


def _set_cue_time(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    """Move/resize one cue; manual cues may overlap the ASR timeline."""
    payload = operation["payload"]
    try:
        change = CueTimeChange(
            cue_id=str(payload["cue_id"]),
            start=float(payload["start"]),
            end=float(payload["end"]),
            expected_start=(
                float(payload["expected_start"])
                if payload.get("expected_start") is not None
                else None
            ),
            expected_end=(
                float(payload["expected_end"])
                if payload.get("expected_end") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DocumentOperationError("cue time must contain numeric start/end") from exc
    try:
        return apply_cue_time(document, change, _provenance(operation, "set_cue_time"))
    except CueTimingError as exc:
        raise DocumentOperationError(str(exc)) from exc


def _set_cue_times(
    document: EditorDocument, operation: Mapping[str, Any]
) -> EditorDocument:
    """Atomically resize/move several cues, including a shared boundary."""
    raw_changes = operation["payload"].get("cues")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise DocumentOperationError("set_cue_times needs at least one cue")
    changes: list[CueTimeChange] = []
    for raw in raw_changes:
        if not isinstance(raw, Mapping):
            raise DocumentOperationError("each cue time change must be an object")
        try:
            changes.append(
                CueTimeChange(
                    cue_id=str(raw.get("cue_id", "")).strip(),
                    start=float(raw["start"]),
                    end=float(raw["end"]),
                    expected_start=(
                        float(raw["expected_start"])
                        if raw.get("expected_start") is not None
                        else None
                    ),
                    expected_end=(
                        float(raw["expected_end"])
                        if raw.get("expected_end") is not None
                        else None
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DocumentOperationError(
                "cue time change must contain numeric start/end"
            ) from exc
    try:
        return apply_cue_times(
            document, changes, _provenance(operation, "set_cue_times")
        )
    except CueTimingError as exc:
        raise DocumentOperationError(str(exc)) from exc


def _insert_cue(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    """Create a manual cue in a free half-open timeline interval."""
    payload = operation["payload"]
    try:
        start = float(payload["start"])
        end = float(payload["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DocumentOperationError("new cue time must contain numeric start/end") from exc
    if start < 0 or end <= start:
        raise DocumentOperationError("new cue time must satisfy 0 <= start < end")
    for existing in document.cues:
        if existing.state is EntityState.DELETED:
            continue
        if start < existing.end and existing.start < end:
            raise DocumentOperationError(
                f"new cue overlaps existing cue: {existing.cue_id}"
            )
    text = str(payload.get("text", "")).strip()
    if not text:
        raise DocumentOperationError("new cue text cannot be empty")
    ordered = list(document.cues)
    insert_at = len(ordered)
    for index, cue in enumerate(ordered):
        if start < cue.start:
            insert_at = index
            break
    provenance = _provenance(operation, "insert_cue")
    groups = document.groups
    group_id = None
    if groups:
        previous = ordered[insert_at - 1] if insert_at > 0 else None
        following = ordered[insert_at] if insert_at < len(ordered) else None
        if previous is not None and following is not None and previous.group_id == following.group_id:
            group_id = previous.group_id
            groups = _mark_groups_dirty(groups, {group_id}, "structure")
        else:
            group_id = stable_id("grp", {"manual_operation_id": operation["operation_id"]})
            groups = (
                *groups,
                SemanticGroup(
                    group_id=group_id,
                    origin="manual",
                    provenance=provenance,
                    dirty_flags=("membership", "structure"),
                ),
            )
    token = DisplayToken(
        token_id=stable_id("dsp", {"operation_id": operation["operation_id"]}),
        text=text,
        original_text=text,
        source_token_ids=(),
        provenance=ChangeProvenance(
            kind=ChangeKind.MANUAL,
            operation="insert_cue_token",
            actor=provenance.actor,
            metadata={**provenance.metadata, "parent_operation": provenance.operation},
        ),
    )
    cue = DisplayCue(
        cue_id=stable_id("cue", {"operation_id": operation["operation_id"]}),
        index=insert_at,
        display_token_ids=(token.token_id,),
        start=start,
        end=end,
        target=None,
        speaker=payload.get("speaker"),
        group_id=group_id,
    )
    cues = _reindex(tuple(ordered[:insert_at] + [cue] + ordered[insert_at:]))
    return replace(
        document,
        display_tokens=(*document.display_tokens, token),
        cues=cues,
        groups=groups,
        changes=(*document.changes, provenance),
    )


def _merge(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    payload = operation["payload"]
    cue = _find_cue(document, str(payload["cue_id"]))
    ids = tuple(str(value) for value in payload.get("token_ids", []))
    if len(ids) < 2:
        raise DocumentOperationError("merge needs at least two tokens")
    positions = [cue.display_token_ids.index(value) for value in ids]
    if positions != list(range(positions[0], positions[0] + len(ids))):
        raise DocumentOperationError("merge tokens must be contiguous and ordered")
    tokens = _token_map(document)
    selected = [tokens[value] for value in ids]
    provenance = _provenance(operation, "merge")
    merged = DisplayToken(
        token_id=stable_id(
            "dsp",
            {
                "operation_id": operation["operation_id"],
                "source_token_ids": [
                    source_id for token in selected for source_id in token.source_token_ids
                ],
            },
        ),
        text=str(payload.get("text", "")).strip(),
        original_text=" ".join(token.text for token in selected),
        source_token_ids=tuple(
            source_id for token in selected for source_id in token.source_token_ids
        ),
        provenance=provenance,
    )
    if not merged.text:
        raise DocumentOperationError("merged text cannot be empty")
    remaining = tuple(token for token in document.display_tokens if token.token_id not in ids)
    insert_at = next(
        index for index, token in enumerate(document.display_tokens) if token.token_id == ids[0]
    )
    display_tokens = (*remaining[:insert_at], merged, *remaining[insert_at:])
    cue_ids = list(cue.display_token_ids)
    cue_ids[positions[0] : positions[0] + len(ids)] = [merged.token_id]
    updated_cue = replace(cue, display_token_ids=tuple(cue_ids))
    return replace(
        document,
        display_tokens=display_tokens,
        cues=_replace_cue(document.cues, cue.cue_id, updated_cue),
        changes=(*document.changes, provenance),
    )


def _delete(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    payload = operation["payload"]
    requested_cues = {str(value) for value in payload.get("cue_ids", [])}
    requested_tokens = {str(value) for value in payload.get("token_ids", [])}
    cue_map = {cue.cue_id: cue for cue in document.cues}
    tokens = _token_map(document)
    if requested_cues - set(cue_map) or requested_tokens - set(tokens):
        raise DocumentOperationError("delete target does not exist")
    provenance = _provenance(operation, "delete")
    display_tokens = tuple(
        replace(token, state=EntityState.DELETED)
        if token.token_id in requested_tokens
        else token
        for token in document.display_tokens
    )
    cues = tuple(
        replace(cue, state=EntityState.DELETED)
        if cue.cue_id in requested_cues
        else cue
        for cue in document.cues
    )
    return replace(
        document,
        display_tokens=display_tokens,
        cues=cues,
        changes=(*document.changes, provenance),
    )


def _purge_cue(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    """Permanently remove active cues from this revision.

    V2 requires complete, unique source lineage. Removing a cue therefore also
    removes its owned display tokens and the source tokens referenced by them.
    The previous revision remains available through document undo/history.
    """
    payload = operation["payload"]
    requested_cues = {str(value) for value in payload.get("cue_ids", [])}
    if not requested_cues:
        raise DocumentOperationError("purge_cue needs cue_ids")
    cue_map = {cue.cue_id: cue for cue in document.cues}
    if requested_cues - set(cue_map):
        raise DocumentOperationError("purge cue target does not exist")
    if any(cue_map[cue_id].state is not EntityState.ACTIVE for cue_id in requested_cues):
        raise DocumentOperationError("only active cues can be permanently deleted")

    removed_display_ids = {
        token_id
        for cue_id in requested_cues
        for token_id in cue_map[cue_id].display_token_ids
    }
    display_map = _token_map(document)
    removed_source_ids = {
        source_id
        for token_id in removed_display_ids
        for source_id in display_map[token_id].source_token_ids
    }
    source_tokens = tuple(
        replace(token, index=index)
        for index, token in enumerate(
            token
            for token in document.source_tokens
            if token.token_id not in removed_source_ids
        )
    )
    display_tokens = tuple(
        token for token in document.display_tokens if token.token_id not in removed_display_ids
    )
    cues = _reindex(
        tuple(cue for cue in document.cues if cue.cue_id not in requested_cues)
    )
    remaining_group_ids = {cue.group_id for cue in cues if cue.group_id}
    groups = tuple(
        group for group in document.groups if group.group_id in remaining_group_ids
    )
    provenance = _provenance(operation, "purge_cue")
    provenance = replace(
        provenance,
        metadata={
            **dict(provenance.metadata),
            "removed_cue_ids": sorted(requested_cues),
            "removed_display_token_ids": sorted(removed_display_ids),
            "removed_source_token_ids": sorted(removed_source_ids),
        },
    )
    return replace(
        document,
        source_tokens=source_tokens,
        display_tokens=display_tokens,
        cues=cues,
        groups=groups,
        changes=(*document.changes, provenance),
    )


def _restore(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    payload = operation["payload"]
    requested_cues = {str(value) for value in payload.get("cue_ids", [])}
    requested_tokens = {str(value) for value in payload.get("token_ids", [])}
    cue_map = {cue.cue_id: cue for cue in document.cues}
    tokens = _token_map(document)
    if not requested_cues and not requested_tokens:
        raise DocumentOperationError("restore needs token_ids or cue_ids")
    if requested_cues - set(cue_map) or requested_tokens - set(tokens):
        raise DocumentOperationError("restore target does not exist")
    if any(cue_map[cue_id].state is not EntityState.DELETED for cue_id in requested_cues):
        raise DocumentOperationError("restore cue target must be deleted")
    if any(tokens[token_id].state is not EntityState.DELETED for token_id in requested_tokens):
        raise DocumentOperationError("restore token target must be deleted")
    token_parents = {
        token_id: cue
        for cue in document.cues
        for token_id in cue.display_token_ids
    }
    blocked_tokens = [
        token_id
        for token_id in requested_tokens
        if token_parents[token_id].state is EntityState.DELETED
        and token_parents[token_id].cue_id not in requested_cues
    ]
    if blocked_tokens:
        raise DocumentOperationError("restore the containing cue before restoring its tokens")

    restored_ranges = [
        (cue_map[cue_id].start, cue_map[cue_id].end)
        for cue_id in requested_cues
    ]
    displaced_cues = {
        cue.cue_id
        for cue in document.cues
        if cue.state is EntityState.ACTIVE
        and cue.cue_id not in requested_cues
        and not _is_manual_cue(document, cue)
        and any(not _is_manual_cue(document, cue_map[cue_id]) for cue_id in requested_cues)
        and any(
            start < cue.end - 1e-9 and end > cue.start + 1e-9
            for start, end in restored_ranges
        )
    }
    base_provenance = _provenance(operation, "restore")
    provenance = replace(
        base_provenance,
        metadata={
            **dict(base_provenance.metadata),
            "displaced_cue_ids": sorted(displaced_cues),
        },
    )
    display_tokens = tuple(
        replace(token, state=EntityState.ACTIVE)
        if token.token_id in requested_tokens
        else token
        for token in document.display_tokens
    )
    cues = tuple(
        replace(cue, state=EntityState.ACTIVE)
        if cue.cue_id in requested_cues
        else replace(cue, state=EntityState.DELETED)
        if cue.cue_id in displaced_cues
        else cue
        for cue in document.cues
    )
    return replace(
        document,
        display_tokens=display_tokens,
        cues=cues,
        changes=(*document.changes, provenance),
    )


def _insert(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    payload = operation["payload"]
    cue = _find_cue(document, str(payload["cue_id"]))
    anchor = payload.get("after_token_id")
    position = 0 if anchor is None else cue.display_token_ids.index(str(anchor)) + 1
    raw = payload.get("token", {})
    if raw.get("source_token_ids"):
        raise DocumentOperationError("manual insert must not claim source lineage")
    provenance = _provenance(operation, "insert")
    text = str(raw.get("text", "")).strip()
    token = DisplayToken(
        token_id=stable_id(
            "dsp", {"operation_id": operation["operation_id"], "position": position}
        ),
        text=text,
        original_text=str(raw.get("original_text") or text),
        source_token_ids=(),
        provenance=provenance,
    )
    display_tokens = (*document.display_tokens, token)
    cue_ids = list(cue.display_token_ids)
    cue_ids.insert(position, token.token_id)
    updated_cue = replace(cue, display_token_ids=tuple(cue_ids))
    return replace(
        document,
        display_tokens=display_tokens,
        cues=_replace_cue(document.cues, cue.cue_id, updated_cue),
        groups=_mark_groups_dirty(document.groups, {cue.group_id}, "structure"),
        changes=(*document.changes, provenance),
    )


def _split_cue(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    payload = operation["payload"]
    cue = _find_cue(document, str(payload["cue_id"]))
    position = cue.display_token_ids.index(str(payload["after_token_id"]))
    if position >= len(cue.display_token_ids) - 1:
        raise DocumentOperationError("split boundary must be inside cue")
    left_ids = cue.display_token_ids[: position + 1]
    right_ids = cue.display_token_ids[position + 1 :]
    left_bounds = _source_bounds(document, left_ids)
    right_bounds = _source_bounds(document, right_ids)
    if left_bounds and right_bounds:
        boundary = (left_bounds[1] + right_bounds[0]) / 2
    else:
        boundary = (cue.start + cue.end) / 2
    boundary = min(max(boundary, cue.start + 0.001), cue.end - 0.001)
    provenance = _provenance(operation, "split_cue")
    copied_target = (
        _copy_translation_for_split(
            cue.target,
            cue_id=cue.cue_id,
            operation=operation,
            provenance=provenance,
        )
        if cue.target is not None
        else None
    )
    left = replace(
        cue,
        display_token_ids=left_ids,
        end=boundary,
        target=copied_target,
    )
    requested_right_id = str(payload.get("right_cue_id", "")).strip()
    if requested_right_id:
        if not requested_right_id.startswith("cue_") or any(
            item.cue_id == requested_right_id for item in document.cues
        ):
            raise DocumentOperationError("split right cue ID is invalid or already exists")
    else:
        requested_right_id = stable_id(
            "cue", {"operation_id": operation["operation_id"], "side": "right"}
        )
    right = DisplayCue(
        cue_id=requested_right_id,
        index=cue.index + 1,
        display_token_ids=right_ids,
        start=boundary,
        end=cue.end,
        target=copied_target,
        speaker=cue.speaker,
        state=cue.state,
        group_id=cue.group_id,
        mapping=cue.mapping,
    )
    cue_index = document.cues.index(cue)
    cues = _reindex((*document.cues[:cue_index], left, right, *document.cues[cue_index + 1 :]))
    return replace(
        document,
        cues=cues,
        groups=_mark_groups_dirty(document.groups, {cue.group_id}, "structure"),
        changes=(*document.changes, provenance),
    )


def _merge_cues(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    payload = operation["payload"]
    cue_ids = [str(value) for value in payload.get("cue_ids", [])]
    if len(cue_ids) != 2:
        raise DocumentOperationError("merge_cues needs two cue IDs")
    left = _find_cue(document, cue_ids[0])
    right = _find_cue(document, cue_ids[1])
    left_index = document.cues.index(left)
    if document.cues.index(right) != left_index + 1:
        raise DocumentOperationError("merge cues must be adjacent")
    provenance = _provenance(operation, "merge_cues")
    targets = [target for target in (left.target, right.target) if target is not None]
    target = None
    if len(targets) == 1:
        target = targets[0]
    elif len(targets) == 2 and _translations_are_unedited_copies(targets[0], targets[1]):
        copy_group = _translation_copy_group(targets[0])
        merged_provenance = replace(
            provenance,
            metadata={
                **dict(provenance.metadata),
                "translation_copy_group": copy_group,
            },
        )
        target = TranslationTrack(
            target_text=targets[0].target_text,
            original_text=targets[0].original_text,
            language=targets[0].language,
            provenance=merged_provenance,
        )
    elif targets:
        target = TranslationTrack(
            target_text=_join_translation_text(
                targets[0].target_text, targets[1].target_text
            ),
            original_text=_join_translation_text(
                targets[0].original_text or targets[0].target_text,
                targets[1].original_text or targets[1].target_text,
            ),
            language=targets[0].language,
            provenance=provenance,
        )
    groups = document.groups
    merged_group_id = left.group_id
    if groups and left.group_id != right.group_id:
        group_map = {group.group_id: group for group in groups}
        parents = [
            group_map[group_id]
            for group_id in (left.group_id, right.group_id)
            if group_id in group_map
        ]
        source_group_ids = tuple(
            dict.fromkeys(
                source_id
                for group in parents
                for source_id in (group.source_group_ids or (group.group_id,))
            )
        )
        execution_block_ids = tuple(
            dict.fromkeys(
                block_id for group in parents for block_id in group.execution_block_ids
            )
        )
        merged_group_id = stable_id(
            "grp",
            {
                "operation_id": operation["operation_id"],
                "parent_group_ids": [group.group_id for group in parents],
            },
        )
        groups = (
            *groups,
            SemanticGroup(
                group_id=merged_group_id,
                origin="merged",
                provenance=provenance,
                source_group_ids=source_group_ids,
                execution_block_ids=execution_block_ids,
                dirty_flags=("membership", "structure"),
            ),
        )
    elif groups:
        groups = _mark_groups_dirty(groups, {merged_group_id}, "structure")
    merged = replace(
        left,
        display_token_ids=(*left.display_token_ids, *right.display_token_ids),
        end=right.end,
        target=target,
        group_id=merged_group_id,
        speaker=left.speaker if left.speaker == right.speaker else None,
    )
    cues = _reindex((*document.cues[:left_index], merged, *document.cues[left_index + 2 :]))
    used_group_ids = {cue.group_id for cue in cues if cue.group_id}
    groups = tuple(group for group in groups if group.group_id in used_group_ids)
    return replace(
        document,
        cues=cues,
        groups=groups,
        changes=(*document.changes, provenance),
    )


def _set_cue_speaker(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    payload = operation["payload"]
    cue_id = str(payload.get("cue_id", ""))
    cue = _find_cue(document, cue_id)
    raw_speaker = payload.get("speaker")
    speaker = None if raw_speaker is None else str(raw_speaker).strip() or None
    if speaker is not None and speaker not in {f"speaker_{index}" for index in range(4)}:
        raise DocumentOperationError("speaker must be one of speaker_0..speaker_3 or null")
    provenance = _provenance(operation, "set_cue_speaker")
    return replace(
        document,
        cues=_replace_cue(document.cues, cue_id, replace(cue, speaker=speaker)),
        changes=(*document.changes, provenance),
    )


def _set_speaker_names(document: EditorDocument, operation: Mapping[str, Any]) -> EditorDocument:
    raw = dict(operation["payload"].get("speaker_names", {}))
    allowed = {f"speaker_{index}" for index in range(4)}
    if set(raw) - allowed:
        raise DocumentOperationError("speaker name keys must be speaker_0..speaker_3")
    names = tuple((key, str(raw.get(key, "")).strip()) for key in sorted(allowed))
    provenance = _provenance(operation, "set_speaker_names")
    return replace(
        document,
        properties=replace(document.properties, speaker_names=names),
        changes=(*document.changes, provenance),
    )


_APPLIERS = {
    "replace": _replace,
    "batch_replace": _batch_replace,
    "set_ai_calibration": _set_ai_calibration,
    "set_presentation": _set_presentation,
    "set_target": _set_target,
    "set_cue_time": _set_cue_time,
    "set_cue_times": _set_cue_times,
    "insert_cue": _insert_cue,
    "merge": _merge,
    "delete": _delete,
    "purge_cue": _purge_cue,
    "restore": _restore,
    "insert": _insert,
    "split_cue": _split_cue,
    "merge_cues": _merge_cues,
    "set_cue_speaker": _set_cue_speaker,
    "set_speaker_names": _set_speaker_names,
}


def apply_document_operation(
    document: EditorDocument, operation: Mapping[str, Any]
) -> EditorDocument:
    operation_type = str(operation.get("type", ""))
    if operation_type not in _APPLIERS:
        raise DocumentOperationError(f"unsupported operation: {operation_type}")
    if not str(operation.get("operation_id", "")).strip():
        raise DocumentOperationError("operation_id is required")
    ordered_document = canonicalize_document_cues(document)
    _assert_deleted_cues_frozen(
        ordered_document, operation_type, operation.get("payload", {})
    )
    result = _APPLIERS[operation_type](ordered_document, operation)
    return canonicalize_document_cues(result)
