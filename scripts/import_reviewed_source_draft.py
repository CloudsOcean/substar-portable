from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.stage1 import extract_alignment, extract_master, split_groups
from substar_core.stage1_chunking import _unit_original_ranges


TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:[’'][A-Za-z0-9]+)*|[\u3400-\u9fff]",
    flags=re.UNICODE,
)


def tokens_with_spans(text: str) -> list[tuple[str, int, int]]:
    return [
        (match.group(0).casefold().replace("’", "'"), match.start(), match.end())
        for match in TOKEN_RE.finditer(text)
    ]


def reviewed_structure(
    text: str,
) -> tuple[list[list[str]], list[tuple[str, int, int, int, int]]]:
    blocks = split_groups(text)
    rows: list[tuple[str, int, int, int, int]] = []
    token_cursor = 0
    for block_number, block in enumerate(blocks):
        for line_number, line in enumerate(block):
            count = len(tokens_with_spans(line))
            if not count:
                raise ValueError(
                    f"人工稿第 {block_number + 1} 组第 {line_number + 1} 行没有可映射文字"
                )
            rows.append(
                (
                    line,
                    block_number,
                    line_number,
                    token_cursor,
                    token_cursor + count - 1,
                )
            )
            token_cursor += count
    return blocks, rows


def sequence_mapping(
    master_tokens: list[tuple[str, int, int]],
    reviewed_tokens: list[tuple[str, int, int]],
) -> tuple[dict[int, int], list[dict[str, Any]]]:
    left = [item[0] for item in master_tokens]
    right = [item[0] for item in reviewed_tokens]
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    mapping: dict[int, int] = {}
    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(j2 - j1):
                mapping[j1 + offset] = i1 + offset
            continue
        changes.append(
            {
                "type": tag,
                "master_token_start": i1,
                "master_token_end": i2 - 1,
                "reviewed_token_start": j1,
                "reviewed_token_end": j2 - 1,
                "master_text": " ".join(left[i1:i2]),
                "reviewed_text": " ".join(right[j1:j2]),
            }
        )
        if tag == "replace":
            source_count = max(1, i2 - i1)
            target_count = max(1, j2 - j1)
            for offset in range(j2 - j1):
                source_offset = min(
                    source_count - 1,
                    int(offset * source_count / target_count),
                )
                mapping[j1 + offset] = i1 + source_offset
        elif tag == "insert":
            anchor = i1 - 1 if i1 > 0 else min(i1, len(left) - 1)
            for target in range(j1, j2):
                mapping[target] = anchor
    return mapping, changes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把无标记的人工源文稿映射回 alignment 并冻结为 Stage1 计划"
    )
    parser.add_argument("--reviewed", required=True, type=Path)
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    reviewed = args.reviewed.read_text(encoding="utf-8-sig")
    material = args.material.read_text(encoding="utf-8")
    master = extract_master(material)
    units = extract_alignment(material)
    unit_ranges = _unit_original_ranges(master, units)
    master_tokens = tokens_with_spans(master)
    reviewed_tokens = tokens_with_spans(reviewed)
    blocks, rows = reviewed_structure(reviewed)
    mapping, changes = sequence_mapping(master_tokens, reviewed_tokens)

    master_token_units: list[tuple[int, int]] = []
    for _, token_start, token_end in master_tokens:
        overlaps = [
            int(unit.index)
            for unit, (unit_start, unit_end) in zip(units, unit_ranges)
            if unit_start < token_end and token_start < unit_end
        ]
        if not overlaps:
            raise ValueError(f"主稿 token {token_start}-{token_end} 无 alignment")
        master_token_units.append((overlaps[0], overlaps[-1]))

    row_ranges: dict[tuple[int, int], tuple[int, int]] = {}
    for _, block_number, line_number, token_start, token_end in rows:
        mapped = [
            mapping[index]
            for index in range(token_start, token_end + 1)
            if index in mapping
        ]
        if not mapped:
            raise ValueError(
                f"人工稿第 {block_number + 1} 组第 {line_number + 1} 行无法映射"
            )
        row_ranges[(block_number, line_number)] = (
            master_token_units[min(mapped)][0],
            master_token_units[max(mapped)][1],
        )

    plan_groups: list[dict[str, Any]] = []
    projection_warnings: list[dict[str, Any]] = []
    previous_group_end = -1
    for block_number, block in enumerate(blocks):
        ranges = [
            row_ranges[(block_number, line_number)]
            for line_number in range(len(block))
        ]
        group_start = ranges[0][0]
        group_end = ranges[-1][1]
        if group_start <= previous_group_end:
            raise ValueError(
                f"人工稿第 {block_number + 1} 组与前组时间索引重叠"
            )
        breaks: list[int] = []
        previous_break = group_start - 1
        for line_number, (_, line_end) in enumerate(ranges[:-1]):
            if line_end <= previous_break or line_end >= group_end:
                raise ValueError(
                    f"人工稿第 {block_number + 1} 组第 {line_number + 1} 行"
                    "无法形成唯一递增切点"
                )
            breaks.append(line_end)
            previous_break = line_end
        for line_number, (line, (_, line_end)) in enumerate(
            zip(block, ranges), start=1
        ):
            if len(line) > 55:
                projection_warnings.append(
                    {
                        "code": "reviewed_line_over_55",
                        "group_id": f"g{block_number + 1:04d}",
                        "line_number": line_number,
                        "characters": len(line),
                        "alignment_end": line_end,
                        "text": line,
                    }
                )
        plan_groups.append(
            {
                "group_id": f"g{block_number + 1:04d}",
                "alignment_start": group_start,
                "alignment_end": group_end,
                "line_breaks_after": breaks,
                "alternative_breaks_after": [],
                "confidence": 1.0,
                "needs_review": bool(
                    any(
                        item["group_id"] == f"g{block_number + 1:04d}"
                        for item in projection_warnings
                    )
                ),
                "protected_spans": [],
                "deletions": [],
                "corrections": [],
                "reason": "人工纯文本审阅稿自动映射；换行与空行均已冻结",
            }
        )
        previous_group_end = group_end

    normalized_draft = "\n\n".join("\n".join(block) for block in blocks) + "\n"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "stage03A_source_draft.txt").write_text(
        normalized_draft,
        encoding="utf-8",
    )
    (args.output_dir / "stage1_display_cues.txt").write_text(
        normalized_draft,
        encoding="utf-8",
    )
    plan = {
        "schema_version": "substar.stage1.direct.v1",
        "source_language": "en",
        "groups": plan_groups,
        "coverage_check": {
            "complete": False,
            "ordered": True,
            "human_reviewed_source": True,
        },
    }
    (args.output_dir / "stage1_direct_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = {
        "schema_version": "substar.reviewed-source-import.v1",
        "reviewed_file": str(args.reviewed.resolve()),
        "group_count": len(blocks),
        "cue_count": sum(len(block) for block in blocks),
        "master_token_count": len(master_tokens),
        "reviewed_token_count": len(reviewed_tokens),
        "token_similarity": difflib.SequenceMatcher(
            None,
            [item[0] for item in master_tokens],
            [item[0] for item in reviewed_tokens],
            autojunk=False,
        ).ratio(),
        "content_changes": changes,
        "warnings": projection_warnings,
        "unmapped_word_timing_policy": (
            "人工替换/新增词继承其对应原词或邻接原词的时间包络；不创建伪造词级时间"
        ),
        "deleted_alignment_policy": (
            "人工删除造成的组间索引缺口保留为空白时间，不恢复被删文字"
        ),
    }
    (args.output_dir / "human_revision_mapping_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
