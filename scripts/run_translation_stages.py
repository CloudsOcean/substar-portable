from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.process_command import python_script_command


def finalize_existing_outputs(
    *,
    output_dir: Path,
    models: dict[str, str],
    job_dir: Path,
    stage1_dir: Path,
) -> dict:
    t1_srt = output_dir / "T1" / "substar_bilingual.srt"
    if not t1_srt.exists():
        raise RuntimeError("缺少现有 T1/T2 映射字幕，无法定稿")
    final_srt = output_dir / "substar_bilingual_final.srt"
    shutil.copy2(t1_srt, final_srt)
    manifest = {
        "schema_version": "substar.translation-stages.v2",
        "models": models,
        "job_dir": str(job_dir.resolve()),
        "stage1_dir": str(stage1_dir.resolve()),
        "T1": str(t1_srt.resolve()),
        "T2": str(t1_srt.resolve()),
        "final": str(final_srt.resolve()),
        "final_stage": "T2_atom_assembly",
        "risk_review": "editor_ai_correction",
    }
    (output_dir / "translation_stages_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def run_stage(name: str, command: list[str], output_dir: Path) -> None:
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    (output_dir / f"{name}.stdout.log").write_text(
        result.stdout, encoding="utf-8"
    )
    (output_dir / f"{name}.stderr.log").write_text(
        result.stderr, encoding="utf-8"
    )
    if result.returncode:
        raise RuntimeError(
            f"{name} 失败（exit={result.returncode}）：{result.stderr[-1000:]}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="执行 T1 目标语语义原子翻译、T2 固定 Cue 原子组装"
    )
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--stage1-dir", required=True, type=Path)
    parser.add_argument("--project-store-dir", required=True, type=Path)
    parser.add_argument("--expected-revision-id")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--progress-file",
        type=Path,
        help="共享 T1/T2 进度账本；由子阶段写入，供 V2 状态面板读取",
    )
    parser.add_argument("--model", help="作为 T1/T2 两个阶段的共同模型")
    parser.add_argument("--t1-model")
    parser.add_argument("--t2-model")
    parser.add_argument("--t1-thinking-mode", choices=["enabled", "disabled"], default="disabled")
    parser.add_argument("--t2-thinking-mode", choices=["enabled", "disabled"], default="disabled")
    parser.add_argument("--t1-reasoning-effort", choices=["low", "medium", "high", "max", "xhigh"], default="high")
    parser.add_argument("--t2-reasoning-effort", choices=["low", "medium", "high", "max", "xhigh"], default="high")
    parser.add_argument("--t1-max-tokens", type=int, default=65536)
    parser.add_argument("--t2-max-tokens", type=int, default=131072)
    parser.add_argument("--t1-temperature", type=float, default=1.3)
    parser.add_argument("--t2-temperature", type=float, default=0.0)
    parser.add_argument("--workers", type=int, default=64)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--repair-attempts",
        type=int,
        choices=range(0, 4),
        default=2,
        help="T1/T2 契约或响应不完整时的阶段级重试次数",
    )
    parser.add_argument(
        "--finalize-only",
        action="store_true",
        help="不调用API，仅根据已有T1/T2产物定稿",
    )
    args = parser.parse_args()
    t1_model = args.t1_model or args.model
    t2_model = args.t2_model or args.model
    if not all((t1_model, t2_model)):
        parser.error("必须提供 --model，或分别提供 --t1-model/--t2-model")
    models = {
        "T1": str(t1_model),
        "T2": str(t2_model),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    t1_dir = args.output_dir / "T1"
    t1_dir.mkdir(parents=True, exist_ok=True)
    if args.finalize_only:
        manifest = finalize_existing_outputs(
            output_dir=args.output_dir,
            models=models,
            job_dir=args.job_dir,
            stage1_dir=args.stage1_dir,
        )
        print(f"complete final={manifest['final']}", flush=True)
        return 0

    resume = ["--resume"] if args.resume else []
    t1 = python_script_command(
        "scripts/run_stage2_pipeline.py",
        "--job-dir",
        str(args.job_dir),
        "--stage1-dir",
        str(args.stage1_dir),
        "--project-store-dir",
        str(args.project_store_dir),
        *(
            ["--expected-revision-id", args.expected_revision_id]
            if args.expected_revision_id
            else []
        ),
        "--output-dir",
        str(t1_dir),
        "--workers",
        str(args.workers),
        "--repair-attempts",
        str(args.repair_attempts),
        "--translation-model",
        str(t1_model),
        "--mapping-model",
        str(t2_model),
        "--translation-thinking-mode",
        args.t1_thinking_mode,
        "--mapping-thinking-mode",
        args.t2_thinking_mode,
        "--translation-reasoning-effort",
        args.t1_reasoning_effort,
        "--mapping-reasoning-effort",
        args.t2_reasoning_effort,
        "--translation-max-tokens",
        str(args.t1_max_tokens),
        "--mapping-max-tokens",
        str(args.t2_max_tokens),
        "--translation-temperature",
        str(args.t1_temperature),
        "--mapping-temperature",
        str(args.t2_temperature),
        *(["--progress-file", str(args.progress_file)] if args.progress_file else []),
        *resume,
    )
    run_stage("T1", t1, args.output_dir)
    t1_srt = t1_dir / "substar_bilingual.srt"
    if not t1_srt.exists():
        raise RuntimeError("T1 未生成 substar_bilingual.srt")

    manifest = finalize_existing_outputs(
        output_dir=args.output_dir,
        models=models,
        job_dir=args.job_dir,
        stage1_dir=args.stage1_dir,
    )
    print(f"complete final={manifest['final']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
