from __future__ import annotations

from dataclasses import dataclass

from .material import AlignmentUnit, extract_alignment, extract_master
from .chunking import (
    SegmentationChunk,
    _unit_original_ranges,
    build_segmentation_chunks,
    render_chunk_material,
)


@dataclass(frozen=True)
class AdaptiveAnalysisChunk:
    chunk_id: str
    core_start: int
    core_end: int
    analysis_start: int
    analysis_end: int
    start_seconds: float
    end_seconds: float
    material: str
    units: list[AlignmentUnit]


def _slice_master(
    master: str,
    units: list[AlignmentUnit],
    ranges: list[tuple[int, int]],
    start_position: int,
    end_position: int,
) -> str:
    char_start = 0 if start_position == 0 else ranges[start_position][0]
    char_end = (
        len(master)
        if end_position == len(units) - 1
        else ranges[end_position + 1][0]
    )
    return master[char_start:char_end].strip()


def build_adaptive_analysis_chunks(
    material: str,
    *,
    target_seconds: float = 360.0,
    overlap_seconds: float = 40.0,
) -> list[AdaptiveAnalysisChunk]:
    """Build long semantic cores with overlapping A1 proposal scopes.

    Core ownership is disjoint and lossless. Only A1 proposal input overlaps;
    no overlapping text is ever concatenated into the final transcript.
    """

    master = extract_master(material)
    units = extract_alignment(material)
    cores = build_segmentation_chunks(material, target_seconds)
    if len(cores) == 1:
        return [
            AdaptiveAnalysisChunk(
                chunk_id=cores[0].chunk_id,
                core_start=int(units[0].index),
                core_end=int(units[-1].index),
                analysis_start=int(units[0].index),
                analysis_end=int(units[-1].index),
                start_seconds=float(units[0].start),
                end_seconds=float(units[-1].end),
                material=material,
                units=units,
            )
        ]

    ranges = _unit_original_ranges(master, units)
    position_by_index = {int(unit.index): position for position, unit in enumerate(units)}
    chunks: list[AdaptiveAnalysisChunk] = []
    for core in cores:
        core_start_position = position_by_index[int(core.units[0].index)]
        core_end_position = position_by_index[int(core.units[-1].index)]
        left_time = max(float(units[0].start), float(core.units[0].start) - overlap_seconds)
        right_time = min(float(units[-1].end), float(core.units[-1].end) + overlap_seconds)
        analysis_start_position = core_start_position
        while (
            analysis_start_position > 0
            and float(units[analysis_start_position - 1].end) >= left_time
        ):
            analysis_start_position -= 1
        analysis_end_position = core_end_position
        while (
            analysis_end_position + 1 < len(units)
            and float(units[analysis_end_position + 1].start) <= right_time
        ):
            analysis_end_position += 1
        analysis_units = units[analysis_start_position : analysis_end_position + 1]
        analysis_master = _slice_master(
            master,
            units,
            ranges,
            analysis_start_position,
            analysis_end_position,
        )
        proposal_chunk = SegmentationChunk(
            chunk_id=core.chunk_id,
            master_text=analysis_master,
            units=analysis_units,
            context_before="",
            context_after="",
            start_seconds=float(analysis_units[0].start),
            end_seconds=float(analysis_units[-1].end),
        )
        scope = "\n".join(
            [
                "# ADAPTIVE_A1_SCOPE",
                f"core_alignment: {core.units[0].index}-{core.units[-1].index}",
                f"analysis_alignment: {analysis_units[0].index}-{analysis_units[-1].index}",
                "请分析整个 analysis 范围。core 是本块稳定所有权；与相邻块重叠部分是"
                "非绑定接缝提案，后续 A1-S 会统一裁决。",
                "",
                render_chunk_material(proposal_chunk),
            ]
        )
        chunks.append(
            AdaptiveAnalysisChunk(
                chunk_id=core.chunk_id,
                core_start=int(core.units[0].index),
                core_end=int(core.units[-1].index),
                analysis_start=int(analysis_units[0].index),
                analysis_end=int(analysis_units[-1].index),
                start_seconds=float(analysis_units[0].start),
                end_seconds=float(analysis_units[-1].end),
                material=scope,
                units=analysis_units,
            )
        )
    return chunks


def seam_seed_windows(
    chunks: list[AdaptiveAnalysisChunk],
) -> list[dict[str, int | str]]:
    windows: list[dict[str, int | str]] = []
    for position in range(len(chunks) - 1):
        left = chunks[position]
        right = chunks[position + 1]
        start = max(left.analysis_start, right.analysis_start)
        end = min(left.analysis_end, right.analysis_end)
        if start > end:
            start = left.core_end
            end = right.core_start
        windows.append(
            {
                "seam_id": f"s{position + 1:04d}",
                "left_chunk": left.chunk_id,
                "right_chunk": right.chunk_id,
                "window_start": int(start),
                "window_end": int(end),
                "technical_boundary_after": int(left.core_end),
            }
        )
    return windows
