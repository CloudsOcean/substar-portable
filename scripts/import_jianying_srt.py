from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.config import load_settings  # noqa: E402
from substar_core.model_paths import resolve_local_model_path  # noqa: E402
from substar_core.recognition.registry import profile_settings  # noqa: E402
from substar_core.pipeline import (  # noqa: E402
    _alignment_tsv,
    _chatbox_material,
    _sha256,
    extract_audio,
    probe_media,
)
from substar_core.qwen_backend import (  # noqa: E402
    _choose_alignment_language,
    _configure_model_download,
    _device_kwargs,
    _serialize_items,
)


TIME_RE = re.compile(
    r"^(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2})[,.](?P<sms>\d{3})"
    r"\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2})[,.](?P<ems>\d{3})$"
)
FALLBACK_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|[\u3400-\u9fff]",
    flags=re.UNICODE,
)


@dataclass(frozen=True)
class SourceCue:
    cue_id: int
    start: float
    end: float
    text: str


def parse_time(match: re.Match[str], prefix: str) -> float:
    return (
        int(match.group(prefix + "h")) * 3600
        + int(match.group(prefix + "m")) * 60
        + int(match.group(prefix + "s"))
        + int(match.group(prefix + "ms")) / 1000
    )


def parse_srt(path: Path) -> list[SourceCue]:
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    cues: list[SourceCue] = []
    for block_number, block in enumerate(blocks, start=1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            raise ValueError(f"SRT 第 {block_number} 块不足三行")
        try:
            cue_id = int(lines[0])
        except ValueError as exc:
            raise ValueError(f"SRT 第 {block_number} 块编号无效") from exc
        match = TIME_RE.match(lines[1])
        if match is None:
            raise ValueError(f"SRT 第 {block_number} 块时间码无效：{lines[1]}")
        start = parse_time(match, "s")
        end = parse_time(match, "e")
        if end <= start:
            raise ValueError(f"SRT 第 {block_number} 块结束时间不晚于开始时间")
        body = " ".join(lines[2:]).strip()
        if not body:
            raise ValueError(f"SRT 第 {block_number} 块正文为空")
        cues.append(SourceCue(cue_id=cue_id, start=start, end=end, text=body))
    if [cue.cue_id for cue in cues] != list(range(1, len(cues) + 1)):
        raise ValueError("SRT cue 编号必须从1连续递增")
    for left, right in zip(cues, cues[1:]):
        if right.start < left.start:
            raise ValueError(f"SRT cue {right.cue_id} 开始时间倒退")
    return cues


def fallback_units(cue: SourceCue) -> list[dict[str, Any]]:
    tokens = [match.group(0) for match in FALLBACK_TOKEN_RE.finditer(cue.text)]
    if not tokens:
        return []
    weights = [max(1, len(token)) for token in tokens]
    total = sum(weights)
    duration = cue.end - cue.start
    cursor = cue.start
    output: list[dict[str, Any]] = []
    remaining_weight = total
    for position, (token, weight) in enumerate(zip(tokens, weights)):
        if position == len(tokens) - 1:
            end = cue.end
        else:
            allocated = duration * weight / remaining_weight
            end = min(cue.end, cursor + allocated)
        output.append(
            {
                "text": token,
                "start": round(cursor, 3),
                "end": round(max(cursor, end), 3),
                "kind": (
                    "character"
                    if len(token) == 1 and re.search(r"[\u3400-\u9fff]", token)
                    else "word"
                ),
            }
        )
        duration -= max(0.0, end - cursor)
        remaining_weight -= weight
        cursor = end
    return output


def normalize_units(
    cue: SourceCue,
    items: list[dict[str, Any]],
    *,
    timing_source: str,
) -> list[dict[str, Any]]:
    if not items:
        return []
    items = [dict(item) for item in items if str(item.get("text", "")).strip()]
    if not items:
        return []
    previous = cue.start
    for item in items:
        acoustic_start = max(cue.start, min(cue.end, float(item["start"])))
        acoustic_end = max(acoustic_start, min(cue.end, float(item["end"])))
        acoustic_start = max(previous, acoustic_start)
        acoustic_end = max(acoustic_start, acoustic_end)
        item["acoustic_start"] = round(acoustic_start, 3)
        item["acoustic_end"] = round(acoustic_end, 3)
        item["start"] = round(acoustic_start, 3)
        item["end"] = round(acoustic_end, 3)
        previous = acoustic_start
    # The user has designated Jianying's cue boundaries as authoritative.
    # Qwen estimates only the internal word positions.
    items[0]["start"] = round(cue.start, 3)
    items[-1]["end"] = round(cue.end, 3)
    for position, item in enumerate(items):
        item.update(
            {
                "source_cue_id": cue.cue_id,
                "source_cue_start": round(cue.start, 3),
                "source_cue_end": round(cue.end, 3),
                "source_cue_text": cue.text,
                "source_cue_first": position == 0,
                "source_cue_last": position == len(items) - 1,
                "sentence_id": cue.cue_id,
                "sentence_start": position == 0,
                "sentence_end": position == len(items) - 1,
                "timing_source": timing_source,
            }
        )
    return items


def call_aligner(
    aligner: Any,
    entries: list[tuple[SourceCue, Any, str]],
    *,
    sample_rate: int,
) -> list[tuple[SourceCue, list[dict[str, Any]], str, str]]:
    if not entries:
        return []
    try:
        results = aligner.align(
            audio=[(audio, sample_rate) for _, audio, _ in entries],
            text=[cue.text for cue, _, _ in entries],
            language=[language for _, _, language in entries],
        )
        if len(results) != len(entries):
            raise RuntimeError(
                f"Qwen 返回数量 {len(results)} 与请求 {len(entries)} 不同"
            )
        output: list[tuple[SourceCue, list[dict[str, Any]], str, str]] = []
        for (cue, _, language), result in zip(entries, results):
            serialized = _serialize_items(result, offset=cue.start)
            output.append((cue, serialized, language, "qwen_forced_aligner"))
        return output
    except Exception as exc:
        if len(entries) > 1:
            middle = len(entries) // 2
            return call_aligner(
                aligner,
                entries[:middle],
                sample_rate=sample_rate,
            ) + call_aligner(
                aligner,
                entries[middle:],
                sample_rate=sample_rate,
            )
        cue, _, language = entries[0]
        return [
            (
                cue,
                fallback_units(cue),
                language,
                f"jianying_proportional_fallback:{type(exc).__name__}",
            )
        ]


def lexical_sequence(value: str) -> list[str]:
    return [
        match.group(0).casefold().replace("’", "'")
        for match in FALLBACK_TOKEN_RE.finditer(value)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="以剪映SRT为唯一主稿和外层时间锚点，逐cue执行Qwen强制对齐"
    )
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=8, choices=range(1, 33))
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    settings = load_settings(include_secret=False)
    snapshot_path = args.output_dir / "workbench_task_settings.json"
    if snapshot_path.is_file():
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            overrides = snapshot.get("settings_overrides", {})
            if isinstance(overrides, dict):
                settings.update(overrides)
        except (OSError, json.JSONDecodeError):
            pass
    settings = profile_settings(
        {**settings, "recognition_profile_id": "jianying_qwen3"}
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = args.output_dir / "alignment_cues"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    cues = parse_srt(args.srt)
    media = probe_media(args.video)
    if cues[-1].end > float(media["duration_seconds"]) + 0.25:
        raise ValueError("剪映SRT末时间超过视频时长")

    wav_path = args.output_dir / "audio_16k_mono.wav"
    if not wav_path.exists():
        print("extract_audio", flush=True)
        extract_audio(args.video, wav_path, denoise_mode="off")

    _configure_model_download(settings)
    try:
        from qwen_asr import Qwen3ForcedAligner
        from qwen_asr.inference.utils import SAMPLE_RATE, normalize_audios
    except ImportError as exc:
        raise RuntimeError("尚未安装 qwen-asr，请先运行安装依赖.cmd") from exc

    waveform = normalize_audios(str(wav_path))[0]
    sample_rate = SAMPLE_RATE
    aligner_path = resolve_local_model_path(
        str(settings.get("qwen_aligner_model_path", "")),
        str(settings.get("model_cache_dir", "")),
        str(settings["qwen_aligner_model"]),
        required_files=("config.json", "model.safetensors"),
    )
    aligner_reference = str(aligner_path or settings["qwen_aligner_model"])
    aligner = Qwen3ForcedAligner.from_pretrained(
        aligner_reference,
        **_device_kwargs(settings),
    )

    results_by_id: dict[int, dict[str, Any]] = {}
    pending: list[tuple[SourceCue, Any, str]] = []
    for cue in cues:
        checkpoint = checkpoint_dir / f"cue_{cue.cue_id:04d}.json"
        if args.resume and checkpoint.exists():
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (
                value.get("source_text") == cue.text
                and math.isclose(float(value.get("start", -1)), cue.start)
                and math.isclose(float(value.get("end", -1)), cue.end)
            ):
                results_by_id[cue.cue_id] = value
                continue
        start_sample = max(0, int(round(cue.start * sample_rate)))
        end_sample = min(len(waveform), int(round(cue.end * sample_rate)))
        audio = waveform[start_sample:end_sample]
        language = _choose_alignment_language(cue.text, "", "Auto")
        pending.append((cue, audio, language))

    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset : offset + args.batch_size]
        aligned = call_aligner(aligner, batch, sample_rate=sample_rate)
        for cue, raw_units, language, timing_source in aligned:
            units = normalize_units(
                cue,
                raw_units,
                timing_source=timing_source,
            )
            if not units:
                units = normalize_units(
                    cue,
                    fallback_units(cue),
                    timing_source="jianying_proportional_fallback:empty",
                )
            value = {
                "schema_version": "substar.jianying-cue-alignment.v1",
                "cue_id": cue.cue_id,
                "start": cue.start,
                "end": cue.end,
                "source_text": cue.text,
                "language": language,
                "timing_source": timing_source,
                "units": units,
            }
            results_by_id[cue.cue_id] = value
            (checkpoint_dir / f"cue_{cue.cue_id:04d}.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        completed = min(offset + len(batch), len(pending))
        print(f"aligned={completed}/{len(pending)}", flush=True)

    all_units: list[dict[str, Any]] = []
    cue_records: list[dict[str, Any]] = []
    fallback_cues: list[int] = []
    mismatch_cues: list[dict[str, Any]] = []
    for cue in cues:
        value = results_by_id[cue.cue_id]
        units = [dict(item) for item in value["units"]]
        if "fallback" in str(value["timing_source"]):
            fallback_cues.append(cue.cue_id)
        aligned_text = " ".join(str(item["text"]) for item in units)
        expected_tokens = lexical_sequence(cue.text)
        actual_tokens = lexical_sequence(aligned_text)
        if expected_tokens != actual_tokens:
            mismatch_cues.append(
                {
                    "cue_id": cue.cue_id,
                    "source_text": cue.text,
                    "aligned_text": aligned_text,
                    "expected_tokens": expected_tokens,
                    "aligned_tokens": actual_tokens,
                }
            )
        start_index = len(all_units)
        for item in units:
            item["index"] = len(all_units)
            all_units.append(item)
        cue_records.append(
            {
                "cue_id": cue.cue_id,
                "start": cue.start,
                "end": cue.end,
                "text": cue.text,
                "language": value["language"],
                "alignment_start": start_index,
                "alignment_end": len(all_units) - 1,
                "unit_count": len(units),
                "timing_source": value["timing_source"],
            }
        )

    master = " ".join(cue.text for cue in cues)
    alignment = {
        "schema_version": "substar.alignment.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "media": {
            "original_name": args.video.name,
            "sha256": _sha256(args.video),
            **media,
        },
        "engines": {
            "profile_id": "jianying_qwen3",
            "transcript": "jianying:srt-import",
            "alignment": aligner_reference,
            "diarization": None,
        },
        "language": ",".join(dict.fromkeys(item["language"] for item in cue_records)),
        "master_text": master,
        "chunks": cue_records,
        "units": all_units,
        "timing_policy": {
            "source_cue_boundaries": "authoritative",
            "internal_units": "qwen_forced_aligner",
            "display_cue_end": "next_output_cue_first_unit_start",
            "last_output_cue_end": "source_cue_end",
        },
    }
    (args.output_dir / "master_transcript.txt").write_text(
        master + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "alignment.json").write_text(
        json.dumps(alignment, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.output_dir / "alignment.tsv").write_text(
        _alignment_tsv(all_units),
        encoding="utf-8",
    )
    (args.output_dir / "chatbox_material.md").write_text(
        _chatbox_material(master, alignment),
        encoding="utf-8",
    )
    shutil.copy2(args.srt, args.output_dir / "jianying_source.srt")
    audit = {
        "schema_version": "substar.jianying-import-audit.v1",
        "status": (
            "pass"
            if not fallback_cues and not mismatch_cues
            else "review"
        ),
        "source_cue_count": len(cues),
        "alignment_unit_count": len(all_units),
        "fallback_cues": fallback_cues,
        "text_mismatch_cues": mismatch_cues,
        "first_start": cues[0].start,
        "last_end": cues[-1].end,
        "media_duration": media["duration_seconds"],
        "time_monotonic": all(
            float(right["start"]) >= float(left["start"])
            for left, right in zip(all_units, all_units[1:])
        ),
        "boundary_policy": alignment["timing_policy"],
    }
    (args.output_dir / "jianying_import_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "substar.run.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_file": args.video.name,
        "source_srt": str(args.srt.resolve()),
        "transcript_source": "jianying_srt",
        "transcript_engine": "jianying:srt-import",
        "alignment_engine": settings["qwen_aligner_model"],
        "alignment_unit_count": len(all_units),
        "source_cue_count": len(cues),
        "configuration": {
            "batch_size": args.batch_size,
            "denoise": "off",
            "cross_asr_review": False,
        },
    }
    (args.output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
