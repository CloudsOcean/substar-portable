from __future__ import annotations

import difflib
import html
import io
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from substar_core.language_layout import layout_tokens


REFERENCE_EXTENSIONS = {".txt", ".srt", ".docx"}
SRT_TIMING_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}.*$"
)


class ManuscriptMatchError(ValueError):
    pass


@dataclass(frozen=True)
class ReferenceToken:
    normalized: str
    text: str
    lexical: str


@dataclass(frozen=True)
class _LexicalSpan:
    start: int
    end: int
    lexical: str


REFERENCE_TOKENIZER_VERSION = "unicode-script-v2"
REFERENCE_BREAK_PRESETS = {
    "zh": "，。？！",
    "en": ".?!",
    "ja": "。！？",
    "ko": ".?!",
    "mixed": "，。？！.?!",
    "auto": "，。？！.?!",
}


def _language_key(language: str | None) -> str:
    value = str(language or "").strip().lower().replace("_", "-")
    if value.startswith("zh") or value in {"chinese", "mandarin"}:
        return "zh"
    if value.startswith("ja") or value == "japanese":
        return "ja"
    if value.startswith("ko") or value == "korean":
        return "ko"
    if value.startswith("en") or value == "english":
        return "en"
    if value.startswith("mixed") or value in {"zh-en", "en-zh"}:
        return "mixed"
    if value in {"", "auto", "automatic"}:
        return "auto"
    return value


def reference_break_symbols_for_language(language: str | None) -> str:
    """Return the deterministic reference-script boundary preset."""

    return REFERENCE_BREAK_PRESETS.get(
        _language_key(language), REFERENCE_BREAK_PRESETS["auto"]
    )


def _character_script(value: str) -> str:
    codepoint = ord(value)
    if (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    ):
        return "han"
    if (
        0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0x1B000 <= codepoint <= 0x1B16F
    ):
        return "kana"
    if (
        0x1100 <= codepoint <= 0x11FF
        or 0x3130 <= codepoint <= 0x318F
        or 0xA960 <= codepoint <= 0xA97F
        or 0xAC00 <= codepoint <= 0xD7AF
        or 0xD7B0 <= codepoint <= 0xD7FF
    ):
        return "hangul"
    return ""


def _is_word_base(value: str) -> bool:
    return unicodedata.category(value).startswith(("L", "N"))


def _is_mark(value: str) -> bool:
    return unicodedata.category(value).startswith("M")


def _is_internal_connector(value: str) -> bool:
    return value in {"'", "’", "-", "‐", "‑", "‒", "–", "—"}


_PROTECTED_LEXICAL_PATTERNS = (
    re.compile(r"(?:https?://|www\.)[^\s<>\[\]{}\"“”]+", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    re.compile(r"[vV]?\d+(?:\.\d+){1,}(?:[%‰])?"),
    re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:[%‰])?"),
    re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"),
    re.compile(r"(?:[A-Za-z]\.){2,}"),
)
_OPAQUE_TRAILING_PUNCTUATION = ".,!?;:，。？！；："


def _protected_lexical_end(value: str, index: int) -> int | None:
    """Return the end of an opaque written token beginning at ``index``."""

    for pattern in _PROTECTED_LEXICAL_PATTERNS:
        match = pattern.match(value, index)
        if match is None:
            continue
        end = match.end()
        if pattern is _PROTECTED_LEXICAL_PATTERNS[0]:
            while end > index and value[end - 1] in _OPAQUE_TRAILING_PUNCTUATION:
                end -= 1
        if end > index:
            return end
    return None


def _lexical_spans(text: str, language: str | None = None) -> list[_LexicalSpan]:
    """Tokenize Unicode scripts while retaining exact display spans."""

    del language
    value = str(text or "")
    spans: list[_LexicalSpan] = []
    index = 0
    while index < len(value):
        char = value[index]
        protected_end = _protected_lexical_end(value, index)
        if protected_end is not None:
            spans.append(_LexicalSpan(index, protected_end, value[index:protected_end]))
            index = protected_end
            continue
        character_script = _character_script(char)
        if character_script and character_script != "hangul":
            end = index + 1
            while end < len(value) and _is_mark(value[end]):
                end += 1
            spans.append(_LexicalSpan(index, end, value[index:end]))
            index = end
            continue
        if character_script == "hangul":
            end = index + 1
            while end < len(value):
                if _character_script(value[end]) == "hangul" or _is_mark(value[end]):
                    end += 1
                    continue
                break
            spans.append(_LexicalSpan(index, end, value[index:end]))
            index = end
            continue
        if not _is_word_base(char):
            index += 1
            continue
        end = index + 1
        while end < len(value):
            candidate = value[end]
            if _is_mark(candidate):
                end += 1
                continue
            if _is_word_base(candidate) and not _character_script(candidate):
                end += 1
                continue
            if (
                _is_internal_connector(candidate)
                and end + 1 < len(value)
                and _is_word_base(value[end + 1])
                and not _character_script(value[end + 1])
            ):
                end += 1
                continue
            break
        spans.append(_LexicalSpan(index, end, value[index:end]))
        index = end
    return spans


def _script_counts(text: str) -> dict[str, int]:
    counts = {"han": 0, "kana": 0, "hangul": 0, "latin": 0, "other": 0}
    for char in str(text or ""):
        script = _character_script(char)
        if script:
            counts[script] += 1
        elif _is_word_base(char):
            key = "latin" if "LATIN" in unicodedata.name(char, "") else "other"
            counts[key] += 1
    return counts


def _tokenization_diagnostics(text: str, language: str | None) -> dict[str, Any]:
    value = str(text or "")
    spans = _lexical_spans(value, language)
    linguistic_count = sum(
        1 for char in value if _is_word_base(char) or _is_mark(char)
    )
    covered_count = sum(
        1
        for span in spans
        for char in value[span.start:span.end]
        if _is_word_base(char) or _is_mark(char)
    )
    counts = _script_counts(value)
    configured = _language_key(language)
    effective = configured
    if configured == "auto":
        if counts["kana"]:
            effective = "ja"
        elif counts["hangul"]:
            effective = "ko"
        elif counts["han"] and counts["latin"]:
            effective = "mixed"
        elif counts["han"]:
            effective = "zh"
        elif counts["latin"]:
            effective = "en"
        else:
            effective = "unicode"
    script_mismatch = False
    if linguistic_count:
        if configured == "zh":
            script_mismatch = counts["han"] == 0
        elif configured == "ja":
            script_mismatch = counts["han"] + counts["kana"] == 0
        elif configured == "ko":
            script_mismatch = counts["hangul"] == 0
        elif configured in {"en", "de", "fr", "es"}:
            script_mismatch = counts["latin"] == 0
    coverage = covered_count / linguistic_count if linguistic_count else 0.0
    return {
        "tokenizer_version": REFERENCE_TOKENIZER_VERSION,
        "configured_language": configured,
        "effective_language": effective,
        "linguistic_character_count": linguistic_count,
        "linguistic_character_coverage": round(coverage, 6),
        "ignored_character_count": max(0, linguistic_count - covered_count),
        "script_counts": counts,
        "script_mismatch": script_mismatch,
    }


def _normalized_lexical(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("’", "'")
    return re.sub(r"[‐‑‒–—]", "-", normalized)


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ManuscriptMatchError("参考文稿编码无法识别")


def _docx_text(payload: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            raw = archive.read("word/document.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise ManuscriptMatchError("DOCX 参考文稿损坏") from exc
    paragraphs: list[str] = []
    for paragraph in re.findall(r"<w:p(?:\s[^>]*)?>(.*?)</w:p>", raw, re.DOTALL):
        paragraph = re.sub(r"<w:tab\s*/>", "\t", paragraph)
        chunks = re.findall(
            r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", paragraph, flags=re.DOTALL
        )
        paragraphs.append(html.unescape("".join(chunks)))
    if paragraphs:
        return "\n".join(paragraphs)
    return html.unescape(re.sub(r"<[^>]+>", "", raw))


def _srt_text(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        value = raw.strip()
        if not value or value.isdigit() or SRT_TIMING_RE.match(value):
            continue
        lines.append(re.sub(r"<[^>]+>", "", value))
    return "\n".join(lines)


def extract_reference_text(payload: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in REFERENCE_EXTENSIONS:
        raise ManuscriptMatchError("参考文稿只支持 TXT、DOCX 或 SRT")
    text = _docx_text(payload) if suffix == ".docx" else _decode_text(payload)
    if suffix == ".srt":
        text = _srt_text(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not _lexical_spans(text):
        raise ManuscriptMatchError("参考文稿没有可匹配文字")
    return text


def reference_tokens(
    text: str, source_language: str | None = None
) -> list[ReferenceToken]:
    matches = _lexical_spans(text, source_language)
    result: list[ReferenceToken] = []
    for position, match in enumerate(matches):
        start = match.start if position else 0
        end = matches[position + 1].start if position + 1 < len(matches) else len(text)
        rendered = text[start:end].strip()
        lexical = match.lexical
        if position == 0 and match.start > 0:
            rendered = text[:end].strip()
        result.append(
            ReferenceToken(
                normalized=_normalized_lexical(lexical),
                text=rendered or lexical,
                lexical=lexical,
            )
        )
    return result


def _unit_tokens(
    units: Iterable[dict[str, Any]], source_language: str | None = None
) -> tuple[list[str], list[int]]:
    values: list[str] = []
    owners: list[int] = []
    for position, unit in enumerate(units):
        for match in _lexical_spans(str(unit.get("text", "")), source_language):
            values.append(_normalized_lexical(match.lexical))
            owners.append(position)
    return values, owners


def _reference_to_source_positions(
    source_values: list[str], reference: list[ReferenceToken]
) -> tuple[list[int], list[dict[str, Any]], float]:
    right = [item.normalized for item in reference]
    matcher = difflib.SequenceMatcher(None, source_values, right, autojunk=False)
    mapping = [-1] * len(reference)
    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(j2 - j1):
                mapping[j1 + offset] = i1 + offset
            continue
        changes.append(
            {
                "kind": tag,
                "source_token_range": [i1, i2 - 1],
                "reference_token_range": [j1, j2 - 1],
                "source_text": " ".join(source_values[i1:i2]),
                "reference_text": " ".join(item.text for item in reference[j1:j2]),
            }
        )
        source_count = i2 - i1
        target_count = j2 - j1
        for offset in range(target_count):
            if source_count:
                mapped = i1 + min(source_count - 1, int(offset * source_count / max(1, target_count)))
            elif i1:
                mapped = i1 - 1
            else:
                mapped = min(i1, len(source_values) - 1)
            mapping[j1 + offset] = mapped
    return mapping, changes, matcher.ratio()


def materialize_reference_alignment(
    reference_text: str,
    alignment: dict[str, Any],
    source_language: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    source_units = [dict(item) for item in alignment.get("units", [])]
    if not source_units:
        raise ManuscriptMatchError("ASR 对齐为空，无法匹配参考文稿")
    source_values, token_owners = _unit_tokens(source_units, source_language)
    reference = reference_tokens(reference_text, source_language)
    if not source_values or not reference:
        raise ManuscriptMatchError("ASR 或参考文稿没有可匹配词元")
    mapping, changes, similarity = _reference_to_source_positions(source_values, reference)
    reference_only_indexes: set[int] = set()
    for change in changes:
        reference_range = change.get("reference_token_range", [])
        source_range = change.get("source_token_range", [])
        if len(reference_range) != 2 or len(source_range) != 2:
            continue
        reference_start, reference_end = map(int, reference_range)
        source_start, source_end = map(int, source_range)
        source_count = max(0, source_end - source_start + 1)
        kind = str(change.get("kind") or "")
        if kind == "insert":
            reference_only_indexes.update(range(reference_start, reference_end + 1))
        elif kind == "replace":
            reference_only_indexes.update(
                range(reference_start + source_count, reference_end + 1)
            )
    mapped_owners = [
        token_owners[max(0, min(len(token_owners) - 1, item))]
        for item in mapping
    ]
    owner_members: dict[int, list[int]] = {}
    for ref_index, owner in enumerate(mapped_owners):
        owner_members.setdefault(owner, []).append(ref_index)
    canonical_units: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    for ref_index, token in enumerate(reference):
        owner = mapped_owners[ref_index]
        source = source_units[owner]
        siblings = owner_members[owner]
        slot = siblings.index(ref_index)
        count = len(siblings)
        start = float(source.get("start", 0.0))
        end = max(start, float(source.get("end", start)))
        width = (end - start) / max(1, count)
        unit = {
            **source,
            "index": ref_index,
            "text": token.text,
            "start": start + slot * width,
            "end": end if slot + 1 == count else start + (slot + 1) * width,
            "sentence_start": ref_index == 0
            or bool(re.search(r"[.!?。！？][\"'”’）》〉」』]*$", reference[ref_index - 1].text)),
            "sentence_end": bool(re.search(r"[.!?。！？][\"'”’）》〉」』]*$", token.text)),
            "timing_source": (
                "reference_exact"
                if source_values[mapping[ref_index]] == token.normalized
                else "reference_envelope_inherited"
            ),
            "reference_changed": source_values[mapping[ref_index]] != token.normalized,
            "reference_only": ref_index in reference_only_indexes,
        }
        canonical_units.append(unit)
        provenance.append(
            {
                "reference_index": ref_index,
                "source_alignment_index": int(source.get("index", owner)),
                "source_text": str(source.get("text", "")),
                "reference_text": token.text,
                "changed": bool(unit["reference_changed"]),
                "reference_only": bool(unit["reference_only"]),
            }
        )
    canonical = dict(alignment)
    canonical["units"] = canonical_units
    canonical["master_text"] = reference_text.strip()
    canonical["reference_manuscript"] = {
        "applied": True,
        "similarity": round(similarity, 6),
    }
    report = {
        "schema_version": "substar.reference-manuscript.v1",
        "similarity": round(similarity, 6),
        "source_unit_count": len(source_units),
        "reference_unit_count": len(canonical_units),
        "changes": changes,
        "provenance": provenance,
        "tokenization": _tokenization_diagnostics(reference_text, source_language),
    }
    return reference_text.strip(), canonical, report


def normalize_break_symbols(value: str) -> str:
    symbols = "".join(dict.fromkeys(str(value)))
    symbols = "".join(symbol for symbol in symbols if not symbol.isspace())
    if not symbols:
        raise ManuscriptMatchError("参考稿模式至少需要一个切分符号")
    if len(symbols) > 32:
        raise ManuscriptMatchError("参考稿切分符号不能超过 32 个字符")
    return symbols


def _trailing_punctuation(text: str, lexical: str) -> str:
    """Return punctuation attached after lexical content, excluding its internals."""

    rendered = str(text or "").rstrip()
    needle = str(lexical or "")
    position = rendered.rfind(needle)
    if position < 0:
        return ""
    return rendered[position + len(needle) :]


def _reference_token_has_break(token: ReferenceToken, symbols: str) -> bool:
    return any(
        symbol in _trailing_punctuation(token.text, token.lexical)
        for symbol in symbols
    )


def _source_text_has_break(text: str, symbols: str, language: str | None) -> bool:
    spans = _lexical_spans(str(text or ""), language)
    if not spans:
        return False
    final = spans[-1]
    return any(symbol in str(text)[final.end :] for symbol in symbols)


def _timed_source_tokens(
    units: Iterable[dict[str, Any]], source_language: str | None = None
) -> list[dict[str, Any]]:
    """Expand provider word fragments to lexical alignment tokens.

    Qwen commonly returns one timed record per Han character, but the provider
    contract also permits a multi-character record. Split that record only in
    the derived alignment projection and divide its time envelope
    deterministically; immutable recognition evidence remains untouched.
    """

    result: list[dict[str, Any]] = []
    for unit_position, unit in enumerate(units):
        raw_text = str(unit.get("text", ""))
        matches = _lexical_spans(raw_text, source_language)
        if not matches:
            continue
        start = float(unit.get("start", 0.0))
        end = max(start, float(unit.get("end", start)))
        width = (end - start) / len(matches)
        for offset, match in enumerate(matches):
            token_start = start + offset * width
            token_end = end if offset + 1 == len(matches) else start + (offset + 1) * width
            rendered_end = (
                matches[offset + 1].start
                if offset + 1 < len(matches)
                else len(raw_text)
            )
            rendered = raw_text[match.start:rendered_end].strip() or match.lexical
            result.append(
                {
                    "normalized": _normalized_lexical(match.lexical),
                    "text": rendered,
                    "lexical": match.lexical,
                    "start": token_start,
                    "end": token_end,
                    "speaker_id": unit.get("speaker_id"),
                    "source_alignment_index": int(unit.get("index", unit_position)),
                }
            )
    return result


def materialize_reference_script(
    reference_text: str,
    recognition_units: Iterable[dict[str, Any]],
    break_symbols: str,
    source_language: str | None = None,
) -> tuple[dict[str, Any], list[int], dict[str, Any]]:
    """Build a reference-primary display projection over the ASR timeline.

    Recognition evidence owns timing, speaker identity and source-only spoken
    tokens.  For every aligned token, the reference owns spelling, casing and
    punctuation.  ASR-only tokens remain visible and auditable instead of being
    treated as deletions from the manuscript.
    """

    symbols = normalize_break_symbols(break_symbols)
    reference = reference_tokens(reference_text, source_language)
    source_units = [dict(item) for item in recognition_units]
    source = _timed_source_tokens(source_units, source_language)
    if not source or not reference:
        raise ManuscriptMatchError("听写或参考稿没有可对齐词元")
    source_values = [str(item["normalized"]) for item in source]
    right = [item.normalized for item in reference]
    matcher = difflib.SequenceMatcher(None, source_values, right, autojunk=False)
    raw_opcodes = matcher.get_opcodes()
    similarity = matcher.ratio()

    # Learn only repeated, conflict-free corrections from the strongest local
    # evidence: one source token aligned to one reference token.  This lets a
    # manuscript consistently correct a recurring name even when one later
    # occurrence sits inside an unequal phrase rewrite, without treating a
    # single ambiguous rewrite as permission to overwrite ASR text.
    consensus_candidates: dict[str, Counter[str]] = defaultdict(Counter)
    for tag, i1, i2, j1, j2 in raw_opcodes:
        if tag != "replace" or i2 - i1 != 1 or j2 - j1 != 1:
            continue
        source_value = source_values[i1]
        reference_value = right[j1]
        if source_value != reference_value:
            consensus_candidates[source_value][reference_value] += 1
    lexical_consensus: dict[str, tuple[str, int]] = {}
    for source_value, candidates in consensus_candidates.items():
        if len(candidates) != 1:
            continue
        reference_value, evidence_count = candidates.most_common(1)[0]
        if evidence_count >= 3:
            lexical_consensus[source_value] = (reference_value, evidence_count)

    changes: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    insertions: list[dict[str, Any]] = []
    retained_source: list[dict[str, Any]] = []
    reference_to_source: dict[int, int] = {}
    direct = 0

    def replacement(
        source_index: int,
        reference_index: int,
        *,
        apply_lexical_change: bool,
        consensus_evidence_count: int | None = None,
    ) -> None:
        nonlocal direct
        owner = source[source_index]
        token = reference[reference_index]
        exact = str(owner["normalized"]) == token.normalized
        direct += int(exact)
        reference_to_source[reference_index] = source_index
        before = str(owner["text"])
        after = token.text
        # Once lexical content is aligned, the reference owns the complete
        # rendering.  This deliberately records punctuation removal and casing
        # changes as reversible reference replacements.
        should_record = before != after and (exact or apply_lexical_change)
        if should_record:
            item = {
                "source_index": source_index,
                "reference_index": reference_index,
                "before": before,
                "after": after,
                "lexical_match": exact,
                "status": "applied" if (exact or apply_lexical_change) else "suggested",
            }
            if consensus_evidence_count is not None and not exact:
                item["decision"] = "document_consensus"
                item["evidence_count"] = consensus_evidence_count
            replacements.append(item)
        elif before != after and not exact:
            replacements.append(
                {
                    "source_index": source_index,
                    "reference_index": reference_index,
                    "before": before,
                    "after": after,
                    "lexical_match": False,
                    "status": "suggested",
                }
            )

    for tag, i1, i2, j1, j2 in raw_opcodes:
        if tag == "equal":
            for offset in range(j2 - j1):
                replacement(
                    i1 + offset,
                    j1 + offset,
                    apply_lexical_change=True,
                )
            continue
        changes.append(
            {
                "kind": tag,
                "source_token_range": [i1, i2 - 1],
                "reference_token_range": [j1, j2 - 1],
                "source_text": " ".join(source_values[i1:i2]),
                "reference_text": " ".join(item.text for item in reference[j1:j2]),
            }
        )
        if tag == "delete":
            for source_index in range(i1, i2):
                retained_source.append(
                    {
                        "source_index": source_index,
                        "before": str(source[source_index]["text"]),
                        "reason": "reference_omitted",
                    }
                )
            continue
        if tag == "insert":
            anchor = i1 - 1
            for reference_index in range(j1, j2):
                reference_to_source[reference_index] = max(0, min(len(source) - 1, anchor))
                insertions.append(
                    {
                        "after_source_index": anchor,
                        "reference_index": reference_index,
                        "text": reference[reference_index].text,
                    }
                )
            continue
        source_count = i2 - i1
        reference_count = j2 - j1
        paired = min(source_count, reference_count)
        # Equal-length spans have an unambiguous token projection. A reliable
        # manuscript therefore corrects every projected token; adjacent name
        # fixes often form one opcode despite mapping one-to-one.
        apply_lexical_change = source_count == reference_count
        for offset in range(paired):
            source_index = i1 + offset
            reference_index = j1 + offset
            consensus = lexical_consensus.get(source_values[source_index])
            consensus_evidence_count = (
                consensus[1]
                if consensus is not None and consensus[0] == right[reference_index]
                else None
            )
            replacement(
                source_index,
                reference_index,
                apply_lexical_change=(
                    apply_lexical_change or consensus_evidence_count is not None
                ),
                consensus_evidence_count=(
                    consensus_evidence_count
                    if not apply_lexical_change
                    else None
                ),
            )
        if source_count == 1 and reference_count > 1:
            # Providers keep numeric/alphanumeric runs such as "20" in one
            # timed token while a CJK manuscript may tokenize the correction as
            # "二" + "十". This remains one unambiguous source owner, so use
            # the complete manuscript span instead of falling back to ASR.
            source_index = i1
            reference_indexes = list(range(j1, j2))
            for reference_index in reference_indexes:
                reference_to_source[reference_index] = source_index
            replacements = [
                item
                for item in replacements
                if not (
                    int(item["source_index"]) == source_index
                    and int(item["reference_index"]) == j1
                )
            ]
            replacements.append(
                {
                    "source_index": source_index,
                    "reference_index": j1,
                    "reference_indexes": reference_indexes,
                    "before": str(source[source_index]["text"]),
                    "after": layout_tokens(
                        (reference[index].text for index in reference_indexes),
                        source_language,
                    ),
                    "lexical_match": False,
                    "status": "applied",
                }
            )
            continue
        for source_index in range(i1 + paired, i2):
            retained_source.append(
                {
                    "source_index": source_index,
                    "before": str(source[source_index]["text"]),
                    "reason": "reference_shorter_replacement",
                }
            )
        anchor = i1 + paired - 1 if paired else i1 - 1
        for reference_index in range(j1 + paired, j2):
            reference_to_source[reference_index] = max(0, min(len(source) - 1, anchor))
            # Extra words inside a phrase rewrite are not reliable evidence of
            # an ASR omission.  Keep the spoken track intact; the mismatch is
            # still present in `changes` for review.

    units = [
        {
            "index": index,
            "start": float(item["start"]),
            "end": float(item["end"]),
            "text": str(item["text"]),
            "speaker_id": item.get("speaker_id"),
        }
        for index, item in enumerate(source)
    ]
    reference_boundaries = [
        index
        for index, token in enumerate(reference[:-1])
        if _reference_token_has_break(token, symbols)
    ]
    asr_breaks = {
        index
        for index, token in enumerate(source[:-1])
        if _source_text_has_break(str(token["text"]), symbols, source_language)
    }
    resolved_reference_breaks: dict[int, int] = {}
    boundary_reconciliations: list[dict[str, int]] = []
    for reference_index in reference_boundaries:
        mapped = reference_to_source.get(reference_index)
        if mapped is None or mapped >= len(source) - 1:
            continue
        next_mapped = next(
            (
                reference_to_source[index]
                for index in range(reference_index + 1, len(reference))
                if reference_to_source.get(index, mapped) > mapped
            ),
            len(source),
        )
        # If ASR contains an oral particle or other retained token before the
        # next aligned reference word, keep the actual spoken sentence ending.
        nearby_asr = sorted(
            boundary for boundary in asr_breaks
            if mapped <= boundary < next_mapped
        )
        resolved = nearby_asr[0] if nearby_asr else mapped
        resolved_reference_breaks[reference_index] = resolved
        if resolved != mapped:
            boundary_reconciliations.append(
                {
                    "reference_index": reference_index,
                    "mapped_source_index": mapped,
                    "resolved_source_index": resolved,
                }
            )

    # Do not render a duplicate reference mark on the earlier aligned word
    # when the boundary was reconciled to a later ASR sentence ending.
    replacements = [
        item
        for item in replacements
        if not (
            int(item["reference_index"]) in resolved_reference_breaks
            and resolved_reference_breaks[int(item["reference_index"])]
            != int(item["source_index"])
            and bool(item.get("lexical_match"))
        )
    ]
    reference_breaks = set(resolved_reference_breaks.values())
    # Reference-script mode retains ASR-only text and punctuation as visible
    # content, but its Cue topology is owned exclusively by the reference.
    retained_asr_breaks: set[int] = set()
    breaks = sorted(reference_breaks)

    reference_boundary_set = set(reference_boundaries)
    for insertion in insertions:
        reference_index = int(insertion["reference_index"])
        insertion["placement"] = (
            "right"
            if reference_index - 1 in reference_boundary_set
            else "left"
        )
    matched_ratio = direct / len(reference)
    segment_ranges: list[tuple[int, int]] = []
    left = 0
    for boundary in reference_boundaries:
        segment_ranges.append((left, boundary))
        left = boundary + 1
    segment_ranges.append((left, len(reference) - 1))
    covered_segments = sum(
        1
        for left, right in segment_ranges
        if any(index in reference_to_source for index in range(left, right + 1))
    )
    segment_coverage = covered_segments / len(segment_ranges)
    reference_diagnostics = _tokenization_diagnostics(reference_text, source_language)
    source_diagnostics = _tokenization_diagnostics(
        "".join(str(item.get("text", "")) for item in source_units),
        source_language,
    )
    coverage = min(
        float(reference_diagnostics["linguistic_character_coverage"]),
        float(source_diagnostics["linguistic_character_coverage"]),
    )
    script_mismatch = bool(reference_diagnostics["script_mismatch"])
    if (
        matched_ratio >= 0.85
        and segment_coverage >= 0.95
        and coverage >= 0.95
        and not script_mismatch
    ):
        quality = "good"
    elif (
        matched_ratio >= 0.40
        and segment_coverage >= 0.50
        and coverage >= 0.80
        and not script_mismatch
    ):
        quality = "warning"
    else:
        quality = "failed"
    if quality == "failed":
        for item in replacements:
            if item.get("status") == "applied":
                item["status"] = "suggested"
    elif quality == "warning":
        for item in replacements:
            if not item.get("lexical_match") and item.get("status") == "applied":
                item["status"] = "suggested"
    material = {
        "schema_version": "substar.segmentation-material.v1",
        "source_transcript": layout_tokens(
            (str(item["text"]) for item in units), source_language
        ),
        "units": units,
    }
    report = {
        "schema_version": "substar.reference-script-alignment.v1",
        "quality": quality,
        "break_symbols": symbols,
        "similarity": round(similarity, 6),
        "matched_token_ratio": round(matched_ratio, 6),
        "segment_coverage": round(segment_coverage, 6),
        "source_token_count": len(source),
        "reference_token_count": len(reference),
        "segment_count": len(breaks) + 1,
        "asr_breaks": sorted(asr_breaks),
        "retained_asr_breaks": sorted(retained_asr_breaks),
        "reference_breaks": sorted(reference_breaks),
        "boundary_reconciliations": boundary_reconciliations,
        "changes": changes,
        "replacements": replacements,
        "insertions": insertions,
        "retained_source": retained_source,
        "lexical_consensus": [
            {
                "source": source_value,
                "reference": reference_value,
                "evidence_count": evidence_count,
            }
            for source_value, (reference_value, evidence_count) in sorted(
                lexical_consensus.items()
            )
        ],
        "tokenization": {
            "reference": reference_diagnostics,
            "source": source_diagnostics,
        },
        "provenance": [
            {
                "reference_index": index,
                "source_index": reference_to_source.get(index),
                "reference_text": token.text,
                "matched": index in reference_to_source
                and source_values[reference_to_source[index]] == token.normalized,
            }
            for index, token in enumerate(reference)
        ],
    }
    return material, breaks, report


def editor_reference_operations(
    reference_text: str,
    units: list[dict[str, Any]],
    source_language: str | None = None,
) -> dict[str, Any]:
    """Return reference-primary corrections without changing cue boundaries."""

    source_values, token_owners = _unit_tokens(units, source_language)
    reference = reference_tokens(reference_text, source_language)
    if not source_values or not reference:
        raise ManuscriptMatchError("当前编辑稿或参考文稿没有可匹配词元")
    mapping, raw_changes, similarity = _reference_to_source_positions(source_values, reference)
    reference_diagnostics = _tokenization_diagnostics(reference_text, source_language)
    source_diagnostics = _tokenization_diagnostics(
        "".join(str(item.get("text", "")) for item in units), source_language
    )
    coverage = min(
        float(reference_diagnostics["linguistic_character_coverage"]),
        float(source_diagnostics["linguistic_character_coverage"]),
    )
    if (
        similarity >= 0.85
        and coverage >= 0.95
        and not reference_diagnostics["script_mismatch"]
    ):
        quality = "good"
    elif (
        similarity >= 0.40
        and coverage >= 0.80
        and not reference_diagnostics["script_mismatch"]
    ):
        quality = "warning"
    else:
        quality = "failed"
    reference_by_owner: dict[int, list[int]] = {}
    for ref_index, source_token in enumerate(mapping):
        source_token = max(0, min(len(token_owners) - 1, source_token))
        reference_by_owner.setdefault(token_owners[source_token], []).append(ref_index)
    edits: list[dict[str, Any]] = []
    merges: list[dict[str, Any]] = []
    insertions: list[dict[str, Any]] = []
    hidden: list[int] = []
    changes: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for owner, ref_indexes in sorted(reference_by_owner.items()):
        source_index = int(units[owner]["index"])
        text = " ".join(reference[index].text for index in ref_indexes).strip()
        original = str(units[owner].get("text", ""))
        if text == original:
            continue
        if quality == "good":
            edits.append({"index": source_index, "text": text})
        consumed.add(owner)
        changes.append(
            {
                "id": f"ref-{source_index}",
                "kind": "reference",
                "type": "replace",
                "source_indices": [source_index],
                "original": original,
                "text": text,
                "status": "applied" if quality == "good" else "suggested",
            }
        )
    # A reference manuscript is corrective evidence, never a deletion request.
    # Source units absent from its alignment remain active and are recorded so
    # the editor can explain why ASR text was kept.
    mapped_owners = set(reference_by_owner)
    for owner, unit in enumerate(units):
        if owner in mapped_owners:
            continue
        source_index = int(unit["index"])
        changes.append(
            {
                "id": f"ref-retained-{source_index}",
                "kind": "reference",
                "type": "retained_source",
                "source_indices": [source_index],
                "original": str(unit.get("text", "")),
                "text": "",
                "status": "retained",
            }
        )
    return {
        "schema_version": "substar.reference-editor.v1",
        "quality": quality,
        "similarity": round(similarity, 6),
        "edits": edits,
        "merges": merges,
        "insertions": insertions,
        "hidden": hidden,
        "reference_changes": changes,
        "diagnostics": raw_changes,
        "tokenization": {
            "reference": reference_diagnostics,
            "source": source_diagnostics,
        },
    }
