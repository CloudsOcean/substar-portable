from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.asr_longform import lexical_tokens  # noqa: E402
from substar_core.config import load_settings  # noqa: E402
from substar_core.policy import SubtitlePolicy  # noqa: E402
from substar_core.stage2 import (  # noqa: E402
    classify_source_language,
    target_language_for,
)


TIMECODE_RE = re.compile(
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})"
)
CJK_RE = re.compile(r"[\u3400-\u9fff]")
BASELINE_PUNCTUATION_RE = re.compile(r"[,，、。.]")
SAFE_SHORT = {
    "yes", "no", "yeah", "right", "okay", "ok", "amazing", "absolutely",
    "wow", "whoa", "what", "why", "thanks", "hello", "hi", "well", "oh",
    "um", "thank you", "bye", "bye bye", "love it", "dang", "all right",
    "oh wow", "voila",
}
DANGLING_END = {
    "a", "an", "the", "and", "or", "but", "because", "if", "when", "while",
    "with", "without", "of", "to", "for", "from", "in", "on", "at", "by",
    "my", "your", "our", "their",
}
DANGLING_PHRASES = {
    ("which", "is"),
    ("which", "are"),
    ("who", "is"),
    ("who", "are"),
}


@dataclass
class Cue:
    number: int
    start: float
    end: float
    source: str
    target: str


def time_seconds(value: str) -> float:
    match = TIMECODE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"无效时间码：{value}")
    return (
        int(match["h"]) * 3600
        + int(match["m"]) * 60
        + int(match["s"])
        + int(match["ms"]) / 1000
    )


def read_srt(
    path: Path,
    *,
    display_order: str = "source_target",
    source_language_hint: str = "en",
) -> list[Cue]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n{2,}", text.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_raw, end_raw = (part.strip() for part in lines[1].split("-->", 1))
        content = lines[2:]
        top = content[0]
        bottom = " ".join(content[1:]).strip()
        if display_order == "en_zh":
            source, target = (
                (bottom, top)
                if source_language_hint.startswith("zh")
                else (top, bottom)
            )
        elif display_order == "zh_en":
            source, target = (
                (top, bottom)
                if source_language_hint.startswith("zh")
                else (bottom, top)
            )
        else:
            source, target = top, bottom
        cues.append(
            Cue(
                number=int(lines[0]),
                start=time_seconds(start_raw),
                end=time_seconds(end_raw),
                source=source,
                target=target,
            )
        )
    return cues


def count_source(text: str, settings: dict[str, Any]) -> int:
    return SubtitlePolicy.from_settings(settings).line_length(text)


def token_similarity(candidate: str, reference: str) -> dict[str, Any]:
    candidate_tokens = [item.normalized for item in lexical_tokens(candidate)]
    reference_tokens = [item.normalized for item in lexical_tokens(reference)]
    matcher = difflib.SequenceMatcher(
        None, reference_tokens, candidate_tokens, autojunk=False
    )
    matched = sum(block.size for block in matcher.get_matching_blocks())
    opcodes = matcher.get_opcodes()
    return {
        "candidate_tokens": len(candidate_tokens),
        "reference_tokens": len(reference_tokens),
        "matched_tokens": matched,
        "reference_recall": round(matched / max(1, len(reference_tokens)), 4),
        "candidate_precision": round(matched / max(1, len(candidate_tokens)), 4),
        "sequence_ratio": round(matcher.ratio(), 4),
        "longest_reference_deletion_tokens": max(
            [i2 - i1 for tag, i1, i2, _, _ in opcodes if tag == "delete"] or [0]
        ),
        "longest_candidate_insertion_tokens": max(
            [j2 - j1 for tag, _, _, j1, j2 in opcodes if tag == "insert"] or [0]
        ),
    }


def audit(job_dir: Path, srt_path: Path, reference: Path | None) -> dict[str, Any]:
    settings = load_settings(include_secret=False)
    policy = SubtitlePolicy.from_settings(settings)
    alignment_path = job_dir / "alignment.json"
    source_language_hint = "en"
    if alignment_path.exists():
        try:
            source_language_hint = str(
                json.loads(alignment_path.read_text(encoding="utf-8")).get(
                    "language", "en"
                )
            )
        except (OSError, json.JSONDecodeError):
            pass
    cues = read_srt(
        srt_path,
        display_order=str(settings.get("display_order", "source_target")),
        source_language_hint=source_language_hint,
    )
    issues: dict[str, list[dict[str, Any]]] = {
        "numbering": [],
        "time": [],
        "source_hard_limit": [],
        "target_hard_limit": [],
        "missing_translation": [],
        "source_copied_as_target": [],
        "bottom_baseline_punctuation": [],
        "short_fragments": [],
        "dangling_boundaries": [],
    }
    previous_end = -1.0
    for index, cue in enumerate(cues, start=1):
        if cue.number != index:
            issues["numbering"].append(
                {"cue": cue.number, "expected": index}
            )
        if cue.start < previous_end - 0.001 or cue.end <= cue.start:
            issues["time"].append(
                {
                    "cue": cue.number,
                    "start": cue.start,
                    "end": cue.end,
                    "previous_end": previous_end,
                }
            )
        previous_end = max(previous_end, cue.end)
        source_count = count_source(cue.source, settings)
        source_limit = policy.hard_limit(cue.source)
        if source_count > source_limit:
            issues["source_hard_limit"].append(
                {
                    "cue": cue.number,
                    "count": source_count,
                    "limit": source_limit,
                    "text": cue.source,
                }
            )
        target_count = policy.line_length(cue.target)
        target_limit = policy.hard_limit(cue.target)
        if target_count > target_limit:
            issues["target_hard_limit"].append(
                {
                    "cue": cue.number,
                    "count": target_count,
                    "limit": target_limit,
                    "text": cue.target,
                }
            )
        if not cue.target or "翻译失败" in cue.target:
            issues["missing_translation"].append(
                {"cue": cue.number, "text": cue.target}
            )
        configured_target = str(settings.get("target_language_mode", "auto_opposite"))
        expected_target = (
            configured_target
            if configured_target in {"zh-CN", "en", "ja", "ko"}
            else target_language_for(classify_source_language(cue.source))
        )
        if (
            cue.target
            and "翻译失败" not in cue.target
            and expected_target == "zh-CN"
            and not CJK_RE.search(cue.target)
            and re.search(r"[A-Za-z]", cue.source)
        ):
            issues["source_copied_as_target"].append(
                {"cue": cue.number, "source": cue.source, "target": cue.target}
            )
        if (
            settings.get("bottom_baseline_punctuation") == "normalize"
            and BASELINE_PUNCTUATION_RE.search(cue.target)
        ):
            issues["bottom_baseline_punctuation"].append(
                {"cue": cue.number, "text": cue.target}
            )
        words = [item.normalized for item in lexical_tokens(cue.source)]
        if (
            0 < len(words) <= 2
            and " ".join(words) not in SAFE_SHORT
            and not CJK_RE.search(cue.source)
        ):
            issues["short_fragments"].append(
                {"cue": cue.number, "word_count": len(words), "text": cue.source}
            )
        if words and (
            words[-1] in DANGLING_END
            or tuple(words[-2:]) in DANGLING_PHRASES
        ):
            issues["dangling_boundaries"].append(
                {"cue": cue.number, "side": "end", "text": cue.source}
            )

    source_text = " ".join(cue.source for cue in cues)
    comparison = None
    if reference and reference.exists():
        reference_cues = read_srt(
            reference,
            display_order=str(settings.get("display_order", "source_target")),
            source_language_hint=source_language_hint,
        )
        reference_source = " ".join(cue.source for cue in reference_cues)
        comparison = {
            "reference": str(reference),
            **token_similarity(source_text, reference_source),
        }

    ingest_path = job_dir / "asr_ingest_report.json"
    delivery_path = job_dir / "delivery_report.json"
    translation_path = job_dir / "final" / "translation_report.json"
    quality_path = job_dir / "quality" / "translation_polish_report.json"
    if not quality_path.exists():
        quality_path = job_dir / "quality" / "quality_review_report.json"
    supporting: dict[str, Any] = {}
    for name, path in {
        "ingest": ingest_path,
        "delivery": delivery_path,
        "translation": translation_path,
        "quality": quality_path,
    }.items():
        if path.exists():
            try:
                supporting[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                supporting[name] = {"status": "unreadable"}

    counts = {name: len(values) for name, values in issues.items()}
    blockers = {
        name: count
        for name, count in counts.items()
        if count
        and name
        in {
            "numbering",
            "time",
            "source_hard_limit",
            "target_hard_limit",
            "missing_translation",
            "source_copied_as_target",
        }
    }
    ingest_status = supporting.get("ingest", {}).get("status", "missing")
    return {
        "schema_version": "substar.automatic-audit.v1",
        "job_dir": str(job_dir),
        "srt": str(srt_path),
        "status": "pass" if not blockers and ingest_status == "pass" else "review",
        "cue_count": len(cues),
        "duration": {
            "first_start": cues[0].start if cues else None,
            "last_end": cues[-1].end if cues else None,
        },
        "configuration": {
            "english_hard_limit": settings["english_hard_limit"],
            "english_count_spaces": settings["english_count_spaces"],
            "english_count_punctuation": settings["english_count_punctuation"],
            "chinese_hard_limit": settings["chinese_hard_limit"],
            "mixed_hard_limit": settings.get("mixed_hard_limit", 25),
            "japanese_hard_limit": settings["japanese_hard_limit"],
            "korean_hard_limit": settings["korean_hard_limit"],
            "bottom_baseline_punctuation": settings["bottom_baseline_punctuation"],
        },
        "issue_counts": counts,
        "blockers": blockers,
        "issues": issues,
        "reference_comparison": comparison,
        "pipeline": {
            "ingest_status": ingest_status,
            "ingest_strategy": supporting.get("ingest", {}).get("strategy"),
            "primary_request_count": len(
                supporting.get("ingest", {}).get("primary_requests", [])
            ),
            "weak_seams": supporting.get("ingest", {}).get("weak_seams", []),
            "weak_locators": supporting.get("ingest", {}).get("weak_locators", []),
            "delivery_status": supporting.get("delivery", {}).get("status"),
        },
    }


def markdown_report(report: dict[str, Any]) -> str:
    comparison = report.get("reference_comparison") or {}
    lines = [
        "# Substar 自动审计",
        "",
        f"- 总体状态：`{report['status']}`",
        f"- 字幕条数：{report['cue_count']}",
        f"- MiMo策略：`{report['pipeline'].get('ingest_strategy')}`",
        f"- 主ASR请求数：{report['pipeline'].get('primary_request_count')}",
        f"- 接缝弱项：{len(report['pipeline'].get('weak_seams') or [])}",
        f"- 定位弱项：{len(report['pipeline'].get('weak_locators') or [])}",
        f"- 交付状态：`{report['pipeline'].get('delivery_status')}`",
        "",
        "## 硬性检查",
        "",
    ]
    for name, count in report["issue_counts"].items():
        lines.append(f"- {name}: {count}")
    if comparison:
        lines.extend(
            [
                "",
                "## 与人工审阅稿源文比较",
                "",
                f"- 参考稿词数：{comparison['reference_tokens']}",
                f"- 新稿词数：{comparison['candidate_tokens']}",
                f"- 参考词召回：{comparison['reference_recall']:.2%}",
                f"- 新稿匹配精度：{comparison['candidate_precision']:.2%}",
                f"- 顺序相似度：{comparison['sequence_ratio']:.2%}",
                f"- 最长纯漏词连续段：{comparison['longest_reference_deletion_tokens']}词",
            ]
        )
    lines.extend(["", "详细问题和样例见 `automatic_audit.json`。", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    srt_path = args.srt or args.job_dir / "substar_bilingual_final.srt"
    report = audit(args.job_dir, srt_path, args.reference)
    (args.job_dir / "automatic_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.job_dir / "automatic_audit.md").write_text(
        markdown_report(report),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
