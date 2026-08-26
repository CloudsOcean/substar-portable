from __future__ import annotations

import base64
import difflib
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np


LEXICAL_TOKEN_RE = re.compile(
    r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+(?:['’][A-Za-zÀ-ÖØ-öø-ÿ0-9]+)*"
    r"|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]"
)


@dataclass(frozen=True)
class AudioRange:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, float]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
        }


@dataclass(frozen=True)
class TextToken:
    text: str
    normalized: str
    start: int
    end: int


def encoded_base64_size(path_or_size: Path | int) -> int:
    """Return the exact Base64 character count without reading the file."""
    size = path_or_size if isinstance(path_or_size, int) else path_or_size.stat().st_size
    return 4 * math.ceil(int(size) / 3)


def payload_megabytes(path_or_size: Path | int) -> float:
    return encoded_base64_size(path_or_size) / (1024 * 1024)


def estimated_mp3_base64_bytes(duration_seconds: float, bitrate_kbps: int) -> int:
    raw_bytes = duration_seconds * bitrate_kbps * 1000 / 8
    return encoded_base64_size(math.ceil(raw_bytes))


def lexical_tokens(text: str) -> list[TextToken]:
    result: list[TextToken] = []
    for match in LEXICAL_TOKEN_RE.finditer(text):
        value = match.group(0)
        result.append(
            TextToken(
                text=value,
                normalized=value.replace("’", "'").casefold(),
                start=match.start(),
                end=match.end(),
            )
        )
    return result


def transcript_quality(text: str, duration_seconds: float) -> dict[str, Any]:
    """Detect non-empty ASR failures such as runaway repetition.

    Thresholds are deliberately permissive. They are intended to catch model
    failures, not reject naturally fast speakers or dense Chinese speech.
    """
    tokens = lexical_tokens(text)
    normalized = [item.normalized for item in tokens]
    cjk_count = sum(
        1
        for item in tokens
        if len(item.text) == 1
        and (
            "\u3400" <= item.text <= "\u9fff"
            or "\u3040" <= item.text <= "\u30ff"
            or "\uac00" <= item.text <= "\ud7af"
        )
    )
    rate = len(tokens) * 60 / max(1.0, duration_seconds)
    ngram_size = 8
    ngrams = [
        tuple(normalized[index : index + ngram_size])
        for index in range(max(0, len(normalized) - ngram_size + 1))
    ]
    unique_ngrams = len(set(ngrams))
    repeated_ratio = (
        1.0 - unique_ngrams / len(ngrams)
        if ngrams
        else 0.0
    )
    cjk_ratio = cjk_count / max(1, len(tokens))
    maximum_rate = 520.0 if cjk_ratio >= 0.35 else 320.0
    reasons: list[str] = []
    if duration_seconds >= 30 and rate > maximum_rate:
        reasons.append(
            f"token_rate_{rate:.1f}_over_{maximum_rate:.0f}_per_minute"
        )
    if len(tokens) >= 80 and repeated_ratio > 0.42:
        reasons.append(f"repeated_8gram_ratio_{repeated_ratio:.3f}")
    if not text.strip():
        reasons.append("empty_text")
    return {
        "status": "invalid" if reasons else "pass",
        "reasons": reasons,
        "token_count": len(tokens),
        "tokens_per_minute": round(rate, 2),
        "cjk_ratio": round(cjk_ratio, 4),
        "repeated_8gram_ratio": round(repeated_ratio, 4),
    }


def _quiet_boundary(
    waveform: np.ndarray,
    sample_rate: int,
    target_seconds: float,
    search_seconds: float,
    *,
    minimum_seconds: float,
    maximum_seconds: float,
) -> float:
    total_samples = int(waveform.shape[0])
    target = int(round(target_seconds * sample_rate))
    left = max(int(round(minimum_seconds * sample_rate)), target - int(search_seconds * sample_rate))
    right = min(int(round(maximum_seconds * sample_rate)), target + int(search_seconds * sample_rate))
    left = max(1, min(total_samples - 1, left))
    right = max(left + 1, min(total_samples - 1, right))
    if right <= left:
        return max(minimum_seconds, min(maximum_seconds, target_seconds))

    # Scan 120 ms windows every 20 ms. A small center-distance penalty avoids
    # selecting a far-away silence when several equally quiet candidates exist.
    samples = np.abs(np.asarray(waveform, dtype=np.float32))
    window = max(1, int(round(0.12 * sample_rate)))
    step = max(1, int(round(0.02 * sample_rate)))
    half = window // 2
    candidates = np.arange(left, right, step, dtype=np.int64)
    starts = np.maximum(0, candidates - half)
    ends = np.minimum(total_samples, starts + window)
    prefix = np.concatenate(([0.0], np.cumsum(samples, dtype=np.float64)))
    energy = (prefix[ends] - prefix[starts]) / np.maximum(1, ends - starts)
    distance = np.abs(candidates - target) / max(1, right - left)
    floor = max(float(np.median(energy)), 1e-8)
    scores = energy / floor + distance * 0.04
    winner = int(candidates[int(np.argmin(scores))])
    return winner / float(sample_rate)


def plan_core_ranges(
    waveform: np.ndarray,
    sample_rate: int,
    *,
    target_seconds: float,
    search_seconds: float = 20.0,
    minimum_chunk_seconds: float = 60.0,
) -> list[AudioRange]:
    """Create exhaustive, non-overlapping ranges with low-energy boundaries."""
    total_seconds = len(waveform) / float(sample_rate)
    if total_seconds <= target_seconds:
        return [AudioRange(0.0, total_seconds)]

    count = max(2, math.ceil(total_seconds / target_seconds))
    ideal = total_seconds / count
    boundaries = [0.0]
    for index in range(1, count):
        target = ideal * index
        remaining = count - index
        # Enforce the requested maximum, even when the quietest point in the
        # search window is far from the ideal boundary.
        lower = max(
            boundaries[-1] + minimum_chunk_seconds,
            total_seconds - remaining * target_seconds,
        )
        upper = min(
            boundaries[-1] + target_seconds,
            total_seconds - remaining * minimum_chunk_seconds,
        )
        target = max(lower, min(upper, target))
        boundary = _quiet_boundary(
            waveform,
            sample_rate,
            target,
            search_seconds,
            minimum_seconds=lower,
            maximum_seconds=upper,
        )
        boundaries.append(boundary)
    boundaries.append(total_seconds)
    return [
        AudioRange(boundaries[index], boundaries[index + 1])
        for index in range(len(boundaries) - 1)
    ]


def add_context_overlap(
    cores: Sequence[AudioRange],
    total_seconds: float,
    overlap_seconds: float,
) -> list[AudioRange]:
    return [
        AudioRange(
            max(0.0, core.start - (overlap_seconds if index else 0.0)),
            min(
                total_seconds,
                core.end + (overlap_seconds if index < len(cores) - 1 else 0.0),
            ),
        )
        for index, core in enumerate(cores)
    ]


def merge_transcript_pair(left: str, right: str) -> tuple[str, dict[str, Any]]:
    """Merge overlapping ASR text around a shared lexical anchor."""
    left_tokens = lexical_tokens(left)
    right_tokens = lexical_tokens(right)
    if not left_tokens:
        return right.strip(), {"status": "right_only", "confidence": 1.0}
    if not right_tokens:
        return left.strip(), {"status": "left_only", "confidence": 1.0}

    left_start = max(0, len(left_tokens) - 500)
    right_end = min(len(right_tokens), 500)
    left_tail = [item.normalized for item in left_tokens[left_start:]]
    right_head = [item.normalized for item in right_tokens[:right_end]]
    matcher = difflib.SequenceMatcher(None, left_tail, right_head, autojunk=False)
    candidates = []
    for block in matcher.get_matching_blocks():
        if block.size <= 0:
            continue
        left_position = left_start + block.a
        # Real overlap belongs near the left suffix and right prefix. Penalize
        # isolated repeated phrases elsewhere in the search windows.
        suffix_distance = len(left_tokens) - (left_position + block.size)
        prefix_distance = block.b
        location_penalty = (suffix_distance + prefix_distance) / max(
            1, len(left_tail) + len(right_head)
        )
        score = block.size - location_penalty * 3.0
        candidates.append((score, block, left_position, suffix_distance, prefix_distance))

    if not candidates:
        return _join_parts(left, right), {
            "status": "no_anchor",
            "confidence": 0.0,
            "matched_tokens": 0,
        }

    _, block, left_position, suffix_distance, prefix_distance = max(
        candidates, key=lambda value: value[0]
    )
    midpoint = max(0, (block.size - 1) // 2)
    left_token = left_tokens[left_position + midpoint]
    right_after = block.b + midpoint + 1
    right_cut = (
        right_tokens[right_after].start if right_after < len(right_tokens) else len(right)
    )
    merged = _join_parts(left[: left_token.end], right[right_cut:])
    location_quality = max(
        0.0,
        1.0
        - (suffix_distance + prefix_distance)
        / max(1, len(left_tail) + len(right_head)),
    )
    confidence = min(1.0, (block.size / 8.0) * 0.65 + location_quality * 0.35)
    status = "merged" if block.size >= 3 and confidence >= 0.45 else "weak_anchor"
    return merged, {
        "status": status,
        "confidence": round(confidence, 4),
        "matched_tokens": int(block.size),
        "left_anchor_token": int(left_position),
        "right_anchor_token": int(block.b),
        "left_suffix_distance": int(suffix_distance),
        "right_prefix_distance": int(prefix_distance),
    }


def merge_with_bridge(left: str, bridge: str, right: str) -> tuple[str, dict[str, Any]]:
    first, first_report = merge_transcript_pair(left, bridge)
    merged, second_report = merge_transcript_pair(first, right)
    success = (
        first_report.get("status") == "merged"
        and second_report.get("status") == "merged"
    )
    return merged, {
        "status": "bridge_merged" if success else "bridge_fallback",
        "confidence": round(
            min(
                float(first_report.get("confidence", 0.0)),
                float(second_report.get("confidence", 0.0)),
            ),
            4,
        ),
        "left_bridge": first_report,
        "bridge_right": second_report,
    }


def _join_parts(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    if left[-1].isspace() or right[0].isspace():
        return left + right
    if right[0] in "，。！？、；：,.!?;:%)]}”’》〉」』":
        return left + right
    if left[-1] in "([{“‘《〈「『":
        return left + right
    if "\u3400" <= left[-1] <= "\u9fff" and "\u3400" <= right[0] <= "\u9fff":
        return left + right
    return left + " " + right


def locate_transcript_spans(
    master: str,
    locator_texts: Sequence[str],
    core_ranges: Sequence[AudioRange],
    total_seconds: float,
) -> tuple[list[int], list[dict[str, Any]]]:
    """Map short locator transcripts into one canonical long transcript.

    Returned boundaries are lexical-token indices and always cover the complete
    master exactly once. Weak matches fall back to duration-proportional cuts.
    """
    master_tokens = lexical_tokens(master)
    master_norm = [item.normalized for item in master_tokens]
    if not master_tokens:
        return [0] * (len(core_ranges) + 1), []

    matches: list[dict[str, Any]] = []
    previous_hint = 0
    for index, (text, core) in enumerate(zip(locator_texts, core_ranges)):
        local_tokens = lexical_tokens(text)
        local_norm = [item.normalized for item in local_tokens]
        expected_start = round(len(master_tokens) * core.start / max(total_seconds, 0.001))
        expected_end = round(len(master_tokens) * core.end / max(total_seconds, 0.001))
        margin = max(80, round(len(master_tokens) * 0.08))
        search_start = max(
            0,
            expected_start - margin,
            previous_hint - max(40, margin // 2),
        )
        search_end = min(len(master_tokens), expected_end + margin)
        if search_end <= search_start:
            search_start = max(0, expected_start - margin)
            search_end = min(len(master_tokens), expected_end + margin)

        matcher = difflib.SequenceMatcher(
            None,
            local_norm,
            master_norm[search_start:search_end],
            autojunk=False,
        )
        blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
        matched = sum(block.size for block in blocks)
        useful = [block for block in blocks if block.size >= 2] or blocks
        if useful and local_norm:
            global_start = search_start + min(block.b for block in useful)
            global_end = search_start + max(block.b + block.size for block in useful)
            coverage = matched / max(1, len(local_norm))
            status = "matched" if coverage >= 0.45 and matched >= 3 else "weak"
        else:
            global_start = expected_start
            global_end = expected_end
            coverage = 0.0
            status = "proportional_fallback"
        global_start = max(0, min(len(master_tokens), global_start))
        global_end = max(global_start, min(len(master_tokens), global_end))
        previous_hint = max(previous_hint, global_end)
        matches.append(
            {
                "index": index,
                "core": core.to_dict(),
                "expected_start_token": expected_start,
                "expected_end_token": expected_end,
                "matched_start_token": global_start,
                "matched_end_token": global_end,
                "matched_tokens": matched,
                "locator_tokens": len(local_norm),
                "coverage": round(coverage, 4),
                "status": status,
            }
        )

    boundaries = [0]
    for index in range(len(matches) - 1):
        left_end = int(matches[index]["matched_end_token"])
        right_start = int(matches[index + 1]["matched_start_token"])
        candidate = round((left_end + right_start) / 2)
        proportional = round(
            len(master_tokens) * core_ranges[index].end / max(total_seconds, 0.001)
        )
        if matches[index]["status"] != "matched" or matches[index + 1]["status"] != "matched":
            candidate = proportional
        minimum = boundaries[-1] + 1
        remaining = len(matches) - index - 1
        maximum = len(master_tokens) - remaining
        boundaries.append(max(minimum, min(maximum, candidate)))
    boundaries.append(len(master_tokens))
    return boundaries, matches


def master_slices(master: str, token_boundaries: Sequence[int]) -> list[str]:
    tokens = lexical_tokens(master)
    if not tokens:
        return [master] if master else []
    slices: list[str] = []
    for index in range(len(token_boundaries) - 1):
        token_start = token_boundaries[index]
        token_end = token_boundaries[index + 1]
        char_start = 0 if token_start <= 0 else tokens[token_start].start
        char_end = len(master) if token_end >= len(tokens) else tokens[token_end].start
        slices.append(master[char_start:char_end].strip())
    return slices


def ranges_as_dict(ranges: Sequence[AudioRange]) -> list[dict[str, float]]:
    return [item.to_dict() for item in ranges]
