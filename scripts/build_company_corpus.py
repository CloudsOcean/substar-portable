from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET


TIMECODE_RE = re.compile(
    r"^\s*\d{2}:\d{2}:\d{2}(?::\d{2}|[,.]\d{3})\s*-+>??\s*"
    r"\d{2}:\d{2}:\d{2}(?::\d{2}|[,.]\d{3})\s*$"
)
LOWER_PUNCTUATION_RE = re.compile(r"[，。,.、]")
HAN_RE = re.compile(r"[\u3400-\u9fff]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_length(text: str) -> int:
    display_tokens: list[str] = []
    for token in text.split():
        if token.endswith("//"):
            continue
        inline_deletion = re.match(r"^([^/\s]+)//([^/\s].*)$", token)
        if inline_deletion:
            token = inline_deletion.group(2)
        correction = re.match(r"^([^/\s]+)/([^/\s]+)/$", token)
        if correction:
            token = correction.group(2)
        display_tokens.append(token)
    return len(re.sub(r"\s+", "", " ".join(display_tokens)))


def han_count(text: str) -> int:
    return len(HAN_RE.findall(text))


def looks_source_language(text: str) -> bool:
    letters = len(ASCII_LETTER_RE.findall(text))
    hans = han_count(text)
    return letters > 0 and letters >= hans


def split_blank_groups(lines: Iterable[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line:
            current.append(line)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def extract_docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in document.findall(".//w:body/w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        paragraphs.append(text.strip())
    return paragraphs


def legacy_flags(source_lines: list[str], target_lines: list[str]) -> list[str]:
    flags: list[str] = []
    if any(display_length(line) > 42 for line in source_lines):
        flags.append("source_over_42_soft")
    if any(display_length(line) > 49 for line in source_lines):
        flags.append("source_over_49_hard")
    if any(han_count(line) > 18 for line in target_lines):
        flags.append("target_over_18_soft")
    if any(han_count(line) > 24 for line in target_lines):
        flags.append("target_over_24_hard")
    if any(LOWER_PUNCTUATION_RE.search(line) for line in source_lines + target_lines):
        flags.append("requires_lower_punctuation_normalization")
    return flags


def make_record(
    *,
    sample_id: str,
    source_name: str,
    authority: str,
    status: str,
    source_lines: list[str],
    target_lines: list[str] | None = None,
    group_index: int | None = None,
) -> dict[str, object]:
    targets = target_lines or []
    return {
        "schema_version": "substar.company-corpus.v1",
        "sample_id": sample_id,
        "source_name": source_name,
        "authority": authority,
        "status": status,
        "group_index": group_index,
        "source_lines": source_lines,
        "target_lines": targets,
        "source_text": " ".join(source_lines),
        "target_text": " ".join(targets),
        "legacy_exception_flags": legacy_flags(source_lines, targets),
    }


def parse_timed_bilingual(path: Path, authority: str) -> list[dict[str, object]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    records: list[dict[str, object]] = []
    current: list[str] = []
    index = 0

    def flush() -> None:
        nonlocal current, index
        clean = [line.strip() for line in current if line.strip()]
        current = []
        if not clean:
            return
        source = [line for line in clean if looks_source_language(line)]
        target = [line for line in clean if not looks_source_language(line)]
        if not source:
            return
        index += 1
        records.append(
            make_record(
                sample_id=f"{path.stem}:cue:{index:04d}",
                source_name=path.name,
                authority=authority,
                status="company_approved",
                source_lines=source,
                target_lines=target,
                group_index=index,
            )
        )

    for line in lines:
        if TIMECODE_RE.match(line):
            flush()
        else:
            current.append(line)
    flush()
    return records


def parse_untimed_bilingual_lines(
    lines: list[str], source_name: str, authority: str
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    index = 0
    for group_index, group in enumerate(split_blank_groups(lines), start=1):
        pending_source: list[str] = []
        pending_target: list[str] = []

        def flush_pair() -> None:
            nonlocal index, pending_source, pending_target
            if not pending_source:
                pending_target = []
                return
            index += 1
            records.append(
                make_record(
                    sample_id=f"{Path(source_name).stem}:pair:{index:04d}",
                    source_name=source_name,
                    authority=authority,
                    status="company_approved",
                    source_lines=pending_source,
                    target_lines=pending_target,
                    group_index=group_index,
                )
            )
            pending_source = []
            pending_target = []

        for line in group:
            if looks_source_language(line):
                if pending_source and pending_target:
                    flush_pair()
                pending_source.append(line)
            elif pending_source:
                pending_target.append(line)
        flush_pair()
    return records


def parse_untimed_bilingual(path: Path, authority: str) -> list[dict[str, object]]:
    return parse_untimed_bilingual_lines(
        path.read_text(encoding="utf-8-sig").splitlines(), path.name, authority
    )


def parse_docx_bilingual(path: Path, authority: str) -> list[dict[str, object]]:
    return parse_untimed_bilingual_lines(extract_docx_paragraphs(path), path.name, authority)


def parse_source_draft(
    path: Path, authority: str, status: str
) -> list[dict[str, object]]:
    groups = split_blank_groups(path.read_text(encoding="utf-8-sig").splitlines())
    return [
        make_record(
            sample_id=f"{path.stem}:group:{index:04d}",
            source_name=path.name,
            authority=authority,
            status=status,
            source_lines=group,
            group_index=index,
        )
        for index, group in enumerate(groups, start=1)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 Substar 公司字幕标准语料库")
    parser.add_argument("--approved-docx", action="append", default=[], type=Path)
    parser.add_argument("--approved-text", action="append", default=[], type=Path)
    parser.add_argument("--timed-text", action="append", default=[], type=Path)
    parser.add_argument("--user-corrected", action="append", default=[], type=Path)
    parser.add_argument("--machine-baseline", action="append", default=[], type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    records: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []

    def remember(path: Path, kind: str) -> None:
        sources.append(
            {
                "name": path.name,
                "kind": kind,
                "sha256": sha256(path),
                "bytes": path.stat().st_size,
            }
        )

    for path in args.approved_docx:
        remember(path, "company_approved_docx")
        records.extend(parse_docx_bilingual(path, "company_approved"))
    for path in args.approved_text:
        remember(path, "company_approved_text")
        records.extend(parse_untimed_bilingual(path, "company_approved"))
    for path in args.timed_text:
        remember(path, "company_approved_timed_text")
        records.extend(parse_timed_bilingual(path, "company_approved"))
    for path in args.user_corrected:
        remember(path, "user_corrected_source_draft")
        records.extend(parse_source_draft(path, "user_corrected", "gold_source_draft"))
    for path in args.machine_baseline:
        remember(path, "machine_baseline_source_draft")
        records.extend(parse_source_draft(path, "machine_baseline", "negative_baseline"))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    authorities = Counter(str(record["authority"]) for record in records)
    statuses = Counter(str(record["status"]) for record in records)
    flags = Counter(
        flag
        for record in records
        for flag in record.get("legacy_exception_flags", [])  # type: ignore[arg-type]
    )
    authority_flags = Counter(
        f"{record['authority']}:{flag}"
        for record in records
        for flag in record.get("legacy_exception_flags", [])  # type: ignore[arg-type]
    )
    manifest = {
        "schema_version": "substar.company-corpus-manifest.v1",
        "corpus_file": args.output.name,
        "record_count": len(records),
        "authorities": dict(authorities),
        "statuses": dict(statuses),
        "derived_flags": dict(flags),
        "derived_flags_by_authority": dict(authority_flags),
        "sources": sources,
        "policy": {
            "company_approved": "公司历史审核稿，是产品风格基线；先做确定性规范化再作为提示词正例。",
            "user_corrected": "用户人工修订稿，是当前切分偏好的最高优先级正例。",
            "machine_baseline": "旧贪心脚本结果，只用于回归比较，不得作为正例。",
            "legacy_exception_flags": "自动派生的待规范化标签，不等于原稿错误。",
        },
    }
    args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
