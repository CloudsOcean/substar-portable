from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..policy import (
    BASELINE_PUNCTUATION,
    RAISED_PUNCTUATION,
    classify_language,
    count_visible_characters,
)
from ..language_layout import layout_tokens

MASTER_RE = re.compile(
    r"^## MASTER_TRANSCRIPT\s*\r?\n\s*```text\s*\r?\n(.*?)\r?\n```",
    flags=re.MULTILINE | re.DOTALL,
)
ALIGNMENT_RE = re.compile(
    r"^## ALIGNMENT.*?```tsv\s*\r?\n(.*?)\r?\n```",
    flags=re.MULTILINE | re.DOTALL,
)
CORRECTION_TOKEN_RE = re.compile(r"^([^/\s]+)/([^/\s]+)/$")
DECIMAL_DOT_RE = re.compile(r"(?<=\d)\.(?=\d)")
TECHNICAL_DOT_RE = re.compile(
    r"(?:https?://\S+|www\.\S+|\b\S+@\S+\.\S+\b|\bv?\d+(?:\.\d+){1,3}\b)",
    flags=re.IGNORECASE,
)
LOWER_PUNCTUATION = set(BASELINE_PUNCTUATION)


@dataclass(frozen=True)
class AlignmentUnit:
    index: int
    start: float
    end: float
    text: str
    sentence_id: int | None = None
    sentence_start: bool = False
    sentence_end: bool = False
    speaker_id: str | None = None
    speaker_confidence: float = 0.0


@dataclass
class SegmentationValidationReport:
    valid: bool = True
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def error(self, code: str, message: str, **context: Any) -> None:
        self.valid = False
        self.errors.append({"code": code, "message": message, **context})

    def warning(self, code: str, message: str, **context: Any) -> None:
        self.warnings.append({"code": code, "message": message, **context})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "substar.segmentation.validation.v1",
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "stats": self.stats,
        }


def extract_master(material: str) -> str:
    match = MASTER_RE.search(material)
    if not match:
        raise ValueError("找不到 MASTER_TRANSCRIPT text 代码块")
    return match.group(1).strip()


def extract_alignment(material: str) -> list[AlignmentUnit]:
    match = ALIGNMENT_RE.search(material)
    if not match:
        raise ValueError("找不到 ALIGNMENT tsv 代码块")
    units: list[AlignmentUnit] = []
    for line_number, raw in enumerate(match.group(1).splitlines(), start=1):
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) not in {4, 7, 9}:
            raise ValueError(
                f"ALIGNMENT 第 {line_number} 行必须是4列旧格式、7列句段格式"
                "或9列含说话人格式 TSV"
            )
        sentence_id = None
        sentence_start = False
        sentence_end = False
        speaker_id = None
        speaker_confidence = 0.0
        if len(parts) in {7, 9}:
            sentence_id = int(parts[4]) if parts[4] not in {"", "-"} else None
            sentence_start = parts[5] == "1"
            sentence_end = parts[6] == "1"
        if len(parts) == 9:
            speaker_id = parts[7] if parts[7] not in {"", "-"} else None
            speaker_confidence = float(parts[8] or 0)
        units.append(
            AlignmentUnit(
                index=int(parts[0]),
                start=float(parts[1]),
                end=float(parts[2]),
                text=parts[3],
                sentence_id=sentence_id,
                sentence_start=sentence_start,
                sentence_end=sentence_end,
                speaker_id=speaker_id,
                speaker_confidence=speaker_confidence,
            )
        )
    if not units:
        raise ValueError("ALIGNMENT 为空")
    return units


def split_groups(draft: str) -> list[list[str]]:
    normalized = draft.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return [
        [line.strip() for line in block.splitlines() if line.strip()]
        for block in re.split(r"\n\s*\n", normalized)
        if block.strip()
    ]


def _project_token(token: str) -> tuple[str, str, list[str]]:
    errors: list[str] = []
    if token.startswith(("http://", "https://")):
        return token, token, errors
    if token == "//":
        return "", "", ["孤立的 // 标记"]
    punctuated_deletion = re.match(r"^([^/\s]+)//([,，、.;；:]*)$", token)
    if punctuated_deletion:
        source, trailing = punctuated_deletion.groups()
        return source + trailing, "", errors
    if token.endswith("//"):
        source = token[:-2]
        if not source or "/" in source:
            errors.append("删除标记必须是 词// 且不能与纠错嵌套")
        return source, "", errors
    inline_deletion = re.match(r"^([^/\s]+)//([^/\s].*)$", token)
    if inline_deletion:
        source, suffix = inline_deletion.groups()
        return source + suffix, suffix, errors
    punctuated_correction = re.match(
        r"^([^/\s]+)/([^/\s]+)/([^\w/\s]*)$", token
    )
    if punctuated_correction:
        source, proposal, trailing = punctuated_correction.groups()
        projected_trailing = "" if trailing in {"-", "—"} else trailing
        return (
            source + trailing,
            proposal.replace("␠", " ") + projected_trailing,
            errors,
        )
    correction = CORRECTION_TOKEN_RE.match(token)
    if correction:
        source, proposal = correction.groups()
        if not (source.isdigit() and proposal.isdigit()):
            return source, proposal.replace("␠", " "), errors
    slash_count = token.count("/")
    if token.endswith("/") and slash_count >= 2:
        errors.append("斜杠不符合 错误词/建议词/ 或 词// 语法")
    return token, token, errors


def project_annotations(text: str) -> tuple[str, str, list[dict[str, Any]]]:
    raw_tokens: list[str] = []
    display_tokens: list[str] = []
    issues: list[dict[str, Any]] = []
    for position, token in enumerate(text.split()):
        raw, display, token_errors = _project_token(token)
        if raw:
            raw_tokens.append(raw)
        if display:
            display_tokens.append(display)
        for message in token_errors:
            issues.append({"token_position": position, "token": token, "message": message})
    return " ".join(raw_tokens), " ".join(display_tokens), issues


def display_normalize(
    text: str,
    *,
    baseline_punctuation: str = "normalize",
    raised_punctuation: str = "preserve",
) -> str:
    if baseline_punctuation not in {"preserve", "normalize"}:
        raise ValueError(f"未知下标点策略：{baseline_punctuation}")
    if raised_punctuation not in {"preserve", "remove"}:
        raise ValueError(f"未知上标点策略：{raised_punctuation}")
    result: list[str] = []
    for index, char in enumerate(text):
        previous = text[index - 1] if index else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        if raised_punctuation == "remove" and char in RAISED_PUNCTUATION:
            continue
        if baseline_punctuation == "preserve":
            result.append(char)
        elif char == "." and previous.isdigit() and following.isdigit():
            result.append(char)
        elif char in {",", "，", "、"}:
            result.append(" ")
        elif char in {".", "。"}:
            continue
        else:
            result.append(char)
    return re.sub(r"\s+", " ", "".join(result)).strip()


def comparison_normalize(text: str) -> str:
    return re.sub(
        r"\s+",
        "",
        display_normalize(text, baseline_punctuation="normalize"),
    )


def visible_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def configured_length(
    text: str,
    *,
    count_spaces: bool = False,
    count_punctuation: bool = True,
) -> int:
    return count_visible_characters(
        text,
        count_spaces=count_spaces,
        count_punctuation=count_punctuation,
    )


def han_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff]", text))


def is_english_dominant(text: str) -> bool:
    return len(re.findall(r"[A-Za-z]", text)) >= max(1, han_count(text))


def illegal_lower_punctuation(text: str) -> list[tuple[int, str]]:
    protected: set[int] = set()
    for match in TECHNICAL_DOT_RE.finditer(text):
        for index in range(match.start(), match.end()):
            protected.add(index)
    return [
        (index, char)
        for index, char in enumerate(text)
        if char in LOWER_PUNCTUATION and index not in protected
    ]


def _all_cues(groups: Iterable[list[str]]) -> list[str]:
    return [cue for group in groups for cue in group]


def validate_draft(
    master: str,
    draft: str,
    *,
    analysis: dict[str, Any] | None = None,
    candidates: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    alignment: list[AlignmentUnit] | None = None,
    baseline_punctuation: str = "preserve",
    raised_punctuation: str = "preserve",
    source_language: str | None = None,
    english_hard_limit: int = 55,
    chinese_hard_limit: int = 24,
    mixed_hard_limit: int = 25,
    japanese_hard_limit: int = 25,
    korean_hard_limit: int = 32,
    english_count_spaces: bool = False,
    english_count_punctuation: bool = True,
    minimum_cue_duration_ms: int = 400,
) -> SegmentationValidationReport:
    report = SegmentationValidationReport()
    groups = split_groups(draft)
    cues = _all_cues(groups)
    if not groups:
        report.error("empty_draft", "源文草案为空")
        return report

    raw_parts: list[str] = []
    display_parts: list[str] = []
    max_english = 0
    max_han = 0
    max_visible = 0
    marked_deletions = 0
    marked_corrections = 0

    for group_number, group in enumerate(groups, start=1):
        if not group:
            report.error("empty_group", "出现空意义群", group=group_number)
        for cue_number, cue in enumerate(group, start=1):
            raw, projected, annotation_issues = project_annotations(cue)
            raw_parts.append(raw)
            display = display_normalize(
                projected,
                baseline_punctuation=baseline_punctuation,
                raised_punctuation=raised_punctuation,
            )
            display = layout_tokens(display.split(), source_language)
            display_parts.append(display)
            marked_deletions += sum(token.count("//") for token in cue.split())
            marked_corrections += sum(bool(CORRECTION_TOKEN_RE.match(token)) for token in cue.split())
            for issue in annotation_issues:
                report.error(
                    "invalid_annotation",
                    issue["message"],
                    group=group_number,
                    cue=cue_number,
                    token=issue["token"],
                )
            if not display:
                report.error(
                    "empty_display_cue",
                    "应用删除标记后 cue 为空",
                    group=group_number,
                    cue=cue_number,
                )
            english_length = configured_length(
                display,
                count_spaces=english_count_spaces,
                count_punctuation=english_count_punctuation,
            )
            chinese_length = han_count(display)
            max_english = max(max_english, english_length)
            max_han = max(max_han, chinese_length)
            max_visible = max(max_visible, english_length)
            active_source = str(source_language or "").strip().lower().replace("_", "-")
            language = classify_language(display)
            if active_source in {"mixed", "zh-en", "en-zh", "zh-en-mixed", "mixed-zh-en"}:
                language = "mixed"
            elif active_source.startswith("zh"):
                language = "zh"
            elif active_source.startswith("ja"):
                language = "ja"
            elif active_source.startswith("ko"):
                language = "ko"
            elif active_source.startswith("en"):
                language = "en"
            if language in {"mixed", "mixed_zh", "mixed_en"}:
                hard_limit = mixed_hard_limit
                code_prefix = "mixed"
                label = "混合源文"
            elif language in {"zh", "mixed_zh"}:
                hard_limit = chinese_hard_limit
                code_prefix = "chinese"
                label = "中文源文"
            elif language == "ja":
                hard_limit = japanese_hard_limit
                code_prefix = "japanese"
                label = "日文源文"
            elif language == "ko":
                hard_limit = korean_hard_limit
                code_prefix = "korean"
                label = "韩文源文"
            else:
                hard_limit = english_hard_limit
                code_prefix = "english"
                label = "英文源文"
            if english_length > hard_limit:
                report.error(
                    f"{code_prefix}_over_{hard_limit}",
                    f"{label} cue 超过 {hard_limit} 字符硬上限",
                    group=group_number,
                    cue=cue_number,
                    length=english_length,
                    text=cue,
                )
            illegal = (
                illegal_lower_punctuation(projected)
                if baseline_punctuation != "preserve"
                else []
            )
            if illegal:
                report.error(
                    "lower_punctuation",
                    "显示轨包含非例外下标点",
                    group=group_number,
                    cue=cue_number,
                    punctuation=[char for _, char in illegal],
                    text=cue,
                )

    reconstructed_raw = " ".join(raw_parts)
    if comparison_normalize(reconstructed_raw) != comparison_normalize(master):
        report.error(
            "source_coverage",
            "草案原始轨不能完整、顺序一致地恢复 MASTER_TRANSCRIPT",
            master_normalized_length=len(comparison_normalize(master)),
            draft_normalized_length=len(comparison_normalize(reconstructed_raw)),
        )

    if decision is not None:
        decision_draft = str(decision.get("final_draft", "")).strip()
        if decision_draft != draft.strip():
            report.error("decision_draft_mismatch", "decision.final_draft 与输出草案不一致")
        decision_groups = decision.get("groups", [])
        assembled = "\n\n".join(
            "\n".join(str(cue) for cue in group.get("cues", []))
            for group in decision_groups
        ).strip()
        if assembled != draft.strip():
            report.error("decision_group_mismatch", "decision.groups 不能逐组重建 final_draft")

    if analysis is not None and candidates is not None and decision is not None:
        _validate_structured_choices(report, master, analysis, candidates, decision, alignment or [])

    report.stats = {
        "group_count": len(groups),
        "cue_count": len(cues),
        "max_english_visible_characters": max_english,
        "max_chinese_characters": max_han,
        "max_source_visible_characters": max_visible,
        "deletion_markers": marked_deletions,
        "correction_markers": marked_corrections,
        "source_coverage": 1.0 if not any(e["code"] == "source_coverage" for e in report.errors) else 0.0,
    }
    return report


def _validate_structured_choices(
    report: SegmentationValidationReport,
    master: str,
    analysis: dict[str, Any],
    candidates: dict[str, Any],
    decision: dict[str, Any],
    alignment: list[AlignmentUnit],
) -> None:
    analysis_list = analysis.get("groups", [])
    candidate_list = candidates.get("groups", [])
    decision_list = decision.get("groups", [])
    analysis_ids = [group.get("group_id") for group in analysis_list]
    candidate_ids = [group.get("group_id") for group in candidate_list]
    decision_ids = [group.get("group_id") for group in decision_list]
    if len(set(analysis_ids)) != len(analysis_ids):
        report.error("duplicate_analysis_group", "03A1 出现重复 group_id")
    if candidate_ids != analysis_ids or decision_ids != analysis_ids:
        report.error("structured_group_order", "03A1/03A2/03A3 的 group_id 顺序不一致")
    analysis_raw = " ".join(str(group.get("source_text", "")) for group in analysis_list)
    if comparison_normalize(analysis_raw) != comparison_normalize(master):
        report.error("analysis_coverage", "03A1 groups 不能完整、顺序一致地恢复主稿")

    analysis_groups = {group.get("group_id"): group for group in analysis_list}
    candidate_groups = {group.get("group_id"): group for group in candidate_list}
    unit_by_index = {unit.index: unit for unit in alignment}

    for group_id, candidate_group in candidate_groups.items():
        analysis_group = analysis_groups.get(group_id)
        if analysis_group is None:
            continue
        group_start = int(analysis_group.get("alignment_start", -1))
        group_end = int(analysis_group.get("alignment_end", -1))
        if group_start < 0 or group_end < group_start:
            report.error("invalid_alignment_range", "03A1 alignment 范围无效", group_id=group_id)
        for candidate in candidate_group.get("candidates", []):
            candidate_cues = [str(value) for value in candidate.get("cues", [])]
            cuts = [int(value) for value in candidate.get("cut_after_alignment", [])]
            if len(cuts) != max(0, len(candidate_cues) - 1):
                report.error(
                    "cut_count_mismatch",
                    "cut_after_alignment 数量必须等于 cues 数量减一",
                    group_id=group_id,
                    candidate_id=candidate.get("candidate_id"),
                )
            if cuts != sorted(set(cuts)):
                report.error(
                    "cut_order",
                    "候选切点必须严格递增且不重复",
                    group_id=group_id,
                    candidate_id=candidate.get("candidate_id"),
                )
            if any(cut < group_start or cut >= group_end for cut in cuts):
                report.error(
                    "cut_outside_group",
                    "候选切点越过意义群 alignment 范围",
                    group_id=group_id,
                    candidate_id=candidate.get("candidate_id"),
                )
            candidate_raw_parts = [project_annotations(cue)[0] for cue in candidate_cues]
            if comparison_normalize(" ".join(candidate_raw_parts)) != comparison_normalize(
                str(analysis_group.get("source_text", ""))
            ):
                report.error(
                    "candidate_coverage",
                    "03A2 候选不能恢复 03A1 source_text",
                    group_id=group_id,
                    candidate_id=candidate.get("candidate_id"),
                )
            if candidate.get("hard_violations"):
                report.error(
                    "declared_hard_violation",
                    "03A2 输出了自报硬违规的候选",
                    group_id=group_id,
                    candidate_id=candidate.get("candidate_id"),
                )

    for selected in decision_list:
        group_id = selected.get("group_id")
        candidate_group = candidate_groups.get(group_id)
        analysis_group = analysis_groups.get(group_id)
        if candidate_group is None or analysis_group is None:
            report.error("missing_structured_group", "结构化阶段缺少 group_id", group_id=group_id)
            continue
        candidate = next(
            (
                item
                for item in candidate_group.get("candidates", [])
                if item.get("candidate_id") == selected.get("selected_candidate_id")
            ),
            None,
        )
        if candidate is None:
            report.error("unknown_candidate", "选择了不存在的候选", group_id=group_id)
            continue
        if candidate.get("cues") != selected.get("cues"):
            report.error("candidate_rewrite", "03A3 改写了 03A2 候选", group_id=group_id)

        cuts = [int(value) for value in candidate.get("cut_after_alignment", [])]
        forbidden = {
            int(item["after_alignment"])
            for item in analysis_group.get("forbidden_boundaries", [])
            if isinstance(item, dict)
            and isinstance(item.get("after_alignment"), int)
        }
        for cut in cuts:
            if cut in forbidden:
                report.error("forbidden_cut", "切点命中 03A1 禁止边界", group_id=group_id, alignment_index=cut)
            if cut not in unit_by_index:
                report.error("unknown_alignment_cut", "切点不存在于 alignment", group_id=group_id, alignment_index=cut)
                continue
            next_unit = unit_by_index.get(cut + 1)
            current = unit_by_index[cut]
            if next_unit and current.start == next_unit.start and current.end == next_unit.end:
                report.error(
                    "shared_envelope_cut",
                    "切点位于不可拆共享时间包络内部",
                    group_id=group_id,
                    alignment_index=cut,
                )
            for span in analysis_group.get("protected_spans", []):
                if span.get("protection_level") != "hard":
                    continue
                start = int(span.get("alignment_start", -1))
                end = int(span.get("alignment_end", -1))
                if start <= cut < end:
                    report.error(
                        "hard_span_cut",
                        "切点落在 hard_protected_span 内部",
                        group_id=group_id,
                        alignment_index=cut,
                        span=span.get("span_id", ""),
                    )
        declared_soft_splits = {
            (str(item.get("span_id", "")), int(item.get("after_alignment", -1)))
            for item in candidate.get("strong_soft_splits", [])
            if isinstance(item, dict)
        }
        for span in analysis_group.get("protected_spans", []):
            if span.get("protection_level") != "strong_soft":
                continue
            start = int(span.get("alignment_start", -1))
            end = int(span.get("alignment_end", -1))
            for cut in cuts:
                if start <= cut < end and (
                    str(span.get("span_id", "")), cut
                ) not in declared_soft_splits:
                    report.error(
                        "undeclared_strong_soft_cut",
                        "切入 strong_soft 必须声明具体硬约束",
                        group_id=group_id,
                        alignment_index=cut,
                        span=span.get("span_id", ""),
                    )


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_validation_report(path: Path, report: SegmentationValidationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
