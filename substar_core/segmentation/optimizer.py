from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .material import (
    AlignmentUnit,
    configured_length,
    display_normalize,
    han_count,
    is_english_dominant,
    visible_length,
)
from .chunking import _unit_original_ranges
from .validation import (
    COUNTRY_NAMES,
    ENGLISH_WORD_RE,
    INDEPENDENT_CLAUSE_HEADS,
    LEADING_DEPENDENCY_WORDS,
    SHORT_STANDALONE,
    STRICT_DANGLING_ENDS,
    _apply_edits,
)


TERMINAL_RE = re.compile(r"[?!。！？]|(?<!\d)\.")
CLAUSE_PUNCTUATION_RE = re.compile(r"[,，、;；:：]")
NEW_SENTENCE_HEADS = {
    "he's",
    "i'm",
    "it's",
    "she's",
    "that's",
    "they're",
    "we're",
    "what's",
    "you're",
}

# These are structural glue, not arbitrary "bad line-end words".  A cut is
# forbidden only when it falls *inside* one of these local expressions.  This
# keeps the rule deterministic and contextual while still allowing, for
# example, a new line to begin with "the" when that gives a better subtitle.
PROTECTED_EXPRESSIONS = {
    ("a", "lot", "of"),
    ("according", "to"),
    ("as", "well", "as"),
    ("because", "of"),
    ("but", "also"),
    ("due", "to"),
    ("each", "other"),
    ("in", "front", "of"),
    ("in", "terms", "of"),
    ("instead", "of"),
    ("kind", "of"),
    ("more", "and", "more"),
    ("not", "only"),
    ("one", "of"),
    ("out", "of"),
    ("sort", "of"),
    ("such", "as"),
}

# Particles in this set very commonly complete an immediately preceding
# English verb ("miss out", "set up", "turn off").  Unlike ordinary
# prepositions such as "in" or "with", they are unsafe line heads when there
# is no real acoustic/sentence boundary.
PHRASAL_PARTICLES = {
    "away",
    "back",
    "down",
    "off",
    "out",
    "through",
    "up",
}

# Function-word dependence is a property of a proposed boundary, not of the
# word in isolation.  A genuine sentence end or an acoustic break can license
# the same surface word, while a fluent continuation cannot.
DEPENDENT_MICRO_WORDS = STRICT_DANGLING_ENDS | {
    "although",
    "before",
    "despite",
    "during",
    "except",
    "since",
    "unless",
    "until",
    "whereas",
}


@dataclass
class OptimizationResult:
    plan: dict[str, Any]
    actions: list[dict[str, Any]]


def compact_mergeable_line_breaks(
    master: str,
    units: list[AlignmentUnit],
    plan: dict[str, Any],
    *,
    baseline_punctuation: str = "preserve",
    raised_punctuation: str = "preserve",
    english_hard_limit: int = 55,
    chinese_hard_limit: int = 24,
    english_count_spaces: bool = False,
    english_count_punctuation: bool = True,
    maximum_merge_pause_seconds: float = 0.3,
) -> OptimizationResult:
    """Remove provably unnecessary display cuts without moving boundaries.

    This conservative pass never adds a cue, changes a meaning-group boundary,
    or crosses an explicit sentence end / strong pause.  It exists separately
    from the full dynamic-programming optimizer so a semantically valid model
    plan can be made denser without reopening all of its boundary decisions.
    """

    compacted = copy.deepcopy(plan)
    raw_ranges = _unit_original_ranges(master, units)
    ranges = {unit.index: raw_ranges[position] for position, unit in enumerate(units)}
    unit_by_index = {unit.index: unit for unit in units}
    actions: list[dict[str, Any]] = []
    for group in compacted.get("groups", []):
        start = int(group["alignment_start"])
        end = int(group["alignment_end"])
        cuts = sorted(
            {
                int(value)
                for value in group.get("line_breaks_after", [])
                if start <= int(value) < end
            }
        )
        before = list(cuts)
        while cuts:
            candidates: list[tuple[float, int, int]] = []
            for position, cut in enumerate(cuts):
                following = unit_by_index.get(cut + 1)
                current = unit_by_index.get(cut)
                if current is None or following is None:
                    continue
                bridge = master[ranges[cut][1] : ranges[cut + 1][0]]
                pause = max(0.0, float(following.start) - float(current.end))
                if TERMINAL_RE.search(bridge) or pause >= maximum_merge_pause_seconds:
                    continue
                segment_start = start if position == 0 else cuts[position - 1] + 1
                segment_end = end if position == len(cuts) - 1 else cuts[position + 1]
                combined = _segment_text(
                    master=master,
                    start=segment_start,
                    end=segment_end,
                    ranges=ranges,
                    group=group,
                    baseline_punctuation=baseline_punctuation,
                    raised_punctuation=raised_punctuation,
                )
                if not _hard_length_ok(
                    combined,
                    english_hard_limit=english_hard_limit,
                    chinese_hard_limit=chinese_hard_limit,
                    english_count_spaces=english_count_spaces,
                    english_count_punctuation=english_count_punctuation,
                ):
                    continue
                if is_english_dominant(combined):
                    length = configured_length(
                        combined,
                        count_spaces=english_count_spaces,
                        count_punctuation=english_count_punctuation,
                    )
                    target = min(40, english_hard_limit)
                else:
                    length = configured_length(
                        combined,
                        count_spaces=english_count_spaces,
                        count_punctuation=english_count_punctuation,
                    )
                    target = min(16, chinese_hard_limit)
                candidates.append((abs(length - target), cut, position))
            if not candidates:
                break
            _, selected_cut, _ = min(candidates)
            cuts.remove(selected_cut)
        if cuts != before:
            group["line_breaks_after"] = cuts
            actions.append(
                {
                    "type": "compact_mergeable_line_breaks",
                    "group_id": group.get("group_id"),
                    "before": before,
                    "after": cuts,
                    "removed": [value for value in before if value not in cuts],
                }
            )
    return OptimizationResult(plan=compacted, actions=actions)


def build_deterministic_fallback_plan(
    master: str,
    units: list[AlignmentUnit],
) -> dict[str, Any]:
    """Build a conservative, fully covered plan when the LLM is unavailable.

    This is intentionally a delivery fallback, not a claim of semantic
    perfection.  It prefers explicit sentence ends and pauses, then caps a
    meaning group at roughly two display lines.  ``optimize_direct_plan``
    performs the exact line fitting afterwards.
    """
    if not units:
        return {
            "schema_version": "substar.segmentation.plan.v1",
            "source_language": "Auto",
            "groups": [],
            "coverage_check": {"complete": True, "ordered": True},
        }
    raw_ranges = _unit_original_ranges(master, units)
    ranges = {unit.index: raw_ranges[position] for position, unit in enumerate(units)}
    groups: list[dict[str, Any]] = []
    start_position = 0
    for position, unit in enumerate(units):
        is_last = position == len(units) - 1
        next_unit = None if is_last else units[position + 1]
        bridge = (
            ""
            if is_last
            else master[ranges[unit.index][1] : ranges[next_unit.index][0]]
        )
        gap = (
            0.0
            if next_unit is None
            else max(0.0, float(next_unit.start) - float(unit.end))
        )
        start_unit = units[start_position]
        group_text = _segment_text(
            master=master,
            start=start_unit.index,
            end=unit.index,
            ranges=ranges,
            group={"deletions": [], "corrections": []},
            baseline_punctuation="preserve",
            raised_punctuation="preserve",
        )
        if is_english_dominant(group_text):
            group_full = visible_length(group_text) >= 88
        else:
            group_full = han_count(group_text) >= 42
        boundary = (
            is_last
            or bool(TERMINAL_RE.search(bridge))
            or gap >= 0.65
            or group_full
        )
        if not boundary:
            continue
        number = len(groups) + 1
        groups.append(
            {
                "group_id": f"g{number:04d}",
                "alignment_start": start_unit.index,
                "alignment_end": unit.index,
                "line_breaks_after": [],
                "confidence": 0.5,
                "needs_review": True,
                "protected_spans": [],
                "deletions": [],
                "corrections": [],
                "reason": "LLM不可用时的确定性交付兜底",
            }
        )
        start_position = position + 1
    return {
        "schema_version": "substar.segmentation.plan.v1",
        "source_language": "Auto",
        "groups": groups,
        "coverage_check": {"complete": True, "ordered": True},
    }


def _group_edits(group: dict[str, Any], key: str, start: int, end: int) -> list[dict[str, Any]]:
    return [
        item
        for item in group.get(key, [])
        if start <= int(item.get("alignment_index", -1)) <= end
    ]


def _segment_text(
    *,
    master: str,
    start: int,
    end: int,
    ranges: dict[int, tuple[int, int]],
    group: dict[str, Any],
    baseline_punctuation: str,
    raised_punctuation: str,
) -> str:
    char_start = ranges[start][0]
    next_index = end + 1
    char_end = ranges[next_index][0] if next_index in ranges else len(master)
    raw = master[char_start:char_end].strip()
    edited = _apply_edits(
        raw,
        char_start=char_start,
        unit_positions=ranges,
        deletions=_group_edits(group, "deletions", start, end),
        corrections=_group_edits(group, "corrections", start, end),
    )
    return display_normalize(
        edited,
        baseline_punctuation=baseline_punctuation,
        raised_punctuation=raised_punctuation,
    )


def _hard_length_ok(
    text: str,
    *,
    english_hard_limit: int,
    chinese_hard_limit: int,
    english_count_spaces: bool,
    english_count_punctuation: bool,
) -> bool:
    if not text:
        return False
    if is_english_dominant(text):
        return configured_length(
            text,
            count_spaces=english_count_spaces,
            count_punctuation=english_count_punctuation,
        ) <= english_hard_limit
    return configured_length(
        text,
        count_spaces=english_count_spaces,
        count_punctuation=english_count_punctuation,
    ) <= chinese_hard_limit


def _cut_inside_span(group: dict[str, Any], cut: int) -> bool:
    return any(
        int(span.get("alignment_start", -1)) <= cut
        < int(span.get("alignment_end", -1))
        for span in group.get("protected_spans", [])
        if str(span.get("protection_level", "hard")) == "hard"
    )


def _geo_apposition_cut(
    master: str,
    cut: int,
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
) -> bool:
    current = unit_by_index.get(cut)
    following = unit_by_index.get(cut + 1)
    if current is None or following is None:
        return False
    if str(following.text).lower() not in COUNTRY_NAMES:
        return False
    bridge = master[ranges[cut][1] : ranges[cut + 1][0]]
    return str(current.text)[:1].isupper() and "," in bridge


def _shared_envelope_cut(
    cut: int,
    unit_by_index: dict[int, AlignmentUnit],
) -> bool:
    current = unit_by_index.get(cut)
    following = unit_by_index.get(cut + 1)
    return bool(
        current
        and following
        and current.start == following.start
        and current.end == following.end
    )


def _normalized_unit_word(unit: AlignmentUnit | None) -> str:
    if unit is None:
        return ""
    words = ENGLISH_WORD_RE.findall(str(unit.text))
    if len(words) != 1:
        return ""
    return words[0].lower().replace("’", "'")


def _lexically_protected_cut(
    *,
    master: str,
    cut: int,
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
) -> bool:
    current = unit_by_index.get(cut)
    following = unit_by_index.get(cut + 1)
    if current is None or following is None:
        return False
    bridge = master[ranges[cut][1] : ranges[cut + 1][0]]
    gap = max(0.0, float(following.start) - float(current.end))
    # A trailing hyphen is an explicit orthographic continuation marker
    # (for example, a tokenizer may expose "self-" and "serve" separately).
    # This is structural evidence, not a vocabulary-specific exception.
    if str(current.text).rstrip().endswith(("-", "‐", "‑", "‒")):
        return True
    # A genuine sentence/acoustic boundary overrides lexical heuristics.
    if TERMINAL_RE.search(bridge) or gap >= 0.4:
        return False

    right = _normalized_unit_word(following)
    if right in PHRASAL_PARTICLES:
        return True

    # Test every 2/3-word window that crosses the proposed boundary.
    for expression in PROTECTED_EXPRESSIONS:
        size = len(expression)
        for left_size in range(1, size):
            indexes = list(
                range(cut - left_size + 1, cut - left_size + 1 + size)
            )
            words = tuple(
                _normalized_unit_word(unit_by_index.get(index)) for index in indexes
            )
            if words == expression:
                return True
    return False


def _syntactically_incomplete_cut(
    *,
    master: str,
    cut: int,
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
) -> bool:
    """Return whether a display cut strands a function word on its left.

    This is intentionally local and evidence based.  Explicit terminal
    punctuation or a strong acoustic break licenses the boundary; otherwise
    determiners, conjunctions and other governors must keep their complement.
    """

    current = unit_by_index.get(cut)
    following = unit_by_index.get(cut + 1)
    if current is None or following is None:
        return False
    bridge = master[ranges[cut][1] : ranges[cut + 1][0]]
    if TERMINAL_RE.search(bridge):
        return False
    return _normalized_unit_word(current) in DEPENDENT_MICRO_WORDS


def _boundary_cost(
    *,
    master: str,
    cut: int,
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
    left_text: str,
    existing_cuts: set[int],
) -> float:
    current = unit_by_index[cut]
    following = unit_by_index.get(cut + 1)
    if following is None:
        return 0.0
    bridge = master[ranges[cut][1] : ranges[cut + 1][0]]
    cost = -2.5 if cut in existing_cuts else 0.0
    if TERMINAL_RE.search(bridge):
        cost -= 9.0
    elif CLAUSE_PUNCTUATION_RE.search(bridge):
        cost -= 4.0
    gap = max(0.0, float(following.start) - float(current.end))
    if gap >= 0.4:
        cost -= 8.0
    elif gap >= 0.2:
        cost -= 4.0
    elif gap >= 0.08:
        cost -= 1.5
    words = ENGLISH_WORD_RE.findall(left_text)
    if words:
        last = words[-1].lower().replace("’", "'")
        if last in STRICT_DANGLING_ENDS:
            cost += 18.0
    return cost


def _segment_cost(
    text: str,
    *,
    duration: float,
    english_count_spaces: bool,
    english_count_punctuation: bool,
    minimum_cue_duration_ms: int,
) -> float:
    if is_english_dominant(text):
        length = configured_length(
            text,
            count_spaces=english_count_spaces,
            count_punctuation=english_count_punctuation,
        )
        words = ENGLISH_WORD_RE.findall(text)
        cost = 0.0
        if length < 16 and len(words) > 1:
            cost += (16 - length) * 1.2
        if len(words) == 1 and not text.rstrip().endswith(("?", "!", "？", "！")):
            cost += 18.0
        minimum_duration = minimum_cue_duration_ms / 1000.0
        if duration < minimum_duration and len(words) > 1:
            cost += 40.0
        elif duration < 0.8 and len(words) > 1:
            cost += 14.0
        return cost
    length = han_count(text)
    cost = 0.0
    if duration < minimum_cue_duration_ms / 1000.0 and length > 2:
        cost += 40.0
    return cost


def _optimize_group_breaks(
    *,
    master: str,
    group: dict[str, Any],
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
    baseline_punctuation: str,
    raised_punctuation: str,
    english_hard_limit: int,
    chinese_hard_limit: int,
    english_count_spaces: bool,
    english_count_punctuation: bool,
    minimum_cue_duration_ms: int,
    maximum_cues: int | None = None,
    allow_incomplete_cuts: bool = False,
) -> list[int] | None:
    start = int(group["alignment_start"])
    end = int(group["alignment_end"])
    existing = {int(value) for value in group.get("line_breaks_after", [])}
    # Keep cue count in the state.  A one-dimensional DP can discard a
    # slightly more expensive partial path that uses fewer cues, even though
    # that is the only path able to finish under ``maximum_cues``.
    best: dict[tuple[int, int], tuple[float, list[int]]] = {
        (start, 0): (0.0, [])
    }
    for cursor in range(start, end + 1):
        states = [
            (cue_count, value)
            for (state_cursor, cue_count), value in best.items()
            if state_cursor == cursor
        ]
        for used_cues, (base_cost, chosen) in states:
            if maximum_cues is not None and used_cues >= maximum_cues:
                continue
            for segment_end in range(cursor, end + 1):
                if segment_end < end and (
                    _cut_inside_span(group, segment_end)
                    or _shared_envelope_cut(segment_end, unit_by_index)
                    or _geo_apposition_cut(master, segment_end, ranges, unit_by_index)
                    or (
                        not allow_incomplete_cuts
                        and _syntactically_incomplete_cut(
                            master=master,
                            cut=segment_end,
                            ranges=ranges,
                            unit_by_index=unit_by_index,
                        )
                    )
                    or _lexically_protected_cut(
                        master=master,
                        cut=segment_end,
                        ranges=ranges,
                        unit_by_index=unit_by_index,
                    )
                ):
                    continue
                text = _segment_text(
                    master=master,
                    start=cursor,
                    end=segment_end,
                    ranges=ranges,
                    group=group,
                    baseline_punctuation=baseline_punctuation,
                    raised_punctuation=raised_punctuation,
                )
                if not _hard_length_ok(
                    text,
                    english_hard_limit=english_hard_limit,
                    chinese_hard_limit=chinese_hard_limit,
                    english_count_spaces=english_count_spaces,
                    english_count_punctuation=english_count_punctuation,
                ):
                    continue
                next_cursor = segment_end + 1
                duration = max(
                    0.0,
                    float(unit_by_index[segment_end].end)
                    - float(unit_by_index[cursor].start),
                )
                # Cue count is a weak compactness preference, never a proxy
                # for meaning-group size or visual length equality.
                cost = base_cost + 1.5 + _segment_cost(
                    text,
                    duration=duration,
                    english_count_spaces=english_count_spaces,
                    english_count_punctuation=english_count_punctuation,
                    minimum_cue_duration_ms=minimum_cue_duration_ms,
                )
                if segment_end < end:
                    cost += _boundary_cost(
                        master=master,
                        cut=segment_end,
                        ranges=ranges,
                        unit_by_index=unit_by_index,
                        left_text=text,
                        existing_cuts=existing,
                    )
                next_used_cues = used_cues + 1
                if maximum_cues is not None and next_used_cues > maximum_cues:
                    continue
                proposal = chosen + ([segment_end] if segment_end < end else [])
                state = (next_cursor, next_used_cues)
                previous = best.get(state)
                if previous is None or cost < previous[0]:
                    best[state] = (cost, proposal)
    finals = [
        (cost, proposal)
        for (cursor, cue_count), (cost, proposal) in best.items()
        if cursor == end + 1
        and (maximum_cues is None or cue_count <= maximum_cues)
    ]
    return min(finals, default=(0.0, None), key=lambda item: item[0])[1]


def _current_group_needs_optimization(
    *,
    master: str,
    group: dict[str, Any],
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
    baseline_punctuation: str,
    raised_punctuation: str,
    english_hard_limit: int,
    chinese_hard_limit: int,
    english_count_spaces: bool,
    english_count_punctuation: bool,
) -> bool:
    start = int(group["alignment_start"])
    end = int(group["alignment_end"])
    cuts = [int(value) for value in group.get("line_breaks_after", [])]
    cursor = start
    for segment_end in cuts + [end]:
        text = _segment_text(
            master=master,
            start=cursor,
            end=segment_end,
            ranges=ranges,
            group=group,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
        )
        if not _hard_length_ok(
            text,
            english_hard_limit=english_hard_limit,
            chinese_hard_limit=chinese_hard_limit,
            english_count_spaces=english_count_spaces,
            english_count_punctuation=english_count_punctuation,
        ):
            return True
        if segment_end < end and _cut_inside_span(group, segment_end):
            return True
        if segment_end < end and _syntactically_incomplete_cut(
            master=master,
            cut=segment_end,
            ranges=ranges,
            unit_by_index=unit_by_index,
        ):
            return True
        cursor = segment_end + 1
    return False


def _group_line_break_risk_count(
    *,
    master: str,
    group: dict[str, Any],
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
    baseline_punctuation: str,
    raised_punctuation: str,
) -> int:
    """Count high-confidence structural risks at existing display cuts.

    This deliberately uses only local, measurable evidence.  It is a trigger
    and monotonic acceptance gate for joint reflow, not a claim to understand
    every possible semantic weakness.
    """

    start = int(group["alignment_start"])
    end = int(group["alignment_end"])
    cursor = start
    risks = 0
    for segment_end in [
        int(value) for value in group.get("line_breaks_after", [])
    ] + [end]:
        if segment_end >= end:
            break
        text = _segment_text(
            master=master,
            start=cursor,
            end=segment_end,
            ranges=ranges,
            group=group,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
        )
        words = ENGLISH_WORD_RE.findall(text)
        bridge = master[ranges[segment_end][1] : ranges[segment_end + 1][0]]
        if (
            words
            and words[-1].lower().replace("’", "'") in STRICT_DANGLING_ENDS
            and not TERMINAL_RE.search(bridge)
        ):
            risks += 1
        if _lexically_protected_cut(
            master=master,
            cut=segment_end,
            ranges=ranges,
            unit_by_index=unit_by_index,
        ):
            risks += 1
        cursor = segment_end + 1
    return risks


def _merge_dependent_micro_groups(
    *,
    master: str,
    groups: list[dict[str, Any]],
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
    baseline_punctuation: str,
    raised_punctuation: str,
) -> list[dict[str, Any]]:
    """Absorb non-independent micro groups into their fluent continuation.

    The rule is deliberately class based: it applies to short groups made
    entirely from dependent function words, never to named entities, answers,
    exclamations or arbitrary short content.  Cue layout is recalculated later.
    """

    actions: list[dict[str, Any]] = []
    position = 0
    while position < len(groups):
        group = groups[position]
        start = int(group["alignment_start"])
        end = int(group["alignment_end"])
        text = _segment_text(
            master=master,
            start=start,
            end=end,
            ranges=ranges,
            group=group,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
        )
        words = [
            word.lower().replace("’", "'")
            for word in ENGLISH_WORD_RE.findall(text)
        ]
        dependent = bool(words) and len(words) <= 3 and (
            all(word in DEPENDENT_MICRO_WORDS for word in words)
            or words[0] in LEADING_DEPENDENCY_WORDS
        )
        if (
            not dependent
            or TERMINAL_RE.search(text.rstrip())
            or position + 1 >= len(groups)
        ):
            position += 1
            continue
        next_group = groups[position + 1]
        next_start = int(next_group["alignment_start"])
        bridge = master[ranges[end][1] : ranges[next_start][0]]
        gap = max(
            0.0,
            float(unit_by_index[next_start].start)
            - float(unit_by_index[end].end),
        )
        # A word-level timestamp gap is not enough to make a conjunction,
        # determiner or preposition linguistically complete.  Forced-alignment
        # word ends are often short; only explicit terminal evidence licenses
        # leaving the micro group independent.
        if TERMINAL_RE.search(bridge):
            position += 1
            continue

        old_id = str(group.get("group_id", ""))
        next_group["alignment_start"] = start
        next_group["line_breaks_after"] = sorted(
            {
                int(value)
                for value in (
                    list(group.get("line_breaks_after", []))
                    + list(next_group.get("line_breaks_after", []))
                )
                if start <= int(value) < int(next_group["alignment_end"])
            }
        )
        for key in ("deletions", "corrections", "protected_spans"):
            next_group[key] = list(group.get(key, [])) + list(
                next_group.get(key, [])
            )
        next_group["needs_review"] = bool(
            group.get("needs_review") or next_group.get("needs_review")
        )
        next_group["reason"] = (
            f"{next_group.get('reason', '')} "
            "程序将非独立功能词微组并入其连续补足成分。"
        ).strip()
        groups.pop(position)
        actions.append(
            {
                "type": "merge_dependent_micro_group",
                "from_group": old_id,
                "to_group": next_group.get("group_id"),
                "alignment_start": start,
                "alignment_end": end,
                "text": text,
                "continuation_gap_seconds": round(gap, 3),
            }
        )
    for number, group in enumerate(groups, start=1):
        group["group_id"] = f"g{number:04d}"
    return actions


def _move_trailing_incomplete_clause_to_next_group(
    *,
    master: str,
    groups: list[dict[str, Any]],
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for position in range(len(groups) - 1):
        left = groups[position]
        right = groups[position + 1]
        left_start = int(left["alignment_start"])
        left_end = int(left["alignment_end"])
        if left_end <= left_start:
            continue
        for cut in range(left_end - 1, left_start - 1, -1):
            bridge = master[ranges[cut][1] : ranges[cut + 1][0]]
            if not TERMINAL_RE.search(bridge):
                continue
            tail_indexes = list(range(cut + 1, left_end + 1))
            if len(tail_indexes) > 2:
                break
            tail = " ".join(str(unit_by_index[index].text) for index in tail_indexes)
            words = ENGLISH_WORD_RE.findall(tail)
            normalized = " ".join(word.lower().replace("’", "'") for word in words)
            if not words:
                break
            last = words[-1].lower().replace("’", "'")
            if (
                normalized not in NEW_SENTENCE_HEADS
                and last not in STRICT_DANGLING_ENDS
            ):
                break
            next_start = unit_by_index.get(left_end + 1)
            if next_start is None:
                break
            continuation_gap = max(
                0.0,
                float(next_start.start) - float(unit_by_index[left_end].end),
            )
            continuation_bridge = master[
                ranges[left_end][1] : ranges[left_end + 1][0]
            ]
            if TERMINAL_RE.search(continuation_bridge) or continuation_gap > 0.3:
                break
            old_end = left_end
            left["alignment_end"] = cut
            right["alignment_start"] = cut + 1
            moved_cuts = [
                value for value in left.get("line_breaks_after", []) if int(value) > cut
            ]
            left["line_breaks_after"] = [
                value for value in left.get("line_breaks_after", []) if int(value) < cut
            ]
            right["line_breaks_after"] = sorted(
                set(moved_cuts + [int(value) for value in right.get("line_breaks_after", [])])
            )
            for key in ("deletions", "corrections"):
                moved = [
                    item
                    for item in left.get(key, [])
                    if int(item.get("alignment_index", -1)) > cut
                ]
                left[key] = [
                    item
                    for item in left.get(key, [])
                    if int(item.get("alignment_index", -1)) <= cut
                ]
                right[key] = moved + list(right.get(key, []))
            moved_spans = [
                span
                for span in left.get("protected_spans", [])
                if int(span.get("alignment_start", -1)) > cut
            ]
            left["protected_spans"] = [
                span
                for span in left.get("protected_spans", [])
                if int(span.get("alignment_end", -1)) <= cut
            ]
            right["protected_spans"] = moved_spans + list(right.get("protected_spans", []))
            actions.append(
                {
                    "type": "move_trailing_incomplete_clause",
                    "from_group": left.get("group_id"),
                    "to_group": right.get("group_id"),
                    "alignment_start": cut + 1,
                    "alignment_end": old_end,
                    "text": tail,
                    "continuation_gap_seconds": round(continuation_gap, 3),
                }
            )
            break
    return actions


def _move_dangling_group_tail_to_next(
    *,
    master: str,
    groups: list[dict[str, Any]],
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
) -> list[dict[str, Any]]:
    """Move only an unfinished final cue across a meaning-group boundary.

    This enforces a linguistic invariant without joining two otherwise
    independent speech acts.  When the whole left group is the unfinished
    phrase, it is absorbed into the right group.
    """

    actions: list[dict[str, Any]] = []
    position = 0
    while position < len(groups) - 1:
        left = groups[position]
        right = groups[position + 1]
        left_start = int(left["alignment_start"])
        left_end = int(left["alignment_end"])
        right_start = int(right["alignment_start"])
        if right_start != left_end + 1:
            position += 1
            continue
        bridge = master[ranges[left_end][1] : ranges[right_start][0]]
        if TERMINAL_RE.search(bridge):
            position += 1
            continue
        final_word = _normalized_unit_word(unit_by_index.get(left_end))
        if final_word not in DEPENDENT_MICRO_WORDS:
            position += 1
            continue

        existing_cuts = sorted(
            int(value)
            for value in left.get("line_breaks_after", [])
            if left_start <= int(value) < left_end
        )
        tail_start = existing_cuts[-1] + 1 if existing_cuts else left_start
        old_left_id = str(left.get("group_id", ""))
        old_right_id = str(right.get("group_id", ""))
        if tail_start == left_start:
            right["alignment_start"] = left_start
            right["line_breaks_after"] = sorted(
                set(existing_cuts + [int(value) for value in right.get("line_breaks_after", [])])
            )
            for key in ("deletions", "corrections", "protected_spans"):
                right[key] = list(left.get(key, [])) + list(right.get(key, []))
            right["needs_review"] = bool(
                left.get("needs_review") or right.get("needs_review")
            )
            groups.pop(position)
        else:
            left["alignment_end"] = tail_start - 1
            left["line_breaks_after"] = [
                value for value in existing_cuts if value < tail_start - 1
            ]
            right["alignment_start"] = tail_start
            moved_cuts = [value for value in existing_cuts if value >= tail_start]
            right["line_breaks_after"] = sorted(
                set(moved_cuts + [int(value) for value in right.get("line_breaks_after", [])])
            )
            for key in ("deletions", "corrections"):
                moved = [
                    item
                    for item in left.get(key, [])
                    if int(item.get("alignment_index", -1)) >= tail_start
                ]
                left[key] = [
                    item
                    for item in left.get(key, [])
                    if int(item.get("alignment_index", -1)) < tail_start
                ]
                right[key] = moved + list(right.get(key, []))
            moved_spans = [
                span
                for span in left.get("protected_spans", [])
                if int(span.get("alignment_start", -1)) >= tail_start
            ]
            left["protected_spans"] = [
                span
                for span in left.get("protected_spans", [])
                if int(span.get("alignment_end", -1)) < tail_start
            ]
            right["protected_spans"] = moved_spans + list(
                right.get("protected_spans", [])
            )
            position += 1
        actions.append(
            {
                "type": "move_dangling_group_tail",
                "from_group": old_left_id,
                "to_group": old_right_id,
                "alignment_start": tail_start,
                "alignment_end": left_end,
                "final_word": final_word,
            }
        )
    for number, group in enumerate(groups, start=1):
        group["group_id"] = f"g{number:04d}"
    return actions


def _move_leading_residual_cue_to_previous_group(
    *,
    master: str,
    groups: list[dict[str, Any]],
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
    baseline_punctuation: str,
    raised_punctuation: str,
    english_hard_limit: int,
    chinese_hard_limit: int,
    english_count_spaces: bool,
    english_count_punctuation: bool,
) -> list[dict[str, Any]]:
    """Move a short non-clausal first cue back to the predicate it completes."""

    actions: list[dict[str, Any]] = []
    blocked_heads = INDEPENDENT_CLAUSE_HEADS | LEADING_DEPENDENCY_WORDS | {
        "am",
        "are",
        "can",
        "could",
        "did",
        "do",
        "does",
        "had",
        "has",
        "have",
        "is",
        "may",
        "might",
        "must",
        "shall",
        "should",
        "was",
        "were",
        "will",
        "would",
    }
    for position in range(1, len(groups)):
        left = groups[position - 1]
        right = groups[position]
        right_start = int(right["alignment_start"])
        right_end = int(right["alignment_end"])
        right_cuts = sorted(
            int(value)
            for value in right.get("line_breaks_after", [])
            if right_start <= int(value) < right_end
        )
        if not right_cuts:
            continue
        first_end = right_cuts[0]
        first_text = _segment_text(
            master=master,
            start=right_start,
            end=first_end,
            ranges=ranges,
            group=right,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
        )
        words = ENGLISH_WORD_RE.findall(first_text)
        normalized = [
            word.lower().replace("’", "'") for word in words
        ]
        if (
            not words
            or len(words) > 3
            or words[0][:1].isupper()
            or normalized[0] in blocked_heads
            or " ".join(normalized) in SHORT_STANDALONE
            or TERMINAL_RE.search(first_text.rstrip())
        ):
            continue
        left_end = int(left["alignment_end"])
        bridge = master[ranges[left_end][1] : ranges[right_start][0]]
        if TERMINAL_RE.search(bridge):
            continue
        left_cuts = sorted(
            int(value)
            for value in left.get("line_breaks_after", [])
            if int(left["alignment_start"]) <= int(value) < left_end
        )
        left_tail_start = left_cuts[-1] + 1 if left_cuts else int(left["alignment_start"])
        left_tail = _segment_text(
            master=master,
            start=left_tail_start,
            end=left_end,
            ranges=ranges,
            group=left,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
        )
        combined = f"{left_tail} {first_text}".strip()
        if not _hard_length_ok(
            combined,
            english_hard_limit=english_hard_limit,
            chinese_hard_limit=chinese_hard_limit,
            english_count_spaces=english_count_spaces,
            english_count_punctuation=english_count_punctuation,
        ):
            continue

        old_right_start = right_start
        left["alignment_end"] = first_end
        right["alignment_start"] = first_end + 1
        right["line_breaks_after"] = right_cuts[1:]
        for key in ("deletions", "corrections"):
            moved = [
                item
                for item in right.get(key, [])
                if old_right_start
                <= int(item.get("alignment_index", -1))
                <= first_end
            ]
            right[key] = [
                item
                for item in right.get(key, [])
                if int(item.get("alignment_index", -1)) > first_end
            ]
            left[key] = list(left.get(key, [])) + moved
        moved_spans = [
            span
            for span in right.get("protected_spans", [])
            if old_right_start <= int(span.get("alignment_start", -1))
            and int(span.get("alignment_end", -1)) <= first_end
        ]
        right["protected_spans"] = [
            span
            for span in right.get("protected_spans", [])
            if int(span.get("alignment_start", -1)) > first_end
        ]
        left["protected_spans"] = list(left.get("protected_spans", [])) + moved_spans
        actions.append(
            {
                "type": "move_leading_residual_cue_to_previous_group",
                "from_group": right.get("group_id"),
                "to_group": left.get("group_id"),
                "alignment_start": old_right_start,
                "alignment_end": first_end,
                "text": first_text,
            }
        )
    for number, group in enumerate(groups, start=1):
        group["group_id"] = f"g{number:04d}"
    return actions


def _split_groups_at_independent_centers(
    *,
    master: str,
    groups: list[dict[str, Any]],
    ranges: dict[int, tuple[int, int]],
    unit_by_index: dict[int, AlignmentUnit],
) -> list[dict[str, Any]]:
    """Split at existing cue starts that introduce independent clause heads.

    Cue count is not used.  The pass requires at least two observable centers,
    and each selected boundary must already be a model-proposed display start,
    preserve a non-trivial prefix, and avoid protected spans.
    """

    actions: list[dict[str, Any]] = []
    rebuilt: list[dict[str, Any]] = []
    for group in groups:
        start = int(group["alignment_start"])
        end = int(group["alignment_end"])
        existing_cuts = sorted(
            int(value)
            for value in group.get("line_breaks_after", [])
            if start <= int(value) < end
        )
        cue_starts = [start] + [value + 1 for value in existing_cuts]
        centers = [
            cue_start
            for cue_start in cue_starts
            if _normalized_unit_word(unit_by_index.get(cue_start))
            in INDEPENDENT_CLAUSE_HEADS
        ]
        if len(centers) < 2:
            rebuilt.append(group)
            continue
        protected = list(group.get("protected_spans", []))
        split_starts: list[int] = []
        for center in centers:
            split_start = center
            while (
                split_start > start
                and _normalized_unit_word(unit_by_index.get(split_start - 1))
                in DEPENDENT_MICRO_WORDS
            ):
                split_start -= 1
            if split_start <= start:
                continue
            cut = split_start - 1
            if any(
                int(span.get("alignment_start", -1)) <= cut
                < int(span.get("alignment_end", -1))
                for span in protected
            ):
                continue
            prefix_start = split_starts[-1] if split_starts else start
            prefix_words = [
                _normalized_unit_word(unit_by_index.get(index))
                for index in range(prefix_start, split_start)
            ]
            if len([word for word in prefix_words if word]) < 4:
                continue
            split_starts.append(split_start)
        if not split_starts:
            rebuilt.append(group)
            continue

        boundaries = [value - 1 for value in split_starts] + [end]
        part_start = start
        original_id = str(group.get("group_id", ""))
        for part_end in boundaries:
            copied = copy.deepcopy(group)
            copied["alignment_start"] = part_start
            copied["alignment_end"] = part_end
            copied["line_breaks_after"] = [
                cut for cut in existing_cuts if part_start <= cut < part_end
            ]
            for key in ("deletions", "corrections"):
                copied[key] = [
                    item
                    for item in group.get(key, [])
                    if part_start
                    <= int(item.get("alignment_index", -1))
                    <= part_end
                ]
            copied["protected_spans"] = [
                span
                for span in protected
                if part_start <= int(span.get("alignment_start", -1))
                and int(span.get("alignment_end", -1)) <= part_end
            ]
            copied["reason"] = (
                f"{group.get('reason', '')} "
                "程序按既有显示起点处的独立谓词中心拆分。"
            ).strip()
            rebuilt.append(copied)
            part_start = part_end + 1
        actions.append(
            {
                "type": "split_at_independent_clause_centers",
                "group_id": original_id,
                "alignment_start": start,
                "alignment_end": end,
                "split_starts": split_starts,
            }
        )
    groups[:] = rebuilt
    for number, group in enumerate(groups, start=1):
        group["group_id"] = f"g{number:04d}"
    return actions


def _split_unreviewed_multisentence_groups_at_reliable_ends(
    *,
    master: str,
    groups: list[dict[str, Any]],
    ranges: dict[int, tuple[int, int]],
) -> list[dict[str, Any]]:
    """Split unreviewed multi-sentence groups only at reliable sentence ends.

    Display-cue count is deliberately irrelevant. Independent sentence
    evidence may justify a semantic boundary; a large number of display cues
    by itself never does. The pass never splits a protected span.
    """

    actions: list[dict[str, Any]] = []
    rebuilt: list[dict[str, Any]] = []
    for group in groups:
        start = int(group["alignment_start"])
        end = int(group["alignment_end"])
        existing_cuts = sorted(
            int(value)
            for value in group.get("line_breaks_after", [])
            if start <= int(value) < end
        )
        if bool(group.get("needs_review")):
            rebuilt.append(group)
            continue
        protected = group.get("protected_spans", [])
        sentence_cuts = [
            cut
            for cut in range(start, end)
            if TERMINAL_RE.search(master[ranges[cut][1] : ranges[cut + 1][0]])
            and not any(
                int(span.get("alignment_start", -1)) <= cut
                < int(span.get("alignment_end", -1))
                for span in protected
            )
        ]
        if len(sentence_cuts) < 2:
            rebuilt.append(group)
            continue
        boundaries = sentence_cuts + [end]
        part_start = start
        original_id = str(group.get("group_id", ""))
        for part_end in boundaries:
            copied = copy.deepcopy(group)
            copied["alignment_start"] = part_start
            copied["alignment_end"] = part_end
            copied["line_breaks_after"] = [
                cut for cut in existing_cuts if part_start <= cut < part_end
            ]
            for key in ("deletions", "corrections"):
                copied[key] = [
                    item
                    for item in group.get(key, [])
                    if part_start
                    <= int(item.get("alignment_index", -1))
                    <= part_end
                ]
            copied["protected_spans"] = [
                span
                for span in protected
                if part_start <= int(span.get("alignment_start", -1))
                and int(span.get("alignment_end", -1)) <= part_end
            ]
            copied["reason"] = (
                f"{group.get('reason', '')} "
                "程序按未复核长组的可靠内部句末拆分。"
            ).strip()
            rebuilt.append(copied)
            part_start = part_end + 1
        actions.append(
            {
                "type": "split_unreviewed_multisentence_group",
                "group_id": original_id,
                "alignment_start": start,
                "alignment_end": end,
                "sentence_cuts": sentence_cuts,
            }
        )
    groups[:] = rebuilt
    for number, group in enumerate(groups, start=1):
        group["group_id"] = f"g{number:04d}"
    return actions


def optimize_direct_plan(
    master: str,
    units: list[AlignmentUnit],
    plan: dict[str, Any],
    *,
    baseline_punctuation: str = "preserve",
    raised_punctuation: str = "preserve",
    english_hard_limit: int = 55,
    chinese_hard_limit: int = 24,
    english_count_spaces: bool = False,
    english_count_punctuation: bool = True,
    minimum_cue_duration_ms: int = 400,
    force_reflow: bool = False,
) -> OptimizationResult:
    optimized = copy.deepcopy(plan)
    groups = optimized.get("groups", [])
    raw_ranges = _unit_original_ranges(master, units)
    ranges = {unit.index: raw_ranges[position] for position, unit in enumerate(units)}
    unit_by_index = {unit.index: unit for unit in units}
    # Stage A: normalize only measurable meaning-group invariants.  No cue
    # count or visual-length target is allowed to create a semantic boundary.
    actions: list[dict[str, Any]] = []
    for group in groups:
        retained_spans: list[dict[str, Any]] = []
        for span in group.get("protected_spans", []):
            level = str(span.get("protection_level", "hard"))
            span_start = int(span.get("alignment_start", -1))
            span_end = int(span.get("alignment_end", -1))
            if level != "hard":
                retained_spans.append(span)
                continue
            if span_start not in ranges or span_end not in ranges:
                retained_spans.append(span)
                continue
            span_text = _segment_text(
                master=master,
                start=span_start,
                end=span_end,
                ranges=ranges,
                group=group,
                baseline_punctuation=baseline_punctuation,
                raised_punctuation=raised_punctuation,
            )
            if _hard_length_ok(
                span_text,
                english_hard_limit=english_hard_limit,
                chinese_hard_limit=chinese_hard_limit,
                english_count_spaces=english_count_spaces,
                english_count_punctuation=english_count_punctuation,
            ):
                retained_spans.append(span)
                continue
            # A span longer than the active hard limit cannot be an absolute
            # no-cut constraint. Downgrade it to review telemetry so the
            # optimizer can choose the least harmful legal cut instead of
            # rejecting the whole multi-minute block.
            actions.append(
                {
                    "type": "downgrade_oversized_protected_span",
                    "group_id": group.get("group_id"),
                    "alignment_start": span_start,
                    "alignment_end": span_end,
                    "category": span.get("category"),
                    "protection_level": level,
                }
            )
            group["needs_review"] = True
            group["protected_spans"] = retained_spans
    actions.extend(
        _merge_dependent_micro_groups(
            master=master,
            groups=groups,
            ranges=ranges,
            unit_by_index=unit_by_index,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
        )
    )
    actions.extend(
        _move_trailing_incomplete_clause_to_next_group(
            master=master,
            groups=groups,
            ranges=ranges,
            unit_by_index=unit_by_index,
        )
    )
    actions.extend(
        _move_dangling_group_tail_to_next(
            master=master,
            groups=groups,
            ranges=ranges,
            unit_by_index=unit_by_index,
        )
    )
    actions.extend(
        _move_leading_residual_cue_to_previous_group(
            master=master,
            groups=groups,
            ranges=ranges,
            unit_by_index=unit_by_index,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
            english_hard_limit=english_hard_limit,
            chinese_hard_limit=chinese_hard_limit,
            english_count_spaces=english_count_spaces,
            english_count_punctuation=english_count_punctuation,
        )
    )
    actions.extend(
        _split_unreviewed_multisentence_groups_at_reliable_ends(
            master=master,
            groups=groups,
            ranges=ranges,
        )
    )
    actions.extend(
        _split_groups_at_independent_centers(
            master=master,
            groups=groups,
            ranges=ranges,
            unit_by_index=unit_by_index,
        )
    )
    # Stage B: lay out display cues inside the already fixed meaning groups.
    for group in groups:
        hard_needs_optimization = _current_group_needs_optimization(
            master=master,
            group=group,
            ranges=ranges,
            unit_by_index=unit_by_index,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
            english_hard_limit=english_hard_limit,
            chinese_hard_limit=chinese_hard_limit,
            english_count_spaces=english_count_spaces,
            english_count_punctuation=english_count_punctuation,
        )
        before_risk_count = _group_line_break_risk_count(
            master=master,
            group=group,
            ranges=ranges,
            unit_by_index=unit_by_index,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
        )
        if (
            not force_reflow
            and not hard_needs_optimization
            and before_risk_count == 0
        ):
            continue
        before = [int(value) for value in group.get("line_breaks_after", [])]
        after = _optimize_group_breaks(
            master=master,
            group=group,
            ranges=ranges,
            unit_by_index=unit_by_index,
            baseline_punctuation=baseline_punctuation,
            raised_punctuation=raised_punctuation,
            english_hard_limit=english_hard_limit,
            chinese_hard_limit=chinese_hard_limit,
            english_count_spaces=english_count_spaces,
            english_count_punctuation=english_count_punctuation,
            minimum_cue_duration_ms=minimum_cue_duration_ms,
            maximum_cues=(
                len(before) + 1
                if force_reflow
                or (before_risk_count > 0 and not hard_needs_optimization)
                else None
            ),
        )
        if after is None:
            # Structural completeness outranks preserving the model's current
            # cue count.  Retry without the compactness ceiling before
            # permitting any linguistically incomplete cut.
            after = _optimize_group_breaks(
                master=master,
                group=group,
                ranges=ranges,
                unit_by_index=unit_by_index,
                baseline_punctuation=baseline_punctuation,
                raised_punctuation=raised_punctuation,
                english_hard_limit=english_hard_limit,
                chinese_hard_limit=chinese_hard_limit,
                english_count_spaces=english_count_spaces,
                english_count_punctuation=english_count_punctuation,
                minimum_cue_duration_ms=minimum_cue_duration_ms,
                maximum_cues=None,
            )
            if after is not None:
                actions.append(
                    {
                        "type": "increase_cue_count_to_preserve_structure",
                        "group_id": group.get("group_id"),
                    }
                )
        if after is None:
            # Finite delivery fallback: relax only the incomplete-function-word
            # boundary ban.  The result remains reviewable and is surfaced in
            # telemetry instead of causing repeated whole-block retries.
            after = _optimize_group_breaks(
                master=master,
                group=group,
                ranges=ranges,
                unit_by_index=unit_by_index,
                baseline_punctuation=baseline_punctuation,
                raised_punctuation=raised_punctuation,
                english_hard_limit=english_hard_limit,
                chinese_hard_limit=chinese_hard_limit,
                english_count_spaces=english_count_spaces,
                english_count_punctuation=english_count_punctuation,
                minimum_cue_duration_ms=minimum_cue_duration_ms,
                maximum_cues=None,
                allow_incomplete_cuts=True,
            )
            if after is not None:
                actions.append(
                    {
                        "type": "relax_incomplete_cut_for_delivery",
                        "group_id": group.get("group_id"),
                    }
                )
        if after is None:
            group["needs_review"] = True
            actions.append(
                {
                    "type": "optimizer_no_path",
                    "group_id": group.get("group_id"),
                }
            )
            continue
        if before_risk_count > 0 and not hard_needs_optimization:
            candidate = copy.deepcopy(group)
            candidate["line_breaks_after"] = after
            after_risk_count = _group_line_break_risk_count(
                master=master,
                group=candidate,
                ranges=ranges,
                unit_by_index=unit_by_index,
                baseline_punctuation=baseline_punctuation,
                raised_punctuation=raised_punctuation,
            )
            if after_risk_count >= before_risk_count:
                continue
        group["line_breaks_after"] = after
        if before != after:
            actions.append(
                {
                    "type": "optimize_line_breaks",
                    "group_id": group.get("group_id"),
                    "before": before,
                    "after": after,
                }
            )
    compacted = compact_mergeable_line_breaks(
        master,
        units,
        optimized,
        baseline_punctuation=baseline_punctuation,
        raised_punctuation=raised_punctuation,
        english_hard_limit=english_hard_limit,
        chinese_hard_limit=chinese_hard_limit,
        english_count_spaces=english_count_spaces,
        english_count_punctuation=english_count_punctuation,
    )
    actions.extend(compacted.actions)
    return OptimizationResult(plan=compacted.plan, actions=actions)
