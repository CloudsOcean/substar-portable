from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.pipeline import _alignment_tsv, _chatbox_material


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mono_16k(path: Path) -> np.ndarray:
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    if sample_rate != 16000:
        raise ValueError(
            f"说话人旁路要求16 kHz音频，当前为{sample_rate} Hz：{path}"
        )
    return np.ascontiguousarray(mono, dtype=np.float32)


def assign_units(
    alignment: dict[str, Any],
    turns: list[dict[str, Any]],
) -> dict[str, int]:
    assigned = 0
    unknown = 0
    for unit in alignment.get("units", []):
        start = float(unit.get("start", 0))
        end = max(start, float(unit.get("end", start)))
        duration = max(0.04, end - start)
        overlaps: list[tuple[float, str]] = []
        for turn in turns:
            overlap = max(
                0.0,
                min(end, float(turn["end"])) - max(start, float(turn["start"])),
            )
            if overlap > 0:
                overlaps.append((overlap, str(turn["speaker"])))
        if not overlaps:
            unit["speaker_id"] = "speaker_unknown"
            unit["speaker_confidence"] = 0.0
            unknown += 1
            continue
        overlap, speaker = max(overlaps, key=lambda item: item[0])
        unit["speaker_id"] = speaker
        unit["speaker_confidence"] = round(min(1.0, overlap / duration), 4)
        assigned += 1
    return {"assigned_units": assigned, "unknown_units": unknown}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="运行本地说话人分离，并把speaker_id作为旁路元数据写入Alignment"
    )
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--alignment", required=True, type=Path)
    parser.add_argument("--output-alignment", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output-material", type=Path)
    parser.add_argument("--output-tsv", type=Path)
    parser.add_argument(
        "--model",
        default="pyannote/speaker-diarization-community-1",
    )
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--token-env", default="HF_TOKEN")
    parser.add_argument("--min-speakers", type=int)
    parser.add_argument("--max-speakers", type=int)
    args = parser.parse_args()

    token = os.environ.get(args.token_env, "").strip()
    if not token:
        raise RuntimeError(
            f"未设置{args.token_env}。本地pyannote模型下载需要Hugging Face访问令牌；"
            "令牌不会写入Alignment或报告。"
        )

    from whisperx.diarize import DiarizationPipeline

    alignment = read_json(args.alignment)
    audio = mono_16k(args.audio)
    pipeline = DiarizationPipeline(
        model_name=args.model,
        token=token,
        device=args.device,
        cache_dir=str(args.output_alignment.parent / "speaker_model_cache"),
    )
    diarization = pipeline(
        audio,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
    )
    turns = [
        {
            "start": round(float(row.start), 4),
            "end": round(float(row.end), 4),
            "speaker": str(row.speaker),
        }
        for row in diarization.itertuples()
    ]
    stats = assign_units(alignment, turns)
    alignment["speaker_diarization"] = {
        "enabled": True,
        "backend": "whisperx.pyannote_sidecar",
        "model": args.model,
        "modifies_transcript": False,
        "modifies_word_timing": False,
        "turns": turns,
        **stats,
    }
    write_json(args.output_alignment, alignment)
    if args.output_tsv:
        args.output_tsv.parent.mkdir(parents=True, exist_ok=True)
        args.output_tsv.write_text(
            _alignment_tsv(alignment.get("units", [])),
            encoding="utf-8",
        )
    if args.output_material:
        args.output_material.parent.mkdir(parents=True, exist_ok=True)
        master = str(alignment.get("master_text", "")).strip()
        if not master:
            raise RuntimeError("Alignment缺少master_text，无法生成聊天框原料")
        args.output_material.write_text(
            _chatbox_material(master, alignment),
            encoding="utf-8",
        )
    write_json(
        args.report,
        {
            "schema_version": "substar.speaker-diarization-report.v1",
            "audio": str(args.audio.resolve()),
            "alignment": str(args.alignment.resolve()),
            "output_alignment": str(args.output_alignment.resolve()),
            "model": args.model,
            "device": args.device,
            "speaker_count": len({item["speaker"] for item in turns}),
            "turn_count": len(turns),
            **stats,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
