from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_flash_map_pro_editor import (  # noqa: E402
    PROMPTS,
    build_p1_windows,
    normalize_p1,
    protection_for_editor,
    render_p1_window_payload,
)
from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def stage_schema(stage: str) -> dict[str, Any]:
    if stage == "p1":
        return {
            "schema_version": "substar.stage1.protection.v1",
            "required": [
                "schema_version",
                "spans",
                "preferred_breaks_after",
                "forbidden_breaks_after",
                "coverage_check",
            ],
            "output_file": "result/p1.json",
            "window_output_pattern": "result/windows/window_NNN.json",
        }
    if stage == "p2":
        return {
            "schema_version": "substar.stage1.meaning-groups.v1",
            "required": [
                "schema_version",
                "groups",
                "protection_conflicts",
            ],
            "output_file": "result/p2.json",
        }
    return {
        "schema_version": "substar.stage1.local-candidates.v1",
        "required": [
            "schema_version",
            "candidates",
            "selected_candidate_id",
            "uncertain_boundaries",
            "forced_by_hard_limit",
        ],
        "output_file": "result/p3.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="构建不含人工答案和历史输出的Sol阶段白名单输入包"
    )
    parser.add_argument("--stage", choices=["p1", "p2", "p3"], required=True)
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--upstream-p1", type=Path)
    parser.add_argument("--upstream-p2", type=Path)
    parser.add_argument("--core-units", type=int, default=200)
    parser.add_argument("--overlap-units", type=int, default=48)
    args = parser.parse_args()

    if args.stage in {"p2", "p3"} and not args.upstream_p1:
        parser.error("P2/P3必须提供 --upstream-p1")
    if args.stage == "p3" and not args.upstream_p2:
        parser.error("P3必须提供 --upstream-p2")

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    material = args.material.read_text(encoding="utf-8-sig")
    master = extract_master(material)
    units = extract_alignment(material)
    prompt_source = PROMPTS[args.stage]

    task_path = output / "01_task.md"
    prompt_text = prompt_source.read_text(encoding="utf-8-sig")
    stage_instruction = {
        "p1": (
            "按 06_windows.json 的顺序串行处理每个窗口，并把每窗原始JSON写入"
            " result/windows/window_NNN.json。随后只按head_index核心所有权合并、"
            "去重和重新编号，写出覆盖全片的 result/p1.json。不得省略窗口。"
        ),
        "p2": (
            "读取完整主稿、alignment和06_upstream_p1.json，一次形成覆盖全片的"
            "唯一意义组计划，写入 result/p2.json。不要安排显示cue。"
        ),
        "p3": (
            "读取完整主稿、alignment、P1和冻结P2，覆盖P2全部group生成全片显示"
            "切分候选并选择主方案，写入 result/p3.json。不得改变P2组界。"
        ),
    }[args.stage]
    task_path.write_text(
        "\n\n".join(
            [
                prompt_text,
                "# BLIND_EXECUTION_CONTRACT",
                stage_instruction,
                (
                    "只读取 00_manifest.json 中 allowed_read_paths 列出的文件。"
                    "禁止遍历父目录、workspace、历史输出或网络。不得寻找参考字幕、"
                    "人工答案、其他路线结果或旧评分。只把最终JSON写入允许的输出路径。"
                    "不要保存思维链。"
                ),
            ]
        ),
        encoding="utf-8",
    )
    (output / "03_master_transcript.txt").write_text(master, encoding="utf-8")
    alignment_path = output / "04_alignment.jsonl"
    alignment_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "index": int(unit.index),
                    "start": float(unit.start),
                    "end": float(unit.end),
                    "text": str(unit.text),
                    "sentence_id": unit.sentence_id,
                    "sentence_start": bool(unit.sentence_start),
                    "sentence_end": bool(unit.sentence_end),
                },
                ensure_ascii=False,
            )
            for unit in units
        )
        + "\n",
        encoding="utf-8",
    )
    write_json(output / "05_output_schema.json", stage_schema(args.stage))

    allowed_read = [
        "00_manifest.json",
        "01_task.md",
        "03_master_transcript.txt",
        "04_alignment.jsonl",
        "05_output_schema.json",
    ]
    allowed_write = [stage_schema(args.stage)["output_file"], "provenance.json"]

    if args.stage == "p1":
        windows = build_p1_windows(
            units,
            core_units=args.core_units,
            overlap_units=args.overlap_units,
        )
        write_json(output / "06_windows.json", {"windows": windows})
        allowed_read.append("06_windows.json")
        window_dir = output / "windows"
        window_dir.mkdir(exist_ok=True)
        for number, window in enumerate(windows, start=1):
            path = window_dir / f"window_{number:03d}.md"
            path.write_text(
                render_p1_window_payload(units, window), encoding="utf-8"
            )
            allowed_read.append(path.relative_to(output).as_posix())
            allowed_write.append(
                f"result/windows/window_{number:03d}.json"
            )
    if args.upstream_p1:
        target = output / "06_upstream_p1.json"
        upstream_p1 = normalize_p1(
            json.loads(args.upstream_p1.read_text(encoding="utf-8-sig")),
            units,
            require_coverage=True,
        )
        if args.stage == "p2":
            upstream_p1 = protection_for_editor(upstream_p1)
        write_json(target, upstream_p1)
        allowed_read.append(target.name)
    if args.upstream_p2:
        target = output / "07_upstream_p2.json"
        shutil.copy2(args.upstream_p2, target)
        allowed_read.append(target.name)

    write_json(
        output / "02_policy.json",
        {
            "examples": "synthetic_only",
            "source_text_mutation": False,
            "english_hard_limit": 55,
            "english_count_spaces": True,
            "english_count_punctuation": True,
            "human_reference_available": False,
        },
    )
    allowed_read.append("02_policy.json")
    allowed_read = sorted(set(allowed_read))
    allowed_write = sorted(set(allowed_write))
    manifest = {
        "schema_version": "substar.sol-blind-package.v1",
        "anonymous_run_id": output.name,
        "stage": args.stage,
        "allowed_read_paths": allowed_read,
        "allowed_output_paths": allowed_write,
        "forbidden_actions": [
            "read_parent_directory",
            "search_workspace",
            "read_human_reference",
            "read_other_route",
            "network_or_api",
            "modify_source_or_upstream",
        ],
    }
    write_json(output / "00_manifest.json", manifest)
    manifest["input_sha256"] = {
        relative: sha256(output / relative)
        for relative in allowed_read
        if relative != "00_manifest.json"
    }
    write_json(output / "00_manifest.json", manifest)
    print(f"prepared stage={args.stage} dir={output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
