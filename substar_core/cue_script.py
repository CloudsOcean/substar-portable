from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any, Iterable, Mapping


HEADER = "SUBSTAR-CUE-SCRIPT/1"


OUTPUT_CONTRACTS = {
    "SEGMENT": """
AUTHORITATIVE OUTPUT CONTRACT:
Return only SUBSTAR-CUE-SCRIPT/1 text; no JSON, Markdown, commentary, token IDs, or internal IDs.
Use CUE<TAB>Cue alias<TAB>word span. Write W0001 for one word or W0002-W0004 for an inclusive multi-word range.
Cover every OWN word exactly once, consecutively and in order. Never return CONTEXT words. Finish with END.
""".strip(),
    "CALIBRATE": """
AUTHORITATIVE OUTPUT CONTRACT:
Return only SUBSTAR-CUE-SCRIPT/1 text; no JSON, Markdown, commentary, actions, token IDs, or internal IDs.
Use CUE<TAB>local Cue alias<TAB>complete corrected source-language Cue text. Return every OWN Cue exactly once.
Never return CONTEXT Cues. Preserve meaning, word order and Cue boundaries. Finish with END.
""".strip(),
    "TRANSLATE": """
AUTHORITATIVE OUTPUT CONTRACT:
Return only mapping rows; no header, END, JSON, Markdown, commentary, group IDs, or internal IDs.
Use local Cue alias<TAB>complete target-language subtitle text. In many_to_many mode, consecutive aliases may be joined with + and share one right-hand text; in one_to_one mode each row has one alias.
Return every OWN alias exactly once and in input order. Never return CONTEXT aliases or copy aliases into subtitle text.
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


def records(raw: str, task: str, *, strict: bool = True) -> list[list[str]]:
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
    if strict and not has_header and any(not line.startswith("CUE\t") for line in body_lines):
        issues.append(f"首行必须是 {expected}")
    if strict and not has_trailer and any(not line.startswith("CUE\t") for line in body_lines):
        issues.append("末行必须是 END")
    rows = [line.split("\t") for line in body_lines]
    if strict and not rows:
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
        "RETURN",
        f"{HEADER}\tSEGMENT",
        "CUE\tC001\tW0001",
        "CUE\tC002\tW0002-W0004",
        "END",
    ))
    return "\n".join(lines), SegmentationLedger(words, aliases_by_index)


def parse_segmentation(
    raw: str, ledger: SegmentationLedger, binding: Mapping[str, Any], *,
    require_all: bool = True,
) -> dict[str, Any]:
    """Compile C/W rows; legacy five-column rows remain read-compatible."""
    rows = records(raw, "SEGMENT", strict=require_all)
    issues: list[str] = []
    parsed: list[tuple[str, str, int, int]] = []
    expected_cue = 1
    for number, fields in enumerate(rows, start=1):
        if len(fields) not in {3, 4, 5} or fields[0] != "CUE":
            if require_all:
                issues.append(f"第 {number} 条必须是三字段 CUE 记录")
            continue
        cue = f"C{expected_cue:03d}"
        # v1 experimental output carried a G column. It is deliberately
        # ignored here so old cached responses remain readable without letting
        # G influence the new Cue boundary contract.
        word_range = fields[3] if len(fields) == 5 else fields[2]
        # C labels are presentation ordinals; W ranges carry the binding.
        # Providers occasionally continue a global counter or decorate the
        # ordinal with its owned range (for example C480 or C168-175-01).
        # Canonicalize the label by row order: it has no authority over token
        # binding, so accepting it cannot move or steal a word.
        expected_cue += 1
        match = re.fullmatch(r"(W\d{4})(?:-(W\d{4}))?", word_range)
        first_alias = match.group(1) if match else ""
        last_alias = (match.group(2) or first_alias) if match else ""
        if not match or first_alias not in ledger.words or last_alias not in ledger.words:
            if require_all:
                issues.append(f"{cue} 的词元范围无效")
            continue
        first = ledger.words[first_alias]
        last = ledger.words[last_alias]
        start, end = int(first["index"]), int(last["index"])
        if start > end or not bool(first.get("owner")) or not bool(last.get("owner")):
            if require_all:
                issues.append(f"{cue} 使用了倒序或只读范围")
            continue
        parsed.append((cue, "", start, end))
    ownership = binding.get("ownership", {})
    cursor = int(ownership.get("alignment_start", -1))
    final = int(ownership.get("alignment_end", -1))
    for cue, _group, start, end in parsed:
        if require_all and start != cursor:
            issues.append(f"{cue} 未从预期词元 {cursor} 开始")
        cursor = end + 1
    if require_all and cursor != final + 1:
        issues.append("Cue 范围没有完整覆盖 owned 词元")
    if issues:
        raise CueScriptError(issues)
    groups: list[dict[str, Any]] = []
    for _cue, _legacy_group_alias, start, end in parsed:
        groups.append({
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
            dict(group) for group in groups
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
    repair_feedback: Mapping[str, Any] | None = None,
) -> tuple[str, CueLedger]:
    rows = [dict(cue) for cue in cues]
    by_alias, by_id = alias_rows(rows, "C")
    editable = tuple(alias for alias, cue in by_alias.items() if bool(cue.get("editable", True)))
    lines = [f"TASK\t{task.upper()}", "CUES"]
    for alias, cue in by_alias.items():
        tokens = cue.get("tokens") if isinstance(cue.get("tokens"), list) else []
        source = str(cue.get("source_text") or " ".join(str(token.get("text", "")) for token in tokens)).strip()
        lines.append("\t".join((
            "SRC",
            alias,
            "OWN" if alias in editable else "CONTEXT",
            source.replace("\t", " ").replace("\n", " "),
        )))
    if repair_feedback:
        issues = repair_feedback.get("program_validation_errors", [])
        lines.extend(("", "PROGRAM VALIDATION"))
        for issue in issues if isinstance(issues, list) else []:
            if not isinstance(issue, Mapping):
                continue
            cue_id = str(issue.get("cue_id") or "")
            alias = by_id.get(cue_id, str(issue.get("alias") or ""))
            lines.append("\t".join((
                "ERROR", alias or "BLOCK",
                str(issue.get("code") or "invalid_output"),
                str(issue.get("detail") or "").replace("\t", " ").replace("\n", " "),
            )))
        lines.append(
            "PATCH RULE\tReturn only OWN aliases. CONTEXT aliases and accepted edits are frozen."
        )
    lines.extend(("RETURN", f"{HEADER}\t{task.upper()}", "CUE\tC001\ttext", "END"))
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


def parse_cue_text_candidate(
    raw: str, task: str, ledger: CueLedger,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Salvage unambiguous C rows and report only the local repair scope."""
    rows = records(raw, task, strict=False)
    result: dict[str, str] = {}
    issues: list[dict[str, Any]] = []
    for _number, fields in enumerate(rows, start=1):
        # The alias is the binding authority. A provider omitting only the
        # redundant CUE record tag is therefore a harmless surface variation:
        # both `CUE<TAB>C001<TAB>text` and `C001<TAB>text` are unambiguous.
        if len(fields) >= 3 and fields[0] == "CUE":
            alias, value = fields[1], " ".join(fields[2:]).strip()
        elif len(fields) >= 2 and re.fullmatch(r"C\d{3}", fields[0]):
            alias, value = fields[0], " ".join(fields[1:]).strip()
        else:
            continue
        if alias in ledger.cues and alias not in ledger.editable_aliases:
            continue
        if alias not in ledger.editable_aliases or alias in result or not value:
            continue
        result[alias] = value
    for alias in ledger.editable_aliases:
        if alias not in result:
            cue = ledger.cues[alias]
            issues.append({
                "code": "missing_cue_text",
                "alias": alias,
                "cue_id": str(cue.get("cue_id") or ""),
                "detail": f"{alias} 缺少可绑定的完整校准文本",
            })
    return result, issues


def render_translation_request(
    groups: Iterable[Mapping[str, Any]], *, mapping_mode: str,
) -> tuple[str, CueLedger]:
    """Render one block-wide C namespace without exposing internal groups."""
    cues: list[dict[str, Any]] = []
    for group in groups:
        for cue in group.get("cues", []):
            if isinstance(cue, Mapping):
                cues.append({**dict(cue), "editable": bool(cue.get("editable", True))})
    for cue_number, cue in enumerate(cues, start=1):
        cue["local_alias"] = f"C{cue_number:03d}"
    by_alias = {str(cue["local_alias"]): cue for cue in cues}
    by_id = {str(cue["cue_id"]): str(cue["local_alias"]) for cue in cues}
    lines = [
        "TASK\tTRANSLATE",
        f"MAPPING_MODE\t{mapping_mode}",
    ]
    limit_profiles = {
        (
            int(cue.get("hard_limit") or 0),
            str(cue.get("count_rule") or "all_characters_including_spaces"),
        )
        for cue in by_alias.values() if int(cue.get("hard_limit") or 0) > 0
    }
    if len(limit_profiles) == 1:
        limit, count_rule = next(iter(limit_profiles))
        lines.extend((
            f"TARGET_LIMIT\t{limit}",
            f"COUNT_RULE\t{count_rule}",
        ))
    elif limit_profiles:
        lines.append("TARGET_LIMITS")
        for alias, cue in by_alias.items():
            lines.append("\t".join((
                "LIMIT", alias, str(int(cue.get("hard_limit") or 0)),
                str(cue.get("count_rule") or "all_characters_including_spaces"),
            )))
    lines.append("CUES")
    editable = tuple(
        alias for alias, cue in by_alias.items() if bool(cue.get("editable", True))
    )
    for alias, cue in by_alias.items():
        lines.append("\t".join((
            alias,
            "OWN" if alias in editable else "CONTEXT",
            str(cue.get("source_text", "")).replace("\t", " ").replace("\n", " "),
        )))
    repair_issues = [
        issue
        for group in groups
        for issue in (
            group.get("program_validation_errors", [])
            if isinstance(group.get("program_validation_errors"), list) else []
        )
        if isinstance(issue, Mapping)
    ]
    if repair_issues or len(editable) != len(by_alias):
        lines.extend(("", "PROGRAM VALIDATION"))
        for issue in repair_issues:
            raw_ids = issue.get("cue_ids")
            cue_ids = (
                [str(value) for value in raw_ids]
                if isinstance(raw_ids, list)
                else [str(issue.get("cue_id") or "")]
            )
            aliases = [by_id[cue_id] for cue_id in cue_ids if cue_id in by_id]
            code = str(issue.get("code") or "invalid_output")
            rendered_code = code
            if code == "target_over_limit":
                rendered_code = "TARGET_OVER_LIMIT"
                detail = " ".join((
                    f"ACTUAL={issue.get('count', '')}",
                    f"REQUIRED_MAX={issue.get('limit', '')}",
                    "ACTION=shorten_or_split",
                    "REJECTED=" + str(issue.get("target_text") or ""),
                ))
            else:
                detail = str(issue.get("detail") or "")
            lines.append("\t".join((
                "ERROR", "+".join(aliases) or "BLOCK", rendered_code,
                detail.replace("\t", " ").replace("\n", " "),
            )))
        lines.append(
            "PATCH RULE\tReturn every OWN alias exactly once. Do not return CONTEXT aliases or repeat frozen text."
        )
    example = (
        "C001+C002\tshared target-language subtitle text"
        if mapping_mode == "many_to_many"
        else "C001\ttarget-language text"
    )
    lines.extend((
        "RETURN",
        example,
    ))
    return "\n".join(lines), CueLedger(by_alias, by_id, editable)


def compile_translation_units(
    groups: Iterable[Mapping[str, Any]], units: Iterable[Mapping[str, Any]], *,
    mapping_mode: str,
) -> dict[str, Any]:
    """Compile frozen Cue-owned text units into the legacy delivery contract."""
    normalized: list[dict[str, Any]] = []
    cue_to_unit: dict[str, int] = {}
    for raw in units:
        cue_ids = [str(value) for value in raw.get("cue_ids", []) if str(value)]
        target = str(raw.get("target_text") or "").strip()
        if not cue_ids or not target or any(cue_id in cue_to_unit for cue_id in cue_ids):
            continue
        index = len(normalized)
        normalized.append({"cue_ids": cue_ids, "target_text": target})
        for cue_id in cue_ids:
            cue_to_unit[cue_id] = index

    results: list[dict[str, Any]] = []
    for raw_group in groups:
        group = dict(raw_group)
        group_id = str(group["group_id"])
        cues = [dict(cue) for cue in group.get("cues", [])]
        cue_ids = [str(cue.get("cue_id") or "") for cue in cues]
        if not cue_ids or any(cue_id not in cue_to_unit for cue_id in cue_ids):
            continue
        if mapping_mode == "one_to_one":
            if len(cue_ids) != 1:
                continue
            results.append({
                "group_id": group_id,
                "cue_id": cue_ids[0],
                "target_text": normalized[cue_to_unit[cue_ids[0]]]["target_text"],
            })
            continue
        meaning_units: list[dict[str, Any]] = []
        assignments: list[dict[str, str]] = []
        local_unit_ids: dict[int, str] = {}
        group_set = set(cue_ids)
        for cue_id in cue_ids:
            unit_index = cue_to_unit[cue_id]
            if unit_index not in local_unit_ids:
                unit_id = f"unit_{len(meaning_units) + 1}"
                local_unit_ids[unit_index] = unit_id
                unit = normalized[unit_index]
                # Old execution groups are an internal storage boundary only.
                # If a valid wire row crosses one, retain the shared target but
                # expose only evidence owned by this canonical group.
                evidence = [value for value in unit["cue_ids"] if value in group_set]
                meaning_units.append({
                    "meaning_unit_id": unit_id,
                    "target_text": unit["target_text"],
                    "source_evidence_cue_ids": evidence,
                })
            assignments.append({
                "cue_id": cue_id,
                "meaning_unit_id": local_unit_ids[unit_index],
            })
        results.append({
            "group_id": group_id,
            "meaning_units": meaning_units,
            "cue_assignments": assignments,
        })
    return {"group_results": results}


def finalize_translation(
    raw: str, groups: Iterable[Mapping[str, Any]], ledger: CueLedger,
    *, mapping_mode: str,
) -> dict[str, Any]:
    """Compile explicit alias mappings into the frozen translation contract."""
    lines = _body(raw)
    all_alias_order = {alias: index for index, alias in enumerate(ledger.cues)}
    editable = set(ledger.editable_aliases)
    alias_pattern = r"C\d{3}"
    parsed_units: list[tuple[tuple[str, ...], str]] = []
    seen: set[str] = set()
    warnings: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        value = re.sub(r"^\s*[-*]\s+", "", line.strip())
        value = re.sub(r"<TAB>", "\t", value, flags=re.IGNORECASE)
        previous = None
        while previous != value:
            previous = value
            value = re.sub(
                r"(C\d{3}(?:\s*\+\s*C\d{3})*)\s*\+\s*(\d{3})(?=\D|$)",
                r"\1+C\2", value,
            )
        tab_parts = [part.strip() for part in value.split("\t")]
        leading_aliases: list[str] = []
        leading_fields = 0
        for part in tab_parts[:-1]:
            if re.fullmatch(rf"{alias_pattern}(?:\s*\+\s*{alias_pattern})*", part):
                leading_aliases.extend(
                    alias.strip() for alias in part.split("+")
                )
                leading_fields += 1
            else:
                break
        if (
            mapping_mode == "many_to_many"
            and len(leading_aliases) >= 2
            and len(set(leading_aliases)) == len(leading_aliases)
        ):
            # Some providers spell a join as C001<TAB>C002<TAB>text. The
            # fixed aliases and trailing free-text field make this equivalent
            # to C001+C002<TAB>text without guessing any binding.
            value = "+".join(leading_aliases) + "\t" + "\t".join(
                tab_parts[leading_fields:]
            )
        match = re.fullmatch(
            # C aliases have a fixed, closed grammar, so a provider dropping
            # only the separator is still unambiguous (for example
            # ``C001译文``). Salvage that row instead of spending a repair
            # request on typography. Alias joins still require an explicit +.
            rf"({alias_pattern}(?:\s*\+\s*{alias_pattern})*)(?:\t+| +)?(.+)",
            value,
        )
        if not match:
            # Primary execution may still salvage every complete group whose
            # rows are valid. A malformed row can never acquire a binding.
            continue
        aliases = tuple(part.strip() for part in match.group(1).split("+"))
        text = match.group(2).strip()
        alias_echo = re.match(
            rf"^({alias_pattern}(?:\s*\+\s*{alias_pattern})*)\t+(.+)$", text
        )
        if alias_echo and re.sub(r"\s+", "", alias_echo.group(1)) == re.sub(
            r"\s+", "", match.group(1)
        ):
            text = alias_echo.group(2).strip()
        if text.upper().startswith("TAB\t"):
            text = text[4:].strip()
        cleaned = re.sub(r"^(?:<[^>\n]+>\s*)+", "", text).strip()
        if cleaned:
            text = cleaned
        if not text:
            continue
        if mapping_mode == "one_to_one" and len(aliases) != 1:
            warnings.append({"code": "joined_alias_in_one_to_one", "line": number})
            continue
        if any(alias not in all_alias_order for alias in aliases):
            warnings.append({"code": "unknown_alias", "line": number})
            continue
        if all(alias not in editable for alias in aliases):
            # Models occasionally echo read-only context. It cannot overwrite
            # frozen data and does not justify another repair request.
            continue
        if any(alias not in editable for alias in aliases):
            ignored = [alias for alias in aliases if alias not in editable]
            aliases = tuple(alias for alias in aliases if alias in editable)
            warnings.append({
                "code": "context_aliases_ignored", "line": number,
                "aliases": ignored,
            })
        duplicate = [alias for alias in aliases if alias in seen]
        if duplicate:
            warnings.append({
                "code": "duplicate_alias_ignored", "line": number,
                "aliases": duplicate,
            })
            continue
        positions = [all_alias_order[alias] for alias in aliases]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            warnings.append({"code": "non_consecutive_aliases", "line": number})
            continue
        seen.update(aliases)
        parsed_units.append((aliases, text))
    missing_aliases = [alias for alias in ledger.editable_aliases if alias not in seen]
    if (
        mapping_mode == "one_to_one"
        and missing_aliases
        and len(ledger.editable_aliases) == len(ledger.cues)
    ):
        # One-to-one output order is frozen by the contract. If a provider
        # returns exactly one non-empty row per OWN Cue but drops or repeats
        # only the local labels, recover the labels deterministically. Do not
        # use this for repair requests with CONTEXT Cues or for many-to-many,
        # where row count is intentionally not fixed.
        positional: list[tuple[tuple[str, ...], str]] = []
        explicit: list[str | None] = []
        unusable = False
        for line in lines:
            value = re.sub(r"^\s*[-*]\s+", "", line.strip())
            value = re.sub(r"<TAB>", "\t", value, flags=re.IGNORECASE)
            if (
                not value
                or value in {"END", "结束"}
                or value.startswith((HEADER, "TASK\t", "RETURN", "CUES"))
                or value[0] in "{}[]"
                or re.fullmatch(alias_pattern, value)
            ):
                unusable = True
                break
            match = re.fullmatch(rf"({alias_pattern})(?:\t+| +)(.+)", value)
            if match:
                alias, text = match.group(1), match.group(2).strip()
            else:
                alias, text = None, value.strip()
            if not text or re.fullmatch(rf"{alias_pattern}(?:\s*\+\s*{alias_pattern})+.*", value):
                unusable = True
                break
            explicit.append(alias)
            positional.append(((), text))
        expected = list(ledger.editable_aliases)
        if not unusable and len(positional) == len(expected):
            counts = {
                alias: explicit.count(alias) for alias in set(explicit) if alias is not None
            }
            contradicted = any(
                alias is not None
                and counts.get(alias, 0) == 1
                and alias in editable
                and alias != expected[index]
                for index, alias in enumerate(explicit)
            )
            if not contradicted:
                parsed_units = [
                    ((alias,), positional[index][1])
                    for index, alias in enumerate(expected)
                ]
                seen = set(expected)
                missing_aliases = []
                warnings.append({
                    "code": "positional_aliases_restored",
                    "row_count": len(expected),
                })
    wire_units = [{
        "cue_ids": [str(ledger.cues[alias].get("cue_id") or "") for alias in aliases],
        "target_text": text,
    } for aliases, text in parsed_units]
    missing_aliases = [alias for alias in ledger.editable_aliases if alias not in seen]
    issues = [{
        "code": "missing_cue_translation",
        "alias": alias,
        "cue_id": str(ledger.cues[alias].get("cue_id") or ""),
        "detail": f"{alias} 缺少可绑定的非空译文",
    } for alias in missing_aliases]
    compiled = compile_translation_units(groups, wire_units, mapping_mode=mapping_mode)
    return {
        **compiled,
        "_wire_units": wire_units,
        "_covered_cue_ids": [
            str(ledger.cues[alias].get("cue_id") or "")
            for alias in ledger.editable_aliases if alias in seen
        ],
        "_cue_script_issues": issues,
        "_cue_script_warnings": warnings,
    }


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


def _calibration_actions(
    corrected: Mapping[str, str], ledger: CueLedger,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    sequence = 0
    for alias in ledger.editable_aliases:
        if alias not in corrected:
            continue
        cue = ledger.cues[alias]
        action_prefix = str(cue.get("cue_id") or alias)
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
                        action_id=f"cs_{action_prefix}_{sequence:03d}", kind="merge_span",
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
                        action_id=f"cs_{action_prefix}_{sequence:03d}", kind="set_case",
                        token_ids=token_ids, before=before_text, after=intermediate,
                    ))
                if next_text != intermediate:
                    sequence += 1
                    actions.append(_calibration_action(
                        action_id=f"cs_{action_prefix}_{sequence:03d}", kind="set_punctuation",
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
                    action_id=f"cs_{action_prefix}_{sequence:03d}", kind="replace_token",
                    token_ids=token_ids, before=before_text, after=next_text,
                    confidence="medium" if safe_lexical else "low",
                    disposition="apply" if safe_lexical else "review",
                ))
    return {"actions": actions}


def finalize_calibration(raw: str, ledger: CueLedger) -> dict[str, Any]:
    """Compile complete corrected Cue text to actions bound to real token IDs."""
    return _calibration_actions(
        parse_cue_text(raw, "CALIBRATE", ledger), ledger
    )


def finalize_calibration_candidate(raw: str, ledger: CueLedger) -> dict[str, Any]:
    """Freeze valid C rows while exposing omitted Cues as repairable issues."""
    corrected, issues = parse_cue_text_candidate(raw, "CALIBRATE", ledger)
    return {
        **_calibration_actions(corrected, ledger),
        "_cue_script_issues": issues,
        "_covered_cue_ids": [
            str(ledger.cues[alias].get("cue_id") or "") for alias in corrected
        ],
    }
