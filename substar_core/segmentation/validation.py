from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .material import AlignmentUnit, configured_length, display_normalize, validate_draft
from .chunking import _unit_original_ranges


AUTO_EDIT_CONFIDENCE = 0.98
SENTENCE_END_RE = re.compile(r"[?!。！？]|(?<!\d)\.(?=\s|$)")
ENGLISH_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?|\d+(?:\.\d+)?")
SHORT_STANDALONE = {
    "absolutely",
    "agreed",
    "amazing",
    "correct",
    "definitely",
    "exactly",
    "goodbye",
    "great",
    "hello",
    "hi",
    "incredible",
    "maybe",
    "no",
    "okay",
    "ok",
    "perfect",
    "please",
    "really",
    "right",
    "sorry",
    "sure",
    "thanks",
    "thank you",
    "true",
    "unbelievable",
    "what",
    "whoa",
    "wow",
    "yeah",
    "yes",
}
FUNCTION_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "as",
    "because",
    "but",
    "by",
    "can",
    "could",
    "for",
    "from",
    "had",
    "has",
    "have",
    "in",
    "into",
    "is",
    "my",
    "of",
    "on",
    "or",
    "our",
    "than",
    "that",
    "the",
    "their",
    "these",
    "this",
    "those",
    "to",
    "was",
    "were",
    "which",
    "who",
    "whose",
    "will",
    "with",
    "would",
    "your",
}
LEADING_DEPENDENCY_WORDS = {
    "and",
    "as",
    "because",
    "but",
    "for",
    "from",
    "if",
    "in",
    "into",
    "of",
    "on",
    "or",
    "than",
    "to",
    "when",
    "while",
    "with",
}
STRICT_DANGLING_ENDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "because",
    "but",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "its",
    "my",
    "of",
    "on",
    "or",
    "our",
    "the",
    "their",
    "than",
    "to",
    "with",
    "when",
    "while",
    "your",
    "i'm",
    "you're",
    "he's",
    "she's",
    "it's",
    "we're",
    "they're",
    "that's",
    "what's",
    "there's",
}
COMPLETE_AUXILIARY_PHRASES = {
    "here we are",
    "there you are",
    "there we are",
    "there it is",
    "i am",
    "i can",
    "i could",
    "i have",
    "i will",
    "they are",
    "they can",
    "they have",
    "we are",
    "we can",
    "we have",
    "you are",
    "you can",
    "you have",
}
COPULAR_ENDS = {
    "am",
    "are",
    "be",
    "been",
    "being",
    "is",
    "was",
    "were",
}
OBJECT_PRONOUNS = {
    "her",
    "him",
    "it",
    "me",
    "that",
    "them",
    "this",
    "us",
    "you",
}
DISCOURSE_RESTART_HEADS = {
    "and",
    "but",
    "he",
    "i",
    "no",
    "she",
    "so",
    "they",
    "we",
    "well",
    "yeah",
    "yes",
    "you",
}
COUNTRY_NAMES = {
    "america",
    "australia",
    "brazil",
    "britain",
    "canada",
    "china",
    "france",
    "germany",
    "india",
    "italy",
    "japan",
    "korea",
    "mexico",
    "russia",
    "singapore",
    "spain",
    "thailand",
    "uk",
    "usa",
}
ABBREVIATIONS = {
    "dr.",
    "e.g.",
    "etc.",
    "inc.",
    "jr.",
    "mr.",
    "mrs.",
    "ms.",
    "prof.",
    "sr.",
    "u.k.",
    "u.s.",
    "vs.",
}
NON_TERMINAL_PERIOD_WORDS = {
    "and",
    "because",
    "but",
    "or",
    "so",
    "uh",
    "um",
}
INDEPENDENT_CLAUSE_HEADS = {
    "he",
    "he's",
    "everyone",
    "i",
    "i'm",
    "it",
    "it's",
    "let's",
    "people",
    "she",
    "she's",
    "someone",
    "they",
    "they're",
    "there",
    "there's",
    "this",
    "we",
    "we're",
    "you",
    "you're",
}


@dataclass
class DirectPlanResult:
    draft: str
    issues: list[dict[str, Any]]
    review_notices: list[dict[str, Any]]
    validation: dict[str, Any]

    @property
    def valid(self) -> bool:
        return not self.issues and bool(self.validation.get("valid"))


def _group_id(number: int) -> str:
    return f"g{number:04d}"


def _issue(code: str, message: str, **context: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **context}


def structural_issues(
    plan: dict[str, Any],
    units: list[AlignmentUnit],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    groups = plan.get("groups")
    if not isinstance(groups, list) or not groups:
        return [_issue("missing_groups", "索引计划缺少非空 groups")]
    if not units:
        return [_issue("missing_alignment", "ALIGNMENT 为空")]

    expected_start = units[0].index
    final_end = units[-1].index
    valid_indexes = {unit.index for unit in units}

    for number, group in enumerate(groups, start=1):
        group_id = str(group.get("group_id", ""))
        start = group.get("alignment_start")
        end = group.get("alignment_end")
        if group_id != _group_id(number):
            issues.append(
                _issue(
                    "group_id_order",
                    "group_id 必须从 g0001 连续编号",
                    group_id=group_id,
                    expected=_group_id(number),
                )
            )
        if not isinstance(start, int) or not isinstance(end, int):
            issues.append(_issue("invalid_group_range", "意义组范围必须是整数", group_id=group_id))
            continue
        if start != expected_start:
            issues.append(
                _issue(
                    "group_coverage",
                    "意义组存在遗漏、重叠或调序",
                    group_id=group_id,
                    expected_start=expected_start,
                    actual_start=start,
                )
            )
        if start not in valid_indexes or end not in valid_indexes or end < start:
            issues.append(
                _issue(
                    "invalid_group_range",
                    "意义组范围不在当前 ALIGNMENT 内",
                    group_id=group_id,
                    alignment_start=start,
                    alignment_end=end,
                )
            )
            continue
        expected_start = end + 1

        breaks = group.get("line_breaks_after", [])
        if not isinstance(breaks, list) or breaks != sorted(set(breaks)):
            issues.append(
                _issue("invalid_break_order", "组内切点必须递增且唯一", group_id=group_id)
            )
            breaks = []
        for cut in breaks:
            if not isinstance(cut, int) or cut < start or cut >= end or cut not in valid_indexes:
                issues.append(
                    _issue(
                        "invalid_break",
                        "组内切点必须位于组内合法字词之后，且不能写组末",
                        group_id=group_id,
                        cut_after=cut,
                    )
                )

        protected = group.get("protected_spans", [])
        if not isinstance(protected, list):
            issues.append(_issue("invalid_protected_spans", "protected_spans 必须是数组", group_id=group_id))
            protected = []
        for span in protected:
            if not isinstance(span, dict):
                issues.append(
                    _issue(
                        "invalid_protected_span",
                        "保护范围条目必须是对象",
                        group_id=group_id,
                        span=span,
                    )
                )
                continue
            span_start = span.get("alignment_start")
            span_end = span.get("alignment_end")
            if (
                not isinstance(span_start, int)
                or not isinstance(span_end, int)
                or span_start < start
                or span_end > end
                or span_end < span_start
            ):
                issues.append(
                    _issue(
                        "invalid_protected_span",
                        "保护范围必须完整位于本组",
                        group_id=group_id,
                        span=span,
                    )
                )
                continue
            level = str(span.get("protection_level", "hard"))
            if level not in {"hard", "strong_soft", "outer_soft"}:
                issues.append(
                    _issue(
                        "invalid_protection_level",
                        "保护等级必须是 hard/strong_soft/outer_soft",
                        group_id=group_id,
                        span=span,
                    )
                )
                continue
            if level != "hard":
                continue
            for cut in breaks:
                if span_start <= cut < span_end:
                    issues.append(
                        _issue(
                            "protected_span_cut",
                            "切点落入 hard 保护范围内部",
                            group_id=group_id,
                            cut_after=cut,
                            span_start=span_start,
                            span_end=span_end,
                        )
                    )

        edit_indexes: set[int] = set()
        for kind in ("deletions", "corrections"):
            edits = group.get(kind, [])
            if not isinstance(edits, list):
                issues.append(_issue("invalid_edits", f"{kind} 必须是数组", group_id=group_id))
                continue
            for edit in edits:
                if not isinstance(edit, dict):
                    issues.append(
                        _issue(
                            "invalid_edits",
                            f"{kind} 的每个条目必须是对象",
                            group_id=group_id,
                            edit=edit,
                        )
                    )
                    continue
                index = edit.get("alignment_index")
                if not isinstance(index, int) or index < start or index > end or index not in valid_indexes:
                    issues.append(
                        _issue(
                            "invalid_edit_index",
                            "受控编辑 index 必须位于本组",
                            group_id=group_id,
                            edit=edit,
                        )
                    )
                elif index in edit_indexes:
                    issues.append(
                        _issue(
                            "duplicate_edit",
                            "同一字词不能同时或重复应用多个编辑",
                            group_id=group_id,
                            alignment_index=index,
                        )
                    )
                else:
                    edit_indexes.add(index)

        confidence = group.get("confidence")
        if not isinstance(confidence, (int, float)):
            issues.append(_issue("invalid_confidence", "confidence 必须是 0–1 数字", group_id=group_id))

    if expected_start != final_end + 1:
        issues.append(
            _issue(
                "incomplete_coverage",
                "索引计划没有覆盖 CORE ALIGNMENT 末尾",
                expected_final_end=final_end,
                covered_until=expected_start - 1,
            )
        )
    return issues


def review_notices(
    plan: dict[str, Any],
    *,
    review_confidence: float = 0.72,
) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    for group in plan.get("groups", []):
        group_id = str(group.get("group_id", ""))
        confidence = group.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < review_confidence:
            notices.append(
                _issue(
                    "low_confidence",
                    "意义组置信度较低，建议局部复议",
                    group_id=group_id,
                    confidence=confidence,
                )
            )
        if group.get("needs_review") is True:
            notices.append(
                _issue(
                    "model_requested_review",
                    "模型主动标记该意义组建议人工审阅",
                    group_id=group_id,
                )
            )
    return notices


def semantic_notices(
    master: str,
    units: list[AlignmentUnit],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    notices: list[dict[str, Any]] = []
    ranges = _unit_original_ranges(master, units)
    local_position = {unit.index: position for position, unit in enumerate(units)}
    unit_by_index = {unit.index: unit for unit in units}
    for group in plan.get("groups", []):
        start = group.get("alignment_start")
        end = group.get("alignment_end")
        if start not in local_position or end not in local_position:
            continue
        start_pos = local_position[start]
        end_pos = local_position[end]
        char_start = ranges[start_pos][0]
        char_end = ranges[end_pos + 1][0] if end_pos + 1 < len(ranges) else len(master)
        source = master[char_start:char_end].strip()
        sentence_ends = SENTENCE_END_RE.findall(source)
        if len(sentence_ends) > 1:
            notices.append(
                _issue(
                    "multi_sentence_group",
                    "一个意义组包含多个完整句末，默认应逐句拆组",
                    group_id=str(group.get("group_id", "")),
                    sentence_end_count=len(sentence_ends),
                )
            )
        cue_starts = [int(start)] + [
            int(value) + 1 for value in group.get("line_breaks_after", [])
        ]
        independent_centers: list[dict[str, Any]] = []
        for cue_start in cue_starts:
            unit = unit_by_index.get(cue_start)
            if unit is None:
                continue
            words = ENGLISH_WORD_RE.findall(str(unit.text))
            if not words:
                continue
            head = words[0].lower().replace("’", "'")
            if head in INDEPENDENT_CLAUSE_HEADS:
                independent_centers.append(
                    {"alignment_start": cue_start, "head": head}
                )
        cue_count = len(cue_starts)
        if len(independent_centers) >= 3 or (
            cue_count >= 6 and len(independent_centers) >= 2
        ):
            notices.append(
                _issue(
                    "multiple_independent_clause_centers",
                    "意义组疑似包含多个可独立闭合的谓词中心；应只按跨边界依赖证据决定是否保留同组",
                    group_id=str(group.get("group_id", "")),
                    cue_count=cue_count,
                    independent_centers=independent_centers,
                )
            )
    return notices


def _has_internal_sentence_boundary(raw: str) -> bool:
    stripped = raw.rstrip()
    for match in SENTENCE_END_RE.finditer(stripped):
        if not re.search(r"[A-Za-z0-9\u3400-\u9fff]", stripped[match.end() :]):
            continue
        prefix = stripped[: match.end()]
        token_match = re.search(r"([A-Za-z](?:[A-Za-z.]*)\.)$", prefix)
        token = token_match.group(1) if token_match else ""
        if token.rstrip(".").lower() in NON_TERMINAL_PERIOD_WORDS:
            continue
        if token.lower() in ABBREVIATIONS or (
            token.count(".") >= 2
            and token.replace(".", "").isalpha()
            and token.replace(".", "").isupper()
        ):
            continue
        return True
    return False


def _cue_quality_issues(
    master: str,
    units: list[AlignmentUnit],
    plan: dict[str, Any],
    *,
    baseline_punctuation: str = "preserve",
    raised_punctuation: str = "preserve",
    english_hard_limit: int = 55,
    english_count_spaces: bool = False,
    english_count_punctuation: bool = True,
    minimum_cue_duration_ms: int = 400,
) -> list[dict[str, Any]]:
    """Reject only objectively repairable micro-cues.

    The LLM remains responsible for linguistic boundaries. This gate catches
    cases where a very short fragment can be joined to an immediate neighbour
    inside the same meaning group without crossing the active hard cap.
    """

    issues: list[dict[str, Any]] = []
    ranges = _unit_original_ranges(master, units)
    local_position = {unit.index: position for position, unit in enumerate(units)}
    unit_position = {unit.index: ranges[position] for position, unit in enumerate(units)}
    unit_by_index = {unit.index: unit for unit in units}

    all_rows: list[dict[str, Any]] = []
    for group in plan.get("groups", []):
        group_id = str(group.get("group_id", ""))
        start = group.get("alignment_start")
        end = group.get("alignment_end")
        if start not in local_position or end not in local_position:
            continue
        cue_ends = [int(value) for value in group.get("line_breaks_after", [])] + [int(end)]
        cursor = int(start)
        cue_rows: list[dict[str, Any]] = []
        for cue_end in cue_ends:
            start_pos = local_position[cursor]
            end_pos = local_position[cue_end]
            char_start = ranges[start_pos][0]
            char_end = ranges[end_pos + 1][0] if end_pos + 1 < len(ranges) else len(master)
            raw = master[char_start:char_end].strip()
            if not raw:
                # Repeated punctuation-only ASR units (for example five
                # consecutive "...") can map to identical character offsets.
                # They are still source evidence and must not turn a valid P3
                # slot into an empty line or crash document construction.
                raw = " ".join(
                    str(units[position].text).strip()
                    for position in range(start_pos, end_pos + 1)
                    if str(units[position].text).strip()
                )
            edited = _apply_edits(
                raw,
                char_start=char_start,
                unit_positions=unit_position,
                deletions=[
                    edit
                    for edit in group.get("deletions", [])
                    if cursor <= int(edit.get("alignment_index", -1)) <= cue_end
                ],
                corrections=[
                    edit
                    for edit in group.get("corrections", [])
                    if cursor <= int(edit.get("alignment_index", -1)) <= cue_end
                ],
            )
            text = display_normalize(
                edited,
                baseline_punctuation=baseline_punctuation,
                raised_punctuation=raised_punctuation,
            )
            cue_rows.append(
                {
                    "group_id": group_id,
                    "alignment_start": cursor,
                    "alignment_end": cue_end,
                    "text": text,
                    "raw": raw,
                    "duration": max(
                        0.0,
                        float(unit_by_index[cue_end].end)
                        - float(unit_by_index[cursor].start),
                    ),
                }
            )
            cursor = cue_end + 1
        all_rows.extend(cue_rows)

        for position, row in enumerate(cue_rows):
            text = str(row["text"])
            words = ENGLISH_WORD_RE.findall(text)
            if not words or re.search(r"[\u3400-\u9fff]", text):
                continue
            normalized = " ".join(word.lower().replace("’", "'") for word in words)
            visible = configured_length(
                text,
                count_spaces=english_count_spaces,
                count_punctuation=english_count_punctuation,
            )
            short = len(words) <= 3 or visible <= 12
            if not short:
                continue
            if (
                normalized in SHORT_STANDALONE
                or text.rstrip().endswith(("?", "!", "？", "！"))
                or re.search(r"(?<!\d)[.。]\s*$", str(row["raw"]))
            ):
                continue
            if (
                len(words) == 1
                and words[0][:1].isupper()
                and words[0].lower() not in FUNCTION_WORDS
            ):
                continue

            merge_options: list[dict[str, Any]] = []
            for neighbour_position, direction in (
                (position - 1, "previous"),
                (position + 1, "next"),
            ):
                if neighbour_position < 0 or neighbour_position >= len(cue_rows):
                    continue
                neighbour = cue_rows[neighbour_position]
                combined = (
                    f"{neighbour['text']} {text}"
                    if direction == "previous"
                    else f"{text} {neighbour['text']}"
                )
                combined_visible = configured_length(
                    combined,
                    count_spaces=english_count_spaces,
                    count_punctuation=english_count_punctuation,
                )
                if combined_visible <= english_hard_limit:
                    merge_options.append(
                        {
                            "direction": direction,
                            "neighbour_text": neighbour["text"],
                            "combined_visible_characters": combined_visible,
                        }
                    )
            if not merge_options:
                continue

            first = words[0].lower()
            last = words[-1].lower()
            dangling = (
                first in LEADING_DEPENDENCY_WORDS
                or last in STRICT_DANGLING_ENDS
            )
            code = "dangling_function_phrase" if dangling else "mergeable_short_cue"
            message = (
                        f"功能短语首尾悬空且存在不超过{english_hard_limit}字符的相邻合并方案"
                        if dangling
                        else f"非独立短句存在不超过{english_hard_limit}字符的相邻合并方案"
            )
            issues.append(
                _issue(
                    code,
                    message,
                    group_id=group_id,
                    alignment_start=row["alignment_start"],
                    alignment_end=row["alignment_end"],
                    text=text,
                    word_count=len(words),
                    visible_characters=visible,
                    merge_options=merge_options,
                )
            )
            if float(row["duration"]) * 1000 < minimum_cue_duration_ms:
                issues.append(
                    _issue(
                        "sub_minimum_duration_cue",
                        f"非独立显示行不足{minimum_cue_duration_ms}ms 且可与相邻行合法合并",
                        group_id=group_id,
                        alignment_start=row["alignment_start"],
                        alignment_end=row["alignment_end"],
                        text=text,
                        duration_seconds=round(float(row["duration"]), 3),
                        merge_options=merge_options,
                    )
                )

    existing = {
        (str(item.get("group_id", "")), int(item.get("alignment_start", -1)))
        for item in issues
    }
    for row in all_rows:
        text = str(row["text"])
        words = ENGLISH_WORD_RE.findall(text)
        if (
            not words
            or re.search(r"[\u3400-\u9fff]", text)
            or text.rstrip().endswith(("?", "!", "？", "！"))
            or re.search(r"(?<!\d)[.。]\s*$", str(row["raw"]))
            # A technical chunk cannot repair a phrase whose continuation is
            # intentionally located in the next chunk. The global seam pass
            # receives both sides and the final whole-film validation applies
            # this same rule after reconciliation.
            or int(row["alignment_end"]) == int(units[-1].index)
        ):
            continue
        normalized = " ".join(word.lower().replace("’", "'") for word in words)
        if (
            words[-1].lower() in STRICT_DANGLING_ENDS
            and normalized not in COMPLETE_AUXILIARY_PHRASES
        ):
            key = (str(row["group_id"]), int(row["alignment_start"]))
            if key not in existing:
                issues.append(
                    _issue(
                        "dangling_line_end",
                        "显示行以未闭合功能词结束，必须移动切点以补全语法结构",
                        group_id=row["group_id"],
                        alignment_start=row["alignment_start"],
                        alignment_end=row["alignment_end"],
                        text=text,
                        final_word=words[-1],
                    )
                )
                existing.add(key)

    for row in all_rows:
        if _has_internal_sentence_boundary(str(row["raw"])):
            issues.append(
                _issue(
                    "crossed_sentence_boundary",
                    "一个显示行跨过明确的内部句界，必须在句界处分开",
                    group_id=row["group_id"],
                    alignment_start=row["alignment_start"],
                    alignment_end=row["alignment_end"],
                    text=row["text"],
                )
            )
        cut = int(row["alignment_end"])
        next_index = cut + 1
        current_unit = unit_by_index.get(cut)
        next_unit = unit_by_index.get(next_index)
        if current_unit is None or next_unit is None:
            continue
        bridge = master[unit_position[cut][1] : unit_position[next_index][0]]
        if (
            str(next_unit.text)[:1].isupper()
            and str(current_unit.text)[:1].isupper()
            and "," in bridge
        ):
            issues.append(
                _issue(
                    "suspected_named_entity_apposition_cut",
                    "切点疑似拆开了逗号连接的专名或同位结构，需要局部语义复议",
                    group_id=row["group_id"],
                    alignment_start=row["alignment_start"],
                    alignment_end=row["alignment_end"],
                    left=current_unit.text,
                    right=next_unit.text,
                )
            )

    for position, row in enumerate(all_rows):
        text = str(row["text"])
        words = ENGLISH_WORD_RE.findall(text)
        if (
            not words
            or len(words) > 3
            or re.search(r"[\u3400-\u9fff]", text)
            or text.rstrip().endswith(("?", "!", "？", "！"))
            or re.search(r"(?<!\d)[.。]\s*$", str(row["raw"]))
        ):
            continue
        first = words[0].lower()
        last = words[-1].lower()
        if (
            first not in LEADING_DEPENDENCY_WORDS
            and last not in STRICT_DANGLING_ENDS
        ):
            continue
        if (
            first in LEADING_DEPENDENCY_WORDS
            and position + 1 < len(all_rows)
            and all_rows[position + 1]["group_id"] == row["group_id"]
        ):
            continue
        if (
            last in STRICT_DANGLING_ENDS
            and position > 0
            and all_rows[position - 1]["group_id"] == row["group_id"]
        ):
            continue
        key = (str(row["group_id"]), int(row["alignment_start"]))
        if key in existing:
            continue
        merge_options: list[dict[str, Any]] = []
        for neighbour_position, direction in (
            (position - 1, "previous"),
            (position + 1, "next"),
        ):
            if neighbour_position < 0 or neighbour_position >= len(all_rows):
                continue
            neighbour = all_rows[neighbour_position]
            if neighbour["group_id"] == row["group_id"]:
                continue
            combined = (
                f"{neighbour['text']} {text}"
                if direction == "previous"
                else f"{text} {neighbour['text']}"
            )
            combined_visible = configured_length(
                combined,
                count_spaces=english_count_spaces,
                count_punctuation=english_count_punctuation,
            )
            if combined_visible <= english_hard_limit:
                merge_options.append(
                    {
                        "direction": direction,
                        "neighbour_group_id": neighbour["group_id"],
                        "neighbour_text": neighbour["text"],
                        "combined_visible_characters": combined_visible,
                    }
                )
        if merge_options:
            issues.append(
                _issue(
                    "cross_group_dangling_phrase",
                    f"短功能短语在意义组边界悬空，存在不超过{english_hard_limit}字符的跨组相邻合并方案",
                    group_id=row["group_id"],
                    alignment_start=row["alignment_start"],
                    alignment_end=row["alignment_end"],
                    text=text,
                    word_count=len(words),
                    merge_options=merge_options,
                )
            )

    # Boundary integrity is evaluated on both sides.  These are grammatical
    # relation classes, not project-specific phrase patches.  They deliberately
    # remain review notices: a hard character limit or a real speaker turn can
    # still justify the boundary, but P2/P3 must account for that exception.
    for position in range(len(all_rows) - 1):
        left = all_rows[position]
        right = all_rows[position + 1]
        left_words = [
            word.lower().replace("’", "'")
            for word in ENGLISH_WORD_RE.findall(str(left["text"]))
        ]
        right_words = [
            word.lower().replace("’", "'")
            for word in ENGLISH_WORD_RE.findall(str(right["text"]))
        ]
        if (
            not left_words
            or not right_words
            or re.search(r"[\u3400-\u9fff]", str(left["text"]) + str(right["text"]))
        ):
            continue
        left_normalized = " ".join(left_words)
        code = ""
        message = ""
        if (
            left_words[-1] in COPULAR_ENDS
            and left_normalized not in COMPLETE_AUXILIARY_PHRASES
        ):
            code = "copula_complement_cut"
            message = "切点位于系词与表语之间；除硬上限或真实话轮切换外应移动边界"
        elif (
            right_words[0] in OBJECT_PRONOUNS
            and (
                len(right_words) == 1
                or right_words[1] in DISCOURSE_RESTART_HEADS
            )
        ):
            code = "orphan_short_object"
            message = "下一 cue 以疑似被遗留的短宾语开头；应优先把宾语还给左侧谓语"
        elif (
            len(right_words) >= 2
            and right_words[0] in {"more", "less"}
            and right_words[1] == "than"
        ):
            code = "comparative_complement_cut"
            message = "比较补语被从谓语上剥离；除硬约束外应与谓语保留在同一 cue"
        if not code:
            continue
        issues.append(
            _issue(
                code,
                message,
                group_id=left["group_id"],
                right_group_id=right["group_id"],
                after_alignment=left["alignment_end"],
                left_text=left["text"],
                right_text=right["text"],
            )
        )
    return issues


def _apply_edits(
    text: str,
    *,
    char_start: int,
    unit_positions: dict[int, tuple[int, int]],
    deletions: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
) -> str:
    replacements: list[tuple[int, int, str]] = []
    for edit in deletions:
        if float(edit.get("confidence", 0)) < AUTO_EDIT_CONFIDENCE:
            continue
        start, end = unit_positions[int(edit["alignment_index"])]
        source = text[start - char_start : end - char_start]
        replacements.append((start - char_start, end - char_start, source + "//"))
    for edit in corrections:
        if float(edit.get("confidence", 0)) < AUTO_EDIT_CONFIDENCE:
            continue
        start, end = unit_positions[int(edit["alignment_index"])]
        source = text[start - char_start : end - char_start]
        proposal = str(edit["proposal"]).strip().replace(" ", "␠")
        replacements.append((start - char_start, end - char_start, f"{source}/{proposal}/"))
    result = text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def render_direct_draft(
    master: str,
    units: list[AlignmentUnit],
    plan: dict[str, Any],
    *,
    baseline_punctuation: str = "preserve",
    raised_punctuation: str = "preserve",
) -> str:
    ranges = _unit_original_ranges(master, units)
    unit_position = {unit.index: ranges[position] for position, unit in enumerate(units)}
    local_position = {unit.index: position for position, unit in enumerate(units)}
    blocks: list[str] = []

    for group in plan["groups"]:
        start_index = int(group["alignment_start"])
        end_index = int(group["alignment_end"])
        cut_indexes = [int(value) for value in group.get("line_breaks_after", [])]
        cue_ends = cut_indexes + [end_index]
        cue_start = start_index
        lines: list[str] = []
        for cue_end in cue_ends:
            start_pos = local_position[cue_start]
            end_pos = local_position[cue_end]
            char_start = ranges[start_pos][0]
            char_end = ranges[end_pos + 1][0] if end_pos + 1 < len(ranges) else len(master)
            raw = master[char_start:char_end].strip()
            if not raw:
                raw = " ".join(
                    str(units[position].text).strip()
                    for position in range(start_pos, end_pos + 1)
                    if str(units[position].text).strip()
                )
            edited = _apply_edits(
                raw,
                char_start=char_start,
                unit_positions=unit_position,
                deletions=[
                    edit
                    for edit in group.get("deletions", [])
                    if cue_start <= int(edit.get("alignment_index", -1)) <= cue_end
                ],
                corrections=[
                    edit
                    for edit in group.get("corrections", [])
                    if cue_start <= int(edit.get("alignment_index", -1)) <= cue_end
                ],
            )
            line = display_normalize(
                edited,
                baseline_punctuation=baseline_punctuation,
                raised_punctuation=raised_punctuation,
            )
            if not line:
                raise ValueError(f"{group['group_id']} 生成了空显示行")
            lines.append(line)
            cue_start = cue_end + 1
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip() + "\n"


def evaluate_direct_plan(
    master: str,
    units: list[AlignmentUnit],
    plan: dict[str, Any],
    *,
    review_confidence: float = 0.72,
    baseline_punctuation: str = "preserve",
    raised_punctuation: str = "preserve",
    source_language: str | None = None,
    english_hard_limit: int = 55,
    chinese_hard_limit: int = 24,
    mixed_hard_limit: int = 25,
    japanese_hard_limit: int = 25,
    korean_hard_limit: int = 32,
    english_count_spaces: bool = False,
    english_count_punctuation: bool = True,
    minimum_cue_duration_ms: int = 400,
) -> DirectPlanResult:
    issues = structural_issues(plan, units)
    quality_notices: list[dict[str, Any]] = []
    if not issues:
        quality_notices = _cue_quality_issues(
            master,
            units,
            plan,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
            english_hard_limit=english_hard_limit,
            english_count_spaces=english_count_spaces,
            english_count_punctuation=english_count_punctuation,
            minimum_cue_duration_ms=minimum_cue_duration_ms,
        )
    notices = review_notices(plan, review_confidence=review_confidence)
    notices.extend(semantic_notices(master, units, plan))
    notices.extend(quality_notices)
    draft = ""
    if not any(
        item["code"]
        in {
            "missing_groups",
            "invalid_group_range",
            "group_coverage",
            "incomplete_coverage",
            "invalid_break",
            "invalid_break_order",
        }
        for item in issues
    ):
        try:
            draft = render_direct_draft(
                master,
                units,
                plan,
                baseline_punctuation=baseline_punctuation,
                raised_punctuation=raised_punctuation,
            )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(_issue("draft_rebuild", f"程序无法从索引计划重建草案：{exc}"))
    validation = validate_draft(
        master,
        draft,
        baseline_punctuation=baseline_punctuation,
        raised_punctuation=raised_punctuation,
        source_language=source_language,
        english_hard_limit=english_hard_limit,
        chinese_hard_limit=chinese_hard_limit,
        mixed_hard_limit=mixed_hard_limit,
        japanese_hard_limit=japanese_hard_limit,
        korean_hard_limit=korean_hard_limit,
        english_count_spaces=english_count_spaces,
        english_count_punctuation=english_count_punctuation,
        minimum_cue_duration_ms=minimum_cue_duration_ms,
    ).to_dict() if draft else {
        "schema_version": "substar.segmentation.validation.v1",
        "valid": False,
        "errors": [{"code": "draft_missing", "message": "没有可校验的重建草案"}],
        "warnings": [],
        "stats": {},
    }
    for error in validation.get("errors", []):
        issues.append(
            _issue(
                "draft_" + str(error.get("code", "validation")),
                str(error.get("message", "草案硬校验失败")),
                detail=error,
            )
        )
    return DirectPlanResult(
        draft=draft,
        issues=issues,
        review_notices=notices,
        validation=validation,
    )


def merge_direct_plans(plans: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "schema_version": "substar.segmentation.plan.v1",
        "source_language": ",".join(
            dict.fromkeys(str(plan.get("source_language", "Auto")) for plan in plans)
        ),
        "groups": [],
        "coverage_check": {"complete": True, "ordered": True},
    }
    number = 0
    for plan in plans:
        for group in plan["groups"]:
            number += 1
            copied = copy.deepcopy(group)
            copied["group_id"] = _group_id(number)
            merged["groups"].append(copied)
    return merged
