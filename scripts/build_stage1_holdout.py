from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path


TOKEN_RE = re.compile(
    r"[A-Za-z]+(?:['’][A-Za-z]+)?|\d+(?:[.:/-]\d+)*|[\u3400-\u9fff]"
)
TERMINAL_RE = re.compile(r"[.!?。！？][\"'”’)]*$")


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(char for char in value if char.isalnum())


def main() -> None:
    parser = argparse.ArgumentParser(description="构造不注入提示词的公司稿 Stage1 保留集")
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=80)
    parser.add_argument("--hard-limit", type=int, default=55)
    args = parser.parse_args()

    case_text = normalized(args.cases.read_text(encoding="utf-8-sig"))
    rows = [
        json.loads(line)
        for line in args.corpus.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    eligible: list[dict] = []
    excluded_case_overlap = 0
    excluded_noncompliant = 0
    for row in rows:
        if (
            row.get("authority") != "company_approved"
            or row.get("source_name") != args.source_name
        ):
            continue
        lines = [str(value).strip() for value in row.get("source_lines", []) if str(value).strip()]
        text = " ".join(lines)
        if not text or not re.search(r"[A-Za-z]", text):
            continue
        key = normalized(text)
        if key and key in case_text:
            excluded_case_overlap += 1
            continue
        if any(len(line) > args.hard_limit for line in lines):
            excluded_noncompliant += 1
            continue
        eligible.append(row)
    selected = eligible[args.start : args.start + args.count]
    if not selected:
        raise SystemExit("没有满足条件的公司稿保留样本")

    source_lines: list[str] = []
    gold_blocks: list[list[str]] = []
    block: list[str] = []
    sample_ids: list[str] = []
    for row in selected:
        lines = [str(value).strip() for value in row["source_lines"] if str(value).strip()]
        sample_ids.append(str(row["sample_id"]))
        for line in lines:
            source_lines.append(line)
            block.append(line)
            if TERMINAL_RE.search(line):
                gold_blocks.append(block)
                block = []
    if block:
        gold_blocks.append(block)

    master = " ".join(source_lines)
    tokens = TOKEN_RE.findall(master)
    alignment_lines: list[str] = []
    cursor = 0.0
    for index, token in enumerate(tokens):
        duration = 0.28 if len(token) <= 3 else 0.38
        alignment_lines.append(
            f"{index}\t{cursor:.3f}\t{cursor + duration:.3f}\t{token}\t-\t0\t0"
        )
        cursor += duration + 0.04

    material = (
        "## MASTER_TRANSCRIPT\n\n```text\n"
        + master
        + "\n```\n\n## ALIGNMENT\n\n"
        "字段为 `index / start秒 / end秒 / text / whisper_sentence_id / "
        "sentence_start / sentence_end`。本保留集故意不提供 Whisper 句界。\n\n```tsv\n"
        + "\n".join(alignment_lines)
        + "\n```\n"
    )
    gold = "\n\n".join("\n".join(lines) for lines in gold_blocks).strip() + "\n"

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "chatbox_material.md").write_text(material, encoding="utf-8")
    (args.output_dir / "gold_stage03A.txt").write_text(gold, encoding="utf-8")
    (args.output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "substar.stage1.holdout.v1",
                "source_name": args.source_name,
                "start": args.start,
                "requested_count": args.count,
                "selected_count": len(selected),
                "sample_ids": sample_ids,
                "excluded_exact_case_overlap": excluded_case_overlap,
                "excluded_noncompliant": excluded_noncompliant,
                "whisper_boundaries_injected": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"selected={len(selected)} tokens={len(tokens)} blocks={len(gold_blocks)}")


if __name__ == "__main__":
    main()
