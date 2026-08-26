from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_stage1_pipeline import read  # noqa: E402
from substar_core.stage1 import extract_alignment  # noqa: E402


def srt_time(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 Stage1 计划导出为仅供边界审计的双行 SRT"
    )
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--draft", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    units = {int(item.index): item for item in extract_alignment(read(args.material))}
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    groups = plan.get("groups", [])
    blocks = [
        block.strip().replace("\n", " ")
        for block in args.draft.read_text(encoding="utf-8-sig").split("\n\n")
        if block.strip()
    ]
    if len(groups) != len(blocks):
        raise ValueError(
            f"计划与草案 cue 数不一致：groups={len(groups)} draft={len(blocks)}"
        )

    rendered: list[str] = []
    for cue_id, (group, source) in enumerate(zip(groups, blocks), start=1):
        start_index = int(group["alignment_start"])
        end_index = int(group["alignment_end"])
        start = float(units[start_index].start)
        end = float(units[end_index].end)
        rendered.extend(
            [
                str(cue_id),
                f"{srt_time(start)} --> {srt_time(end)}",
                source,
                "审计占位",
                "",
            ]
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(rendered), encoding="utf-8")
    print(f"exported cues={len(groups)} output={args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
