from __future__ import annotations

import re
from dataclasses import dataclass

from .material import AlignmentUnit, extract_alignment, extract_master


KEEP_RE = re.compile(r"[0-9A-Za-z\u3400-\u9fff]")
SENTENCE_END_RE = re.compile(r"[.?!。！？]")


@dataclass(frozen=True)
class SegmentationChunk:
    chunk_id: str
    master_text: str
    units: list[AlignmentUnit]
    context_before: str
    context_after: str
    start_seconds: float
    end_seconds: float


def _normalized_with_original_positions(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    positions: list[int] = []
    for original_index, char in enumerate(text):
        if not KEEP_RE.fullmatch(char):
            continue
        folded = char.casefold()
        normalized.extend(folded)
        positions.extend([original_index] * len(folded))
    return "".join(normalized), positions


def _unit_normalized(text: str) -> str:
    return "".join(char.casefold() for char in text if KEEP_RE.fullmatch(char))


def _unit_original_ranges(master: str, units: list[AlignmentUnit]) -> list[tuple[int, int]]:
    normalized_master, positions = _normalized_with_original_positions(master)
    normalized_units = "".join(_unit_normalized(unit.text) for unit in units)
    if normalized_master != normalized_units:
        raise ValueError("MASTER_TRANSCRIPT 与 ALIGNMENT 归一化字词序列不一致，不能安全分块")
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for unit in units:
        length = len(_unit_normalized(unit.text))
        if length == 0:
            ranges.append((ranges[-1][1] if ranges else 0, ranges[-1][1] if ranges else 0))
            continue
        start = positions[cursor]
        end = positions[cursor + length - 1] + 1
        ranges.append((start, end))
        cursor += length
    return ranges


def _choose_end(
    master: str,
    units: list[AlignmentUnit],
    ranges: list[tuple[int, int]],
    start_position: int,
    chunk_seconds: float,
) -> int:
    last = len(units) - 1
    start_time = units[start_position].start
    target = start_time + chunk_seconds
    if units[last].end <= target:
        return last

    minimum = start_time + max(60.0, chunk_seconds * 0.55)
    maximum = start_time + chunk_seconds * 1.25
    candidates: list[tuple[float, int]] = []
    for position in range(start_position, last):
        boundary_time = units[position].end
        if boundary_time < minimum or boundary_time > maximum:
            continue
        next_start = ranges[position + 1][0]
        separator = master[ranges[position][1] : next_start]
        sentence_end = bool(SENTENCE_END_RE.search(separator))
        gap = max(0.0, units[position + 1].start - units[position].end)
        distance_seconds = abs(boundary_time - target)
        # Technical chunks should remain close to their requested duration.
        # Sentence punctuation and acoustic gaps select a nearby clean seam;
        # they must not pull a nominal 240-second chunk a minute away from its
        # target because ASR segmentation changed between otherwise equivalent
        # runs.
        score = (
            -distance_seconds
            + (12.0 if sentence_end else 0.0)
            + min(gap, 1.0) * 6.0
        )
        if sentence_end or gap >= 0.5 or boundary_time >= start_time + chunk_seconds * 0.98:
            candidates.append((score, position))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return min(
        range(start_position, last),
        key=lambda position: abs(units[position].end - target),
    )


def build_segmentation_chunks(material: str, chunk_seconds: float = 600.0) -> list[SegmentationChunk]:
    master = extract_master(material)
    units = extract_alignment(material)
    if chunk_seconds <= 0 or units[-1].end - units[0].start <= chunk_seconds * 1.1:
        return [
            SegmentationChunk(
                chunk_id="c0001",
                master_text=master,
                units=units,
                context_before="",
                context_after="",
                start_seconds=units[0].start,
                end_seconds=units[-1].end,
            )
        ]

    ranges = _unit_original_ranges(master, units)
    boundaries: list[tuple[int, int]] = []
    start_position = 0
    while start_position < len(units):
        end_position = _choose_end(master, units, ranges, start_position, chunk_seconds)
        if end_position < start_position:
            raise ValueError("Stage 1 分块器没有前进")
        boundaries.append((start_position, end_position))
        start_position = end_position + 1

    chunks: list[SegmentationChunk] = []
    for number, (unit_start, unit_end) in enumerate(boundaries, start=1):
        char_start = 0 if unit_start == 0 else ranges[unit_start][0]
        char_end = len(master) if unit_end == len(units) - 1 else ranges[unit_end + 1][0]
        core = master[char_start:char_end].strip()
        context_before = master[max(0, char_start - 240) : char_start].strip()
        context_after = master[char_end : min(len(master), char_end + 240)].strip()
        chunks.append(
            SegmentationChunk(
                chunk_id=f"c{number:04d}",
                master_text=core,
                units=units[unit_start : unit_end + 1],
                context_before=context_before,
                context_after=context_after,
                start_seconds=units[unit_start].start,
                end_seconds=units[unit_end].end,
            )
        )

    reconstructed = " ".join(chunk.master_text for chunk in chunks)
    normalized_reconstructed, _ = _normalized_with_original_positions(reconstructed)
    normalized_master, _ = _normalized_with_original_positions(master)
    if normalized_reconstructed != normalized_master:
        raise ValueError("Stage 1 分块后不能完整恢复主稿")
    return chunks


def render_chunk_material(
    chunk: SegmentationChunk,
    *,
    sentence_hint_mode: str = "full",
) -> str:
    if sentence_hint_mode not in {"full", "neutral"}:
        raise ValueError(f"未知 sentence_hint_mode：{sentence_hint_mode}")
    context = [
        "# SEGMENTATION_CHUNK_SCOPE",
        f"chunk_id: {chunk.chunk_id}",
        f"time_range: {chunk.start_seconds:.3f}-{chunk.end_seconds:.3f}",
        "只为 CORE MASTER_TRANSCRIPT 输出 groups。上下文仅用于判断边界，不得写入 source_text。",
    ]
    if chunk.context_before:
        context.extend(["", "## CONTEXT_BEFORE (read only)", chunk.context_before])
    if chunk.context_after:
        context.extend(["", "## CONTEXT_AFTER (read only)", chunk.context_after])
    if sentence_hint_mode == "neutral":
        alignment_lines = "\n".join(
            f"{unit.index}\t{unit.start:.3f}\t{unit.end:.3f}\t"
            + (
                re.sub(r"[.?!。！？]+$", "", str(unit.text))
                if unit.sentence_end
                else str(unit.text)
            )
            for unit in chunk.units
        )
        alignment_description = (
            "字段为 `index / start秒 / end秒 / text`。本视图刻意不提供 ASR 句段编号和"
            "句段起止位；请仅依据连续语义、语法、停顿与显示约束判断边界。"
        )
    else:
        alignment_lines = "\n".join(
            f"{unit.index}\t{unit.start:.3f}\t{unit.end:.3f}\t{unit.text}\t"
            f"{unit.sentence_id if unit.sentence_id is not None else '-'}\t"
            f"{1 if unit.sentence_start else 0}\t{1 if unit.sentence_end else 0}"
            for unit in chunk.units
        )
        alignment_description = (
            "字段为 `index / start秒 / end秒 / text / "
            "whisper_sentence_id / sentence_start / sentence_end`。"
            "`sentence_start/end=1` 是 Whisper 句段外层边界，不是必须照抄的最终字幕行；"
            "明确完整句末优先保留，句法未完成时允许跨相邻句段重新组合。"
        )
    semantic_view = ""
    if any(unit.sentence_end for unit in chunk.units):
        # Whisper often materializes its decoder segments as terminal
        # punctuation. Provide a second, punctuation-neutral reading view so
        # Semantic grouping can judge lexical continuity without deleting punctuation.
        # from the immutable master transcript.
        semantic_tokens = [
            (
                re.sub(r"[.?!。！？]+$", "", str(unit.text))
                if unit.sentence_end
                else str(unit.text)
            )
            for unit in chunk.units
        ]
        semantic_view = (
            "\n\n## SEMANTIC_READING_VIEW (read only)\n\n"
            "该视图只移除了与 Whisper sentence_end 重合的技术性句末标点，"
            "用于连续理解；不得据此改写 MASTER_TRANSCRIPT。\n\n```text\n"
            + " ".join(token for token in semantic_tokens if token)
            + "\n```"
        )
    return "\n".join(context) + "\n\n" + (
        "## MASTER_TRANSCRIPT\n\n```text\n"
        + chunk.master_text
        + "\n```"
        + semantic_view
        + "\n\n## ALIGNMENT\n\n"
        + alignment_description
        + "\n\n```tsv\n"
        + alignment_lines
        + "\n```\n"
    )
