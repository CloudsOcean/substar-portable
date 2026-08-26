from __future__ import annotations

from array import array
import math
from pathlib import Path
import sys
import threading
import wave
from collections import OrderedDict
from typing import Any, Hashable, Iterable, Mapping


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _window_rms(
    source: wave.Wave_read,
    *,
    start: float,
    end: float,
    frame_seconds: float = 0.01,
) -> tuple[list[float], float]:
    rate = source.getframerate()
    channels = source.getnchannels()
    first_frame = max(0, int(math.floor(start * rate)))
    last_frame = min(source.getnframes(), int(math.ceil(end * rate)))
    source.setpos(first_frame)
    samples = array("h")
    samples.frombytes(source.readframes(max(0, last_frame - first_frame)))
    if sys.byteorder != "little":
        samples.byteswap()
    samples_per_bucket = max(1, int(round(rate * frame_seconds))) * channels
    values: list[float] = []
    for offset in range(0, len(samples) - samples_per_bucket + 1, samples_per_bucket):
        bucket = samples[offset : offset + samples_per_bucket]
        mean_square = sum(float(value) * float(value) for value in bucket) / len(bucket)
        values.append(math.sqrt(mean_square) / 32768.0)
    if len(values) >= 3:
        raw_values = values
        values = [
            sum(raw_values[max(0, index - 1) : min(len(raw_values), index + 2)])
            / len(raw_values[max(0, index - 1) : min(len(raw_values), index + 2)])
            for index in range(len(raw_values))
        ]
    return values, first_frame / rate


def smart_forward_snap(
    audio: Path,
    cue_starts: Iterable[Mapping[str, Any]],
    *,
    search_window_ms: int = 1000,
    pre_roll_ms: int = 40,
) -> dict[str, Any]:
    """Return trustworthy local speech onsets with a small leading cushion."""

    search_seconds = max(0.1, min(1.0, int(search_window_ms) / 1000.0))
    pre_roll_seconds = max(0.0, min(0.1, int(pre_roll_ms) / 1000.0))
    prepared = [
        {
            "cue_id": str(item.get("cue_id", "")),
            "start": float(item.get("start", 0.0)),
            "minimum_start": max(0.0, float(item.get("minimum_start", 0.0))),
        }
        for item in cue_starts
        if str(item.get("cue_id", "")).strip()
    ]
    changes: list[dict[str, Any]] = []
    with wave.open(str(audio), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("smart forward snap requires 16-bit PCM")
        duration = source.getnframes() / source.getframerate()
        for item in prepared:
            original = min(duration, max(0.0, item["start"]))
            minimum = min(original, item["minimum_start"])
            window_start = max(minimum, original - search_seconds)
            if original - window_start < 0.04:
                continue
            rms, actual_start = _window_rms(
                source,
                start=window_start,
                end=min(duration, original + 0.3),
            )
            original_index = min(
                len(rms), max(0, int(round((original - actual_start) / 0.01)))
            )
            before = rms[:original_index]
            post = rms[original_index : min(len(rms), original_index + 30)]
            if len(post) < 5:
                continue
            signal_level = _percentile(post, 0.75)
            noise_floor = _percentile(before, 0.25)
            if signal_level < 0.002:
                continue
            threshold = max(
                0.002,
                min(
                    signal_level * 0.45,
                    max(noise_floor * 2.2, signal_level * 0.12),
                ),
            )
            candidates: list[tuple[int, float]] = []
            for index in range(4, min(original_index + 1, len(rms) - 6)):
                quiet = rms[index - 4 : index]
                voiced = rms[index : index + 6]
                quiet_ratio = sum(value < threshold for value in quiet) / len(quiet)
                voiced_ratio = sum(value >= threshold for value in voiced) / len(voiced)
                quiet_mean = sum(quiet) / len(quiet)
                voiced_mean = sum(voiced) / len(voiced)
                contrast = voiced_mean / max(quiet_mean, 0.0001)
                if (
                    quiet_ratio >= 0.5
                    and voiced_ratio >= 2 / 3
                    and voiced_mean >= threshold
                    and contrast >= 1.5
                ):
                    confidence = min(
                        1.0,
                        0.4 * quiet_ratio
                        + 0.35 * voiced_ratio
                        + 0.25 * min(contrast / 4, 1),
                    )
                    candidates.append((index, confidence))
            if not candidates:
                continue
            index, confidence = max(candidates, key=lambda item: (item[1], item[0]))
            onset = actual_start + index * 0.01
            snapped = max(minimum, onset - pre_roll_seconds)
            offset_ms = round((original - snapped) * 1000)
            if offset_ms < 15:
                continue
            changes.append(
                {
                    "cue_id": item["cue_id"],
                    "original_start": round(original, 3),
                    "snapped_start": round(snapped, 3),
                    "offset_ms": offset_ms,
                    "confidence": round(confidence, 3),
                }
            )
    return {
        "schema_version": "substar.smart-forward-snap.v1",
        "search_window_ms": round(search_seconds * 1000),
        "pre_roll_ms": round(pre_roll_seconds * 1000),
        "analyzed": len(prepared),
        "changes": changes,
    }


class WaveformWindowCache:
    """Small process-local LRU for derived waveform window responses."""

    def __init__(self, limit: int = 64):
        self.limit = max(1, int(limit))
        self._lock = threading.RLock()
        self._values: OrderedDict[Hashable, dict[str, Any]] = OrderedDict()

    def get(self, key: Hashable) -> dict[str, Any] | None:
        with self._lock:
            value = self._values.get(key)
            if value is None:
                return None
            self._values.move_to_end(key)
            return {**value, "peaks": list(value.get("peaks", []))}

    def put(self, key: Hashable, value: dict[str, Any]) -> None:
        with self._lock:
            self._values[key] = {**value, "peaks": list(value.get("peaks", []))}
            self._values.move_to_end(key)
            while len(self._values) > self.limit:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


WAVEFORM_WINDOW_CACHE = WaveformWindowCache()
