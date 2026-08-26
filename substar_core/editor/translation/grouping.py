from __future__ import annotations

from collections import defaultdict
from typing import Any

from substar_core.domain import EditorDocument, EntityState


def translation_groups(
    document: EditorDocument,
    settings: dict[str, Any],
    selected: set[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str], dict[str, str]]:
    """Build translation batches from canonical editor groups."""

    groups_by_id = {group.group_id: group for group in document.groups}
    display_by_id = {token.token_id: token for token in document.display_tokens}
    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cue in document.cues:
        if cue.state is not EntityState.ACTIVE:
            continue
        tokens = [
            display_by_id[token_id]
            for token_id in cue.display_token_ids
            if token_id in display_by_id
            and display_by_id[token_id].state is EntityState.ACTIVE
        ]
        group = groups_by_id.get(cue.group_id or "")
        group_id = group.group_id if group else f"manual:{cue.cue_id}"
        semantic_ids = list(group.source_group_ids) if group else [group_id]
        members[group_id].append(
            {
                "cue": cue,
                "source_text": " ".join(token.text for token in tokens).strip(),
                "semantic_group_ids": semantic_ids,
            }
        )

    payloads: list[dict[str, Any]] = []
    required: set[str] = set()
    cue_to_group: dict[str, str] = {}
    configured_target = str(settings.get("target_language_mode") or "auto_opposite")
    explicit_limits = {
        "zh-CN": ("chinese_hard_limit", "characters_excluding_spaces"),
        "en": ("english_hard_limit", "all_characters_including_spaces"),
        "ja": ("japanese_hard_limit", "characters_excluding_spaces"),
        "ko": ("korean_hard_limit", "characters_excluding_spaces"),
    }
    for sequence, (group_id, rows) in enumerate(members.items(), start=1):
        if selected is not None and not any(row["cue"].cue_id in selected for row in rows):
            continue
        rows.sort(key=lambda row: row["cue"].index)
        payload_group_id = f"component_{sequence:04d}"
        semantic_ids = list(
            dict.fromkeys(
                value for row in rows for value in row["semantic_group_ids"]
            )
        )
        cues: list[dict[str, Any]] = []
        for row in rows:
            cue = row["cue"]
            source_text = row["source_text"]
            required.add(cue.cue_id)
            cue_to_group[cue.cue_id] = payload_group_id
            current_target = cue.target.target_text if cue.target else ""
            if configured_target in explicit_limits:
                limit_key, count_rule = explicit_limits[configured_target]
            else:
                target_is_cjk = not any("\u3400" <= char <= "\u9fff" for char in source_text)
                limit_key, count_rule = (
                    ("chinese_hard_limit", "characters_excluding_spaces")
                    if target_is_cjk
                    else ("english_hard_limit", "all_characters_including_spaces")
                )
            hard_limit = int(settings[limit_key])
            cues.append(
                {
                    "cue_id": cue.cue_id,
                    "start": cue.start,
                    "end": cue.end,
                    "source_text": source_text,
                    "current_target": current_target,
                    "hard_limit": hard_limit,
                    "count_rule": count_rule,
                }
            )
        payloads.append(
            {
                "group_id": payload_group_id,
                "semantic_group_ids": semantic_ids,
                "mapping_policy": "translate_whole_then_allocate_to_cues",
                "cues": cues,
            }
        )
    return payloads, required, cue_to_group
