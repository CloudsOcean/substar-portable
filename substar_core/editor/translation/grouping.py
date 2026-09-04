from __future__ import annotations

from typing import Any

from substar_core.domain import EditorDocument, EntityState


def translation_groups(
    document: EditorDocument,
    settings: dict[str, Any],
    selected: set[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str], dict[str, str]]:
    """Build ordered technical components without semantic-group boundaries."""

    groups_by_id = {group.group_id: group for group in document.groups}
    display_by_id = {token.token_id: token for token in document.display_tokens}
    rows: list[dict[str, Any]] = []
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
        execution_ids = list(group.execution_block_ids) if group else []
        rows.append({
            "cue": cue,
            "source_text": " ".join(token.text for token in tokens).strip(),
            # Compatibility read only: the legacy group supplies the accepted
            # scheduler block id, never a translation boundary.
            "execution_block_id": execution_ids[0] if execution_ids else "manual",
        })

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
    max_component_cues = max(1, int(settings.get("translation_execution_max_cues", 48)))
    components: list[list[dict[str, Any]]] = []
    for row in rows:
        if (
            not components
            or components[-1][-1]["execution_block_id"] != row["execution_block_id"]
            or len(components[-1]) >= max_component_cues
        ):
            components.append([])
        components[-1].append(row)
    for sequence, component_rows in enumerate(components, start=1):
        if selected is not None and not any(row["cue"].cue_id in selected for row in component_rows):
            continue
        payload_group_id = f"component_{sequence:04d}"
        cues: list[dict[str, Any]] = []
        for row in component_rows:
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
                "execution_block_id": str(component_rows[0]["execution_block_id"]),
                "mapping_policy": "translate_whole_then_allocate_to_cues",
                "cues": cues,
            }
        )
    return payloads, required, cue_to_group
