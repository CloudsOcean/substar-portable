from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any, Iterable, Mapping


HEADER = "SUBSTAR-CUE-SCRIPT/1"


OUTPUT_CONTRACTS = {
    "SEGMENT": """
EXPERIMENTAL OUTPUT CONTRACT (this final section overrides every earlier JSON-output instruction):
Return only SUBSTAR-CUE-SCRIPT/1 text. Do not return JSON, Markdown, commentary, token IDs, or internal IDs.
Copy the requested local aliases exactly. Return consecutive CUE rows covering every OWN word exactly once.
Each row is: CUE<TAB>Cue alias<TAB>meaning-group alias<TAB>first-last word alias<TAB>readable source preview.
Adjacent Cues may reuse a meaning-group alias. Finish with END.
""".strip(),
    "CALIBRATE": """
EXPERIMENTAL OUTPUT CONTRACT (this final section overrides every earlier JSON/action-output instruction):
Return only SUBSTAR-CUE-SCRIPT/1 text. Do not return JSON, Markdown, commentary, actions, token IDs, or internal IDs.
Return every OWN Cue exactly once as: CUE<TAB>local Cue alias<TAB>the complete corrected source-language Cue text.
Keep CONTEXT Cues read-only. Preserve meaning, word order, and Cue boundaries. You may correct recognition,
case, punctuation, terminology, and merge written forms such as "U.S.". Finish with END.
""".strip(),
    "TRANSLATE": """
EXPERIMENTAL OUTPUT CONTRACT (this final section overrides every earlier JSON/mapping-output instruction):
Return only SUBSTAR-CUE-SCRIPT/1 text. Do not return JSON, Markdown, commentary, group IDs, or internal IDs.
Return every OWN Cue exactly once as: CUE<TAB>local Cue alias<TAB>complete target-language text for that display slot.
Use neighboring Cues and group labels for context, but never combine, omit, or renumber Cue slots. Finish with END.
""".strip(),
}


def output_contract(task: str) -> str:
    return OUTPUT_CONTRACTS[task.upper()]


class CueScriptError(ValueError):
    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = [str(issue) for issue in issues if str(issue)]
        super().__init__("; ".join(self.issues) or "Cue Script 无效")


def _body(raw: str) -> list[str]:
    value = str(raw or "").strip()
    fenced = re.fullmatch(r"```(?:text)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE)
    if fenced:
        value = fenced.group(1).strip()
    return [line.rstrip("\r") for line in value.splitlines() if line.strip()]


def records(raw: str, task: str) -> list[list[str]]:
    lines = _body(raw)
    expected = f"{HEADER}\t{task.upper()}"
    localized_headers = {
        "CALIBRATE": {"子星-CUE-SCRIPT/1\t校准"},
        "TRANSLATE": {"子星-CUE-SCRIPT/1\t翻译"},
        "SEGMENT": {"子星-CUE-SCRIPT/1\t切分"},
    }
    accepted_headers = {expected, *localized_headers.get(task.upper(), set())}
    has_header = bool(lines and lines[0] in accepted_headers)
    has_trailer = bool(lines and lines[-1] in {"END", "结束"})
    body_start = 1 if has_header else 0
    body_end = -1 if has_trailer else len(lines)
    body_lines = lines[body_start:body_end]
    issues: list[str] = []
    # Providers sometimes drop only the envelope while preserving every
    # tab-delimited record. The record shape and full alias coverage are the
    # actual safety boundary, so that envelope omission is recoverable.
    if not has_header and any(not line.startswith("CUE\t") for line in body_lines):
        issues.append(f"首行必须是 {expected}")
    if not has_trailer and any(not line.startswith("CUE\t") for line in body_lines):
        issues.append("末行必须是 END")
    rows = [line.split("\t") for line in body_lines]
    if not rows:
        issues.append("结果没有记录")
    if issues:
        raise CueScriptError(issues)
    return rows


def alias_rows(rows: Iterable[Mapping[str, Any]], prefix: str) -> tuple[dict[str, Mapping[str, Any]], dict[str, str]]:
    by_alias: dict[str, Mapping[str, Any]] = {}
    by_id: dict[str, str] = {}
    for number, row in enumerate(rows, start=1):
        alias = f"{prefix}{number:03d}"
        by_alias[alias] = row
        identity = str(row.get("cue_id") or row.get("group_id") or row.get("token_id") or row.get("index"))
        if identity:
            by_id[identity] = alias
    return by_alias, by_id


@dataclass(frozen=True)
class SegmentationLedger:
    words: dict[str, Mapping[str, Any]]
    aliases_by_index: dict[int, str]


def render_segmentation_request(request: Mapping[str, Any]) -> tuple[str, SegmentationLedger]:
    rows = [dict(row) for row in request.get("rows", []) if isinstance(row, Mapping)]
    words: dict[str, Mapping[str, Any]] = {}
    aliases_by_index: dict[int, str] = {}
    lines = [
        "TASK\tSEGMENT",
        f"LANGUAGE\t{request.get('active_output_profile', {}).get('source_language', 'Auto')}",
        f"HARD_LIMIT\t{request.get('active_output_profile', {}).get('hard_limit', '')}",
        "TOKENS",
    ]
    for number, row in enumerate(rows, start=1):
        alias = f"W{number:04d}"
        words[alias] = row
        aliases_by_index[int(row["index"])] = alias
        lines.append(
            "\t".join((
                alias,
                "OWN" if bool(row.get("owner")) else "CONTEXT",
                str(row.get("start", "")),
                str(row.get("end", "")),
                str(row.get("text", "")).replace("\t", " ").replace("\n", " "),
            ))
        )
    lines.extend((
        "OUTPUT FORMAT",
        f"{HEADER}\tSEGMENT",
        "CUE\tC001\tG001\tW0001-W0003\treadable source preview",
        "END",
        "Return consecutive CUE rows. Word ranges must cover every OWN token exactly once. "
        "Adjacent Cues may share a G id when they belong to one meaning group. Context words are read-only.",
    ))
    return "\n".join(lines), SegmentationLedger(words, aliases_by_index)


def parse_segmentation(raw: str, ledger: SegmentationLedger, binding: Mapping[str, Any]) -> dict[str, Any]:
    rows = records(raw, "SEGMENT")
    issues: list[str] = []
    parsed: list[tuple[str, str, int, int]] = []
    seen_cues: set[str] = set()
    expected_cue = 1
    for number, fields in enumerate(rows, start=1):
        if len(fields) != 5 or fields[0] != "CUE":
            issues.append(f"第 {number} 条必须是五字段 CUE 记录")
            continue
        cue, group, word_range = fields[1], fields[2], fields[3]
        if cue != f"C{expected_cue:03d}" or cue in seen_cues:
            issues.append(f"第 {number} 条 Cue 必须连续编号")
        expected_cue += 1
        seen_cues.add(cue)
        match = re.fullmatch(r"(W\d{4})-(W\d{4})", word_range)
        if not match or match.group(1) not in ledger.words or match.group(2) not in ledger.words:
            issues.append(f"{cue} 的词元范围无效")
            continue
        first = ledger.words[match.group(1)]
        last = ledger.words[match.group(2)]
        start, end = int(first["index"]), int(last["index"])
        if start > end or not bool(first.get("owner")) or not bool(last.get("owner")):
            issues.append(f"{cue} 使用了倒序或只读范围")
            continue
        parsed.append((cue, group, start, end))
    ownership = binding.get("ownership", {})
    cursor = int(ownership.get("alignment_start", -1))
    final = int(ownership.get("alignment_end", -1))
    for cue, _group, start, end in parsed:
        if start != cursor:
            issues.append(f"{cue} 未从预期词元 {cursor} 开始")
        cursor = end + 1
    if cursor != final + 1:
        issues.append("Cue 范围没有完整覆盖 owned 词元")
    if issues:
        raise CueScriptError(issues)
    groups: list[dict[str, Any]] = []
    for _cue, group_alias, start, end in parsed:
        if groups and groups[-1]["alias"] == group_alias:
            groups[-1]["alignment_end"] = end
            groups[-1]["line_breaks_after"].append(end)
        else:
            groups.append({
                "alias": group_alias,
                "alignment_start": start,
                "alignment_end": end,
                "line_breaks_after": [end],
            })
    return {
        "schema_version": "substar.semantic-grouping-result.v1",
        "input_fingerprint": str(binding.get("input_fingerprint", "")),
        "block_id": str(binding.get("block_id", "")),
        "ownership": dict(binding.get("ownership", {})),
        "meaning_groups": [
            {key: value for key, value in group.items() if key != "alias"}
            for group in groups
        ],
        "exceptions": [],
    }


@dataclass(frozen=True)
class CueLedger:
    cues: dict[str, Mapping[str, Any]]
    aliases_by_id: dict[str, str]
    editable_aliases: tuple[str, ...]


def render_cue_request(
    cues: Iterable[Mapping[str, Any]], *, task: str, instructions: str,
) -> tuple[str, CueLedger]:
    rows = [dict(cue) for cue in cues]
    by_alias, by_id = alias_rows(rows, "C")
    editable = tuple(alias for alias, cue in by_alias.items() if bool(cue.get("editable", True)))
    lines = [f"TASK\t{task.upper()}", "CUES"]
    for alias, cue in by_alias.items():
        tokens = cue.get("tokens") if isinstance(cue.get("tokens"), list) else []
        source = str(cue.get("source_text") or " ".join(str(token.get("text", "")) for token in tokens)).strip()
        lines.append("\t".join((
            alias,
            "OWN" if alias in editable else "CONTEXT",
            source.replace("\t", " ").replace("\n", " "),
        )))
    lines.extend(("OUTPUT FORMAT", f"{HEADER}\t{task.upper()}", "CUE\tC001\ttext", "END", instructions))
    return "\n".join(lines), CueLedger(by_alias, by_id, editable)


def parse_cue_text(raw: str, task: str, ledger: CueLedger, *, require_all: bool = True) -> dict[str, str]:
    rows = records(raw, task)
    result: dict[str, str] = {}
    issues: list[str] = []
    for number, fields in enumerate(rows, start=1):
        if len(fields) < 3 or fields[0] != "CUE":
            if require_all:
                issues.append(f"第 {number} 条必须是三字段 CUE 记录")
            continue
        alias, text = fields[1], " ".join(fields[2:]).strip()
        if alias in ledger.cues and alias not in ledger.editable_aliases:
            # Some models copy the read-only halo despite the instruction.
            # It is safe and deterministic to ignore those rows.
            continue
        if alias not in ledger.editable_aliases:
            if require_all:
                issues.append(f"{alias} 不是当前可编辑 Cue")
        elif alias in result:
            # Keep the first binding. A duplicate can never steal another
            # alias; any genuinely missing Cue remains visible to validation.
            continue
        elif not text:
            if require_all:
                issues.append(f"{alias} 文本为空")
        else:
            result[alias] = text
    if require_all:
        missing = [alias for alias in ledger.editable_aliases if alias not in result]
        if missing:
            issues.append("缺少 Cue：" + ", ".join(missing))
    if issues:
        raise CueScriptError(issues)
    return result


def render_translation_request(
    groups: Iterable[Mapping[str, Any]], *, mapping_mode: str,
) -> tuple[str, CueLedger]:
    """Render model-friendly local aliases while retaining group context."""
    cues: list[dict[str, Any]] = []
    for group_number, group in enumerate(groups, start=1):
        group_alias = f"G{group_number:03d}"
        for cue in group.get("cues", []):
            if isinstance(cue, Mapping):
                cues.append({**dict(cue), "local_group_alias": group_alias, "editable": True})
    by_alias, by_id = alias_rows(cues, "C")
    lines = [
        "TASK\tTRANSLATE",
        f"MAPPING_MODE\t{mapping_mode}",
        "CUES",
    ]
    for alias, cue in by_alias.items():
        lines.append("\t".join((
            alias,
            str(cue["local_group_alias"]),
            "OWN",
            str(cue.get("source_text", "")).replace("\t", " ").replace("\n", " "),
        )))
    lines.extend((
        "OUTPUT FORMAT",
        f"{HEADER}\tTRANSLATE",
        "CUE\tC001\ttarget-language text",
        "END",
        "Return every OWN Cue exactly once; use its local alias unchanged.",
    ))
    return "\n".join(lines), CueLedger(
        by_alias, by_id, tuple(by_alias)
    )


def finalize_translation(
    raw: str, groups: Iterable[Mapping[str, Any]], ledger: CueLedger,
    *, mapping_mode: str,
) -> dict[str, Any]:
    """Compile full-Cue text into the existing frozen translation contract."""
    targets = parse_cue_text(raw, "TRANSLATE", ledger, require_all=False)
    by_cue_id = {
        str(cue.get("cue_id")): targets[alias]
        for alias, cue in ledger.cues.items()
        if alias in targets
    }
    results: list[dict[str, Any]] = []
    for group in groups:
        group_id = str(group["group_id"])
        cues = [dict(cue) for cue in group.get("cues", [])]
        if any(str(cue.get("cue_id")) not in by_cue_id for cue in cues):
            # Return a partial canonical response. Existing delivery logic will
            # repair only these groups while preserving every valid Cue.
            continue
        if mapping_mode == "one_to_one":
            cue_id = str(cues[0]["cue_id"])
            results.append({
                "group_id": group_id,
                "cue_id": cue_id,
                "target_text": by_cue_id[cue_id],
            })
            continue
        units: list[dict[str, Any]] = []
        assignments: list[dict[str, str]] = []
        for cue in cues:
            cue_id = str(cue["cue_id"])
            target = by_cue_id[cue_id]
            # Reusing an identical adjacent translation is the explicit,
            # deterministic representation of a many-to-many persistent unit.
            if units and units[-1]["target_text"] == target:
                units[-1]["source_evidence_cue_ids"].append(cue_id)
                unit_id = str(units[-1]["meaning_unit_id"])
            else:
                unit_id = f"unit_{len(units) + 1}"
                units.append({
                    "meaning_unit_id": unit_id,
                    "target_text": target,
                    "source_evidence_cue_ids": [cue_id],
                })
            assignments.append({"cue_id": cue_id, "meaning_unit_id": unit_id})
        results.append({
            "group_id": group_id,
            "meaning_units": units,
            "cue_assignments": assignments,
        })
    return {"group_results": results}


def _calibration_action(
    *, action_id: str, kind: str, token_ids: list[str], before: str,
    after: str, confidence: str = "high", disposition: str = "apply",
) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "kind": kind,
        "token_ids": token_ids,
        "before_text": before,
        "after_text": after,
        "confidence": confidence,
        "evidence": [{
            "kind": "context",
            "reference": "Cue Script full-Cue deterministic finalizer",
        }],
        "disposition": disposition,
        "affects_translation": kind in {"replace_token", "replace_span", "merge_span"},
    }


def _light_core(value: str) -> str:
    return str(value).rstrip(".,?!;:，。？！；：、…-—–")


def _light_suffix(value: str) -> str:
    value = str(value)
    return value[len(_light_core(value)):]


def _alnum(value: str) -> str:
    return "".join(char.casefold() for char in str(value) if char.isalnum())


def _contains_unsupported_symbol(value: str) -> bool:
    """Reject model decorations that are neither text nor supported punctuation."""
    supported = set(".,?!;:，。？！；：、…-—–'\"")
    return any(
        not char.isalnum() and not char.isspace() and char not in supported
        for char in str(value)
    )


def _align_calibration(old: list[str], new: list[str]) -> list[tuple[int, int, int, int, str]]:
    """Align conservatively; unsupported insert/delete edits stay unchanged."""
    matcher = SequenceMatcher(
        None, [_alnum(value) for value in old], [_alnum(value) for value in new],
        autojunk=False,
    )
    result: list[tuple[int, int, int, int, str]] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        old_count, new_count = old_end - old_start, new_end - new_start
        if tag == "equal" or old_count == new_count:
            for offset in range(min(old_count, new_count)):
                result.append((
                    old_start + offset, old_start + offset + 1,
                    new_start + offset, new_start + offset + 1, "one",
                ))
        elif new_count == 1 and old_count >= 2 and (
            _alnum(" ".join(old[old_start:old_end])) == _alnum(new[new_start])
        ):
            result.append((old_start, old_end, new_start, new_end, "merge"))
        # Inserts, deletions, splits and count-changing rewrites cannot be
        # represented by the frozen editor action vocabulary. They are ignored
        # locally instead of invalidating every safe correction in the block.
    return result


def finalize_calibration(raw: str, ledger: CueLedger) -> dict[str, Any]:
    """Compile full corrected Cue text to validated actions bound to real token IDs."""
    corrected = parse_cue_text(raw, "CALIBRATE", ledger)
    actions: list[dict[str, Any]] = []
    sequence = 0
    for alias in ledger.editable_aliases:
        cue = ledger.cues[alias]
        tokens = [dict(token) for token in cue.get("tokens", [])]
        old = [str(token.get("text", "")) for token in tokens]
        new = corrected[alias].split()
        alignment = _align_calibration(old, new)
        for old_start, old_end, new_start, _new_end, kind in alignment:
            next_text = new[new_start]
            before_tokens = old[old_start:old_end]
            token_ids = [str(token["token_id"]) for token in tokens[old_start:old_end]]
            before_text = " ".join(before_tokens)
            if kind == "merge":
                if before_text != next_text:
                    sequence += 1
                    actions.append(_calibration_action(
                        action_id=f"cs_{alias}_{sequence:03d}", kind="merge_span",
                        token_ids=token_ids, before=before_text, after=next_text,
                    ))
                continue
            if before_text == next_text:
                continue
            old_core, new_core = _light_core(before_text), _light_core(next_text)
            if old_core.casefold() == new_core.casefold():
                intermediate = new_core + _light_suffix(before_text)
                if intermediate != before_text:
                    sequence += 1
                    actions.append(_calibration_action(
                        action_id=f"cs_{alias}_{sequence:03d}", kind="set_case",
                        token_ids=token_ids, before=before_text, after=intermediate,
                    ))
                if next_text != intermediate:
                    sequence += 1
                    actions.append(_calibration_action(
                        action_id=f"cs_{alias}_{sequence:03d}", kind="set_punctuation",
                        token_ids=token_ids, before=intermediate, after=next_text,
                    ))
            else:
                similarity = SequenceMatcher(
                    None, before_text.casefold(), next_text.casefold(), autojunk=False
                ).ratio()
                safe_lexical = (
                    similarity >= 0.75
                    and not _contains_unsupported_symbol(next_text)
                )
                sequence += 1
                actions.append(_calibration_action(
                    action_id=f"cs_{alias}_{sequence:03d}", kind="replace_token",
                    token_ids=token_ids, before=before_text, after=next_text,
                    confidence="medium" if safe_lexical else "low",
                    disposition="apply" if safe_lexical else "review",
                ))
    return {"actions": actions}
