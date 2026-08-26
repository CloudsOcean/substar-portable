from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


_LETTER = re.compile(r"^[A-Za-z]$")


@dataclass(frozen=True)
class Canonicalization:
    alignment_start: int
    alignment_end: int
    canonical_text: str
    source_text: str
    source: str
    confidence: float = 1.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alignment_start": self.alignment_start,
            "alignment_end": self.alignment_end,
            "canonical_text": self.canonical_text,
            "source_text": self.source_text,
            "source": self.source,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def _compact(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).casefold()


def glossary_abbreviation_map(entries: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Return deterministic compact aliases for glossary abbreviations."""

    result: dict[str, str] = {}
    for entry in entries:
        if not entry.get("enabled", True):
            continue
        canonical = str(entry.get("standard_source") or entry.get("source") or "").strip()
        if not canonical:
            continue
        raw_aliases = entry.get("aliases", [])
        aliases_list = raw_aliases if isinstance(raw_aliases, list) else []
        forms = [entry.get("source"), entry.get("standard_source"), *aliases_list]
        for form in forms:
            key = _compact(form)
            if len(key) >= 2:
                result.setdefault(key, canonical)
    return result


def deterministic_letter_canonicalizations(
    units: list[Any],
    entries: Iterable[dict[str, Any]],
    *,
    left: int = 0,
    right: int | None = None,
) -> list[dict[str, Any]]:
    """Recognize glossary-backed runs such as ``c a t l`` without reindexing."""

    if not units:
        return []
    right = len(units) - 1 if right is None else min(right, len(units) - 1)
    aliases = glossary_abbreviation_map(entries)
    if not aliases:
        return []
    result: list[dict[str, Any]] = []
    cursor = max(0, left)
    while cursor <= right:
        if not _LETTER.fullmatch(str(units[cursor].text).strip()):
            cursor += 1
            continue
        end = cursor
        while end + 1 <= right and _LETTER.fullmatch(str(units[end + 1].text).strip()):
            end += 1
        # Longest glossary hit wins; shorter suffixes remain available for a
        # subsequent scan. This makes the deterministic result independent of
        # glossary insertion order.
        matched_end: int | None = None
        matched_text = ""
        for candidate_end in range(end, cursor, -1):
            compact = "".join(
                str(units[pos].text).strip()
                for pos in range(cursor, candidate_end + 1)
            ).casefold()
            if compact in aliases:
                matched_end = candidate_end
                matched_text = aliases[compact]
                break
        if matched_end is None:
            cursor = end + 1
            continue
        source_text = " ".join(
            str(units[pos].text).strip() for pos in range(cursor, matched_end + 1)
        )
        result.append(
            Canonicalization(
                alignment_start=int(units[cursor].index),
                alignment_end=int(units[matched_end].index),
                canonical_text=matched_text,
                source_text=source_text,
                source="glossary_deterministic",
                reason="consecutive single-letter glossary abbreviation",
            ).to_dict()
        )
        cursor = matched_end + 1
    return result


def validate_api_canonicalizations(
    rows: object,
    units: list[Any],
    *,
    left: int,
    right: int,
) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("canonicalizations 必须是数组")
    by_index = {int(unit.index): unit for unit in units[left : right + 1]}
    first = int(units[left].index)
    last = int(units[right].index)
    result: list[dict[str, Any]] = []
    occupied: set[int] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("canonicalizations 项必须是对象")
        start = int(raw.get("alignment_start", -1))
        end = int(raw.get("alignment_end", -1))
        canonical = str(raw.get("canonical_text", "")).strip()
        if not first <= start < end <= last or not canonical:
            raise ValueError("canonicalization 范围或 canonical_text 无效")
        indexes = list(range(start, end + 1))
        if any(index not in by_index for index in indexes):
            raise ValueError("canonicalization 引用了不存在的 alignment index")
        if any(index in occupied for index in indexes):
            raise ValueError("canonicalizations 不得重叠")
        letters = [str(by_index[index].text).strip() for index in indexes]
        if not all(_LETTER.fullmatch(value) for value in letters):
            raise ValueError("canonicalization 目前只允许连续单字母缩写")
        if _compact("".join(letters)) != _compact(canonical):
            raise ValueError("canonical_text 必须与连续字母严格等价")
        occupied.update(indexes)
        result.append(
            Canonicalization(
                alignment_start=start,
                alignment_end=end,
                canonical_text=canonical,
                source_text=" ".join(letters),
                source="p2_contextual",
                confidence=float(raw.get("confidence", 0.8)),
                reason=str(raw.get("reason", "P2 contextual abbreviation")),
            ).to_dict()
        )
    return result


def normalize_ai_calibration_candidates(
    rows: object,
    units: list[Any],
    *,
    left: int,
    right: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize model ASR corrections without treating factual certainty as validation.

    The result is split into deterministic, safe-to-apply rows and audit rows
    that are retained but not materialized.  This accepts the current
    ``alignment_start/alignment_end`` shape and the historical ``index/standard``
    and ``source/target`` shapes.
    """

    raw_rows = rows if isinstance(rows, list) else ([] if rows is None else [rows])
    by_index = {int(unit.index): unit for unit in units[left : right + 1]}
    ordered_indexes = list(by_index)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    occupied: set[int] = set()

    def reject(raw: object, detail: str) -> None:
        rejected.append(
            {
                "code": "AI_CALIBRATION_UNAPPLIED",
                "detail": detail,
                "raw": raw,
            }
        )

    for raw in raw_rows:
        if not isinstance(raw, dict):
            reject(raw, "candidate must be an object")
            continue

        target = ""
        for key in ("after_text", "corrected_text", "target", "standard", "canonical_text"):
            if raw.get(key) is not None and str(raw.get(key)).strip():
                target = str(raw.get(key)).strip()
                break
        if not target:
            reject(raw, "candidate has no non-empty correction text")
            continue

        start: int | None = None
        end: int | None = None
        try:
            if raw.get("alignment_start") is not None:
                start = int(raw["alignment_start"])
                end = int(raw.get("alignment_end", start))
            elif raw.get("index") is not None:
                start = end = int(raw["index"])
        except (TypeError, ValueError):
            reject(raw, "candidate alignment is not an integer")
            continue

        source_hint = raw.get("before_text") or raw.get("source_text")
        if source_hint is None and "source" in raw and "target" in raw:
            source_hint = raw.get("source")

        if start is None or end is None:
            source_key = _compact(source_hint)
            if not source_key:
                reject(raw, "candidate has neither alignment nor source text")
                continue
            matches: list[tuple[int, int]] = []
            for begin, begin_index in enumerate(ordered_indexes):
                joined = ""
                for finish in range(begin, len(ordered_indexes)):
                    joined += str(by_index[ordered_indexes[finish]].text).strip()
                    if _compact(joined) == source_key:
                        matches.append((begin_index, ordered_indexes[finish]))
                    if len(_compact(joined)) >= len(source_key):
                        break
            if len(matches) != 1:
                reject(
                    raw,
                    "source text does not resolve to one unique contiguous alignment span",
                )
                continue
            start, end = matches[0]

        if (
            start is None
            or end is None
            or start > end
            or start not in by_index
            or end not in by_index
            or any(index not in by_index for index in range(start, end + 1))
        ):
            reject(raw, "candidate alignment span is outside the execution block")
            continue

        indexes = list(range(start, end + 1))
        if any(index in occupied for index in indexes):
            reject(raw, "candidate overlaps another applied correction")
            continue
        before = " ".join(str(by_index[index].text).strip() for index in indexes).strip()
        if source_hint is not None and _compact(source_hint) != _compact(before):
            reject(raw, "candidate source text does not match ASR tokens")
            continue
        try:
            confidence = float(raw.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        accepted.append(
            {
                "alignment_start": start,
                "alignment_end": end,
                "canonical_text": target,
                "source_text": before,
                "source": "ai_calibration",
                "kind": "ai_calibration",
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(raw.get("reason", "model ASR calibration")),
            }
        )
        occupied.update(indexes)
    return accepted, rejected


def merge_canonicalizations(*collections: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[int, int], dict[str, Any]] = {}
    for collection in collections:
        for row in collection:
            key = (int(row["alignment_start"]), int(row["alignment_end"]))
            current = selected.get(key)
            if current is None or str(row.get("source")) == "glossary_deterministic":
                selected[key] = dict(row)
    ordered = sorted(
        selected.values(),
        key=lambda row: (
            int(row["alignment_start"]),
            int(row["alignment_end"]),
        ),
    )
    previous_end = -1
    for row in ordered:
        if int(row["alignment_start"]) <= previous_end:
            raise ValueError("canonicalizations 合并后存在重叠")
        previous_end = int(row["alignment_end"])
    return ordered


def logical_rows(
    units: list[Any],
    *,
    left: int,
    right: int,
    canonicalizations: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_start = {int(row["alignment_start"]): row for row in canonicalizations}
    position_by_index = {int(unit.index): pos for pos, unit in enumerate(units)}
    rows: list[dict[str, Any]] = []
    cursor = left
    while cursor <= right:
        unit = units[cursor]
        canonical = by_start.get(int(unit.index))
        if canonical is None:
            rows.append(
                {
                    "alignment_start": int(unit.index),
                    "alignment_end": int(unit.index),
                    "text": str(unit.text).strip(),
                    "canonicalized": False,
                }
            )
            cursor += 1
            continue
        rows.append(
            {
                "alignment_start": int(canonical["alignment_start"]),
                "alignment_end": int(canonical["alignment_end"]),
                "text": str(canonical["canonical_text"]),
                "canonicalized": True,
            }
        )
        cursor = position_by_index[int(canonical["alignment_end"])] + 1
    return rows
