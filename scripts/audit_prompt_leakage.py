from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?")


def words(text: str) -> list[str]:
    return [item.casefold().replace("’", "'") for item in WORD_RE.findall(text)]


def ngrams(items: list[str], size: int) -> dict[tuple[str, ...], int]:
    return {
        tuple(items[index : index + size]): index
        for index in range(max(0, len(items) - size + 1))
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查实验提示词是否直接复用了测试素材或人工答案的连续英文措辞"
    )
    parser.add_argument("--prompt", action="append", required=True, type=Path)
    parser.add_argument("--forbidden", action="append", required=True, type=Path)
    parser.add_argument("--ngram", type=int, default=6)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    forbidden: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for path in args.forbidden:
        tokens = words(path.read_text(encoding="utf-8-sig", errors="replace"))
        for phrase, position in ngrams(tokens, args.ngram).items():
            forbidden.setdefault(phrase, []).append(
                {"file": str(path.resolve()), "word_offset": position}
            )

    matches: list[dict[str, Any]] = []
    prompt_rows: list[dict[str, Any]] = []
    for path in args.prompt:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        tokens = words(text)
        prompt_rows.append(
            {
                "file": str(path.resolve()),
                "sha256": sha256(path),
                "english_word_count": len(tokens),
            }
        )
        for phrase, position in ngrams(tokens, args.ngram).items():
            if phrase not in forbidden:
                continue
            matches.append(
                {
                    "prompt": str(path.resolve()),
                    "prompt_word_offset": position,
                    "phrase": " ".join(phrase),
                    "forbidden_sources": forbidden[phrase],
                }
            )

    report = {
        "schema_version": "substar.prompt-leakage-audit.v1",
        "ngram_size": args.ngram,
        "prompts": prompt_rows,
        "forbidden_files": [
            {"file": str(path.resolve()), "sha256": sha256(path)}
            for path in args.forbidden
        ],
        "match_count": len(matches),
        "matches": matches,
        "passed": not matches,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"prompt_leakage_passed={report['passed']} matches={len(matches)}",
        flush=True,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
