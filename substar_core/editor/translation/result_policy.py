from __future__ import annotations

import hashlib
from typing import Any, Mapping


class TranslationResultError(RuntimeError):
    pass


def source_rows(document: Any) -> list[dict[str, str]]:
    tokens = {token.token_id: token for token in document.display_tokens}
    rows: list[dict[str, str]] = []
    for cue in document.cues:
        if cue.state.value != "active":
            continue
        source_text = " ".join(
            tokens[token_id].text
            for token_id in cue.display_token_ids
            if token_id in tokens and tokens[token_id].state.value == "active"
        ).strip()
        rows.append({
            "cue_id": cue.cue_id,
            "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        })
    return rows


def translated_text_by_source_cue(document: Any) -> dict[str, str]:
    parts: dict[str, list[str]] = {}
    for cue in document.cues:
        if cue.state.value != "active" or cue.target is None:
            continue
        target_text = cue.target.target_text.strip()
        if not target_text:
            continue
        mapping = cue.mapping if isinstance(cue.mapping, Mapping) else {}
        if mapping.get("translation_unresolved") is True:
            continue
        source_cue_ids = mapping.get("source_cue_ids", [])
        if not isinstance(source_cue_ids, (list, tuple)) or not source_cue_ids:
            raise TranslationResultError(
                f"translated Cue {cue.cue_id} is missing source-Cue lineage"
            )
        for source_cue_id in source_cue_ids:
            bucket = parts.setdefault(str(source_cue_id), [])
            if not bucket or bucket[-1] != target_text:
                bucket.append(target_text)
    return {cue_id: "\n".join(values) for cue_id, values in parts.items() if values}


def source_hashes_by_lineage(document: Any) -> dict[str, set[str]]:
    active_cues = (cue for cue in document.cues if cue.state.value == "active")
    current: dict[str, set[str]] = {}
    for row, cue in zip(source_rows(document), active_cues):
        mapping = cue.mapping if isinstance(cue.mapping, Mapping) else {}
        source_cue_ids = mapping.get("source_cue_ids", [])
        lineage = (
            [str(value) for value in source_cue_ids]
            if isinstance(source_cue_ids, (list, tuple)) and source_cue_ids
            else [str(row["cue_id"])]
        )
        for source_cue_id in lineage:
            current.setdefault(source_cue_id, set()).add(row["source_hash"])
    return current


def accepted_translation_rows(
    source: list[dict[str, str]], translated_text: Mapping[str, str]
) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    problems: list[str] = []
    for row in source:
        target_text = str(translated_text.get(row["cue_id"], "")).strip()
        if target_text:
            result.append({
                **row,
                "target_text": target_text,
                "translation_status": "translated",
                "issue_code": None,
                "editable": True,
            })
        else:
            problems.append(row["cue_id"])
            result.append({
                **row,
                "target_text": "",
                "translation_status": "manual_required",
                "issue_code": "translation_unresolved",
                "editable": True,
            })
    return result, problems


def translation_problem_cue_ids(document: Any) -> list[str]:
    for change in reversed(document.changes):
        if change.operation != "contextual_translation":
            continue
        raw_ids = change.metadata.get("translation_problem_cue_ids", [])
        if not isinstance(raw_ids, (list, tuple)):
            return []
        return list(dict.fromkeys(str(cue_id) for cue_id in raw_ids if str(cue_id)))
    return []
