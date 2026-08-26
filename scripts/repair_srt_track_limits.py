from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.config import load_settings  # noqa: E402
from substar_core.policy import SubtitlePolicy  # noqa: E402
from substar_core.stage2 import (  # noqa: E402
    Stage2Error,
    call_translation_model,
    classify_source_language,
)


def parse_blocks(text: str) -> list[list[str]]:
    return [
        block.splitlines()
        for block in re.split(r"\r?\n\s*\r?\n", text.strip())
        if block.strip()
    ]


def line_limit(text: str, settings: dict[str, Any]) -> tuple[int, int]:
    policy = SubtitlePolicy.from_settings(settings)
    return policy.line_length(text), policy.hard_limit(text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只压缩双语 SRT 中超过语言硬上限的单行；时间和另一轨锁定"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--thinking-mode", choices=("enabled", "disabled"), default="enabled")
    args = parser.parse_args()

    settings = load_settings(include_secret=True)
    blocks = parse_blocks(args.input.read_text(encoding="utf-8-sig"))
    violations: list[dict[str, Any]] = []
    positions: dict[int, tuple[int, int]] = {}
    for block_position, block in enumerate(blocks):
        if len(block) < 4 or "-->" not in block[1]:
            continue
        cue_id = int(block[0])
        for line_position in range(2, len(block)):
            count, limit = line_limit(block[line_position], settings)
            if count <= limit:
                continue
            positions[cue_id] = (block_position, line_position)
            violations.append(
                {
                    "cue_id": cue_id,
                    "over_limit_line": block[line_position],
                    "language": classify_source_language(block[line_position]),
                    "count": count,
                    "hard_limit": limit,
                    "locked_other_line": block[3 if line_position == 2 else 2],
                    "previous_cue": (
                        blocks[block_position - 1][2:]
                        if block_position > 0
                        else []
                    ),
                    "next_cue": (
                        blocks[block_position + 1][2:]
                        if block_position + 1 < len(blocks)
                        else []
                    ),
                }
            )
    if not violations:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            args.input.read_text(encoding="utf-8-sig"), encoding="utf-8"
        )
        return 0

    prompt = "\n".join(
        [
            "你是双语字幕硬上限终检编辑。",
            "只压缩 over_limit_line，不得改变语言、事实、专名或 cue_id。",
            "locked_other_line、时间轴、相邻 cue 全部只读。",
            "保留原句核心意义，删除重复和非必要赘词；不得把内容转移到相邻 cue。",
            "replacement 必须严格不超过每项 hard_limit，计数包括空格和标点。",
            "只输出 JSON：",
            '{"groups":[{"group_id":"hard-limit-repair","cues":'
            '[{"cue_id":1,"target":"replacement"}]}]}',
        ]
    )
    result, telemetry = call_translation_model(
        base_url=str(settings["translation_api_base_url"]),
        api_key=str(settings["translation_api_key"]),
        model=args.model,
        system_prompt=prompt,
        groups=[{"group_id": "hard-limit-repair", "cues": violations}],
        timeout=600,
        thinking_mode=args.thinking_mode,
        reasoning_effort="max",
        request_attempts=1,
    )
    replacements: dict[int, str] = {}
    for group in result.get("groups", []):
        for cue in group.get("cues", []):
            replacements[int(cue["cue_id"])] = str(cue["target"]).strip()
    if set(replacements) != set(positions):
        raise Stage2Error("硬上限修复未完整返回全部 cue_id")

    changes: list[dict[str, Any]] = []
    for cue_id, replacement in replacements.items():
        block_position, line_position = positions[cue_id]
        before = blocks[block_position][line_position]
        expected_language = classify_source_language(before)
        count, limit = line_limit(replacement, settings)
        if classify_source_language(replacement) != expected_language or count > limit:
            raise Stage2Error(
                f"cue {cue_id} 硬上限修复无效：language/count={count}/{limit}"
            )
        blocks[block_position][line_position] = replacement
        changes.append(
            {
                "cue_id": cue_id,
                "before": before,
                "after": replacement,
                "count": count,
                "limit": limit,
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n\n".join("\n".join(block) for block in blocks) + "\n",
        encoding="utf-8",
    )
    args.output.with_suffix(".hard-limit-repair.json").write_text(
        json.dumps(
            {
                "schema_version": "substar.srt-track-limit-repair.v1",
                "model": args.model,
                "request_attempts": 1,
                "changes": changes,
                "api_call": telemetry,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"changes": changes}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
