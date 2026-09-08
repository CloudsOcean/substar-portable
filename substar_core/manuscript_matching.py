from __future__ import annotations

import difflib
import html
import hashlib
import io
import json
import math
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
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
REFERENCE_MATCHER_VERSION = "local-reference-v4"
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
    if not any(unicodedata.category(char).startswith("L") for char in value):
        script_mismatch = False  # Digits are shared by every supported script.
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


def materialize_reference_alignment(
    reference_text: str, alignment: dict[str, Any], source_language: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Project the shared reference decisions into the semantic word ledger."""
    material, _, report = materialize_reference_script(
        reference_text, alignment.get("units", []),
        reference_break_symbols_for_language(source_language), source_language,
    )
    source = material["units"]
    edits = {int(item["source_index"]): item for item in report["replacements"]
             if item["status"] == "applied"}
    edits.update({int(item["source_index"]): item for item in report["merges"]})
    skipped = {index for item in report["merges"] for index in item["source_indexes"][1:]}
    retained = {item["source_index"]: item for item in report["retained_source"]}
    insertions: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in report["insertions"]:
        insertions[item["after_source_index"]].append(item)
    canonical_units = []
    provenance = []
    changes = []
    evidence_hash = hashlib.sha256(json.dumps(alignment, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def append(text: str, owners: list[int], *, hidden: bool = False, changed: bool = False) -> None:
        first, last = source[owners[0]], source[owners[-1]]
        tokens = reference_tokens(text, source_language)
        start, end = float(first["start"]), float(last["end"])
        original = layout_tokens((source[index]["text"] for index in owners), source_language)
        replacement_group = len(canonical_units) if changed else None
        for offset, token in enumerate(tokens):
            index = len(canonical_units)
            width = (end - start) / len(tokens)
            canonical_units.append({**first, "index": index, "text": token.text,
                "start": start + offset * width, "end": end if offset + 1 == len(tokens) else start + (offset + 1) * width,
                "reference_only": hidden, "reference_changed": changed,
                "timing_source": "reference_envelope_inherited" if changed or hidden else "reference_exact",
                "timing": {"kind": "subdivided" if len(tokens) > 1 else "envelope-inherited",
                           "evidence_sha256": first["timing"].get("evidence_sha256", evidence_hash),
                           "native_token_ids": list(dict.fromkeys(
                               token_id for owner in owners
                               for token_id in source[owner]["timing"]["native_token_ids"])),
                           "native_start": first["timing"]["native_start"],
                           "native_end": last["timing"]["native_end"]}})
            provenance.append({"reference_index": index, "source_alignment_index": owners[0],
                               "source_text": original, "reference_text": token.text,
                               "changed": changed or hidden, "reference_only": hidden,
                               "replacement_group": replacement_group,
                               "retained_source": not hidden and owners[0] in retained})
            if changed or hidden:
                changes.append({"kind": "insert" if hidden else "replace",
                                "source_token_range": [owners[0], owners[-1]],
                                "reference_token_range": [index, index]})

    for item in insertions[-1]:
        append(item["text"], [0], hidden=True)
    for index, unit in enumerate(source):
        if index not in skipped:
            edit = edits.get(index)
            append(edit["after"] if edit else unit["text"],
                   edit.get("source_indexes", [index]) if edit else [index], changed=bool(edit))
        for item in insertions[index]:
            append(item["text"], [index], hidden=True)
    canonical = {**alignment, "units": canonical_units, "master_text": reference_text.strip(),
                 "reference_manuscript": {"authority": "reference_assisted", "applied": True,
                                          "similarity": report["similarity"], "requires_review": report["requires_review"]}}
    audit = {**report, "schema_version": "substar.reference-manuscript.v1",
             "source_unit_count": len(source), "reference_unit_count": len(canonical_units),
             "changes": changes, "alignment_changes": report["changes"], "provenance": provenance}
    return reference_text.strip(), canonical, audit


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
            rendered = raw_text[0 if offset == 0 else match.start:rendered_end].strip() or match.lexical
            result.append(
                {
                    "normalized": _normalized_lexical(match.lexical),
                    "text": rendered,
                    "lexical": match.lexical,
                    "start": token_start,
                    "end": token_end,
                    "speaker_id": unit.get("speaker_id"),
                    "source_alignment_index": int(unit.get("index", unit_position)),
                    "timing": {
                        **dict(unit.get("timing") or {}),
                        "kind": "subdivided" if len(matches) > 1 else (unit.get("timing") or {}).get("kind", "envelope-inherited"),
                        "native_token_ids": (unit.get("timing") or {}).get("native_token_ids", [str(unit.get("index", unit_position))]),
                        "native_start": (unit.get("timing") or {}).get("native_start", start),
                        "native_end": (unit.get("timing") or {}).get("native_end", end),
                    },
                }
            )
    return result


def _reference_frequency(values: list[str]) -> Counter[tuple[str, ...]]:
    """Count written words and short phrases, never individual Han characters."""
    counts: Counter[tuple[str, ...]] = Counter()
    for start, value in enumerate(values):
        if len(value) > 1 or not _character_script(value[0]):
            counts[(value,)] += 1
        for width in range(2, min(6, len(values) - start) + 1):
            counts[tuple(values[start:start + width])] += 1
    return counts


def _phrase_reference_opcodes(opcodes, reference):
    """Keep a short moved phrase together between stable outside anchors."""
    result = []
    position = 0
    while position < len(opcodes):
        if 0 < position and position + 3 < len(opcodes):
            left = opcodes[position - 1]
            first, middle, last, right = opcodes[position:position + 4]
            if (left[0] == right[0] == middle[0] == "equal"
                    and {first[0], last[0]} == {"insert", "delete"}
                    and left[2] - left[1] >= 2 and right[2] - right[1] >= 2
                    and middle[2] - middle[1] <= 3
                    and max(last[2] - first[1], last[4] - first[3]) <= 12
                    and not any(re.search(r"[。！？.!?]", token.text)
                                for token in reference[first[3]:last[4]])):
                result.append(("replace", first[1], last[2], first[3], last[4]))
                position += 3
                continue
        result.append(opcodes[position])
        position += 1
    return result


def _local_reference_score(
    opcodes: list[tuple[str, int, int, int, int]], position: int,
    source: list[str], reference: list[str], frequencies: Counter[tuple[str, ...]],
) -> dict[str, Any]:
    _, i1, i2, j1, j2 = opcodes[position]
    left = opcodes[position - 1] if position else None
    right = opcodes[position + 1] if position + 1 < len(opcodes) else None
    left_size = left[2] - left[1] if left and left[0] == "equal" else 0
    right_size = right[2] - right[1] if right and right[0] == "equal" else 0
    # A word counts as a context anchor in alphabetic scripts; CJK needs two
    # characters so a common particle cannot authorize an arbitrary rewrite.
    def anchor(size: int, value: str) -> bool:
        return size >= 2 or (size == 1 and len(value) >= 2)
    left_anchor = bool(left_size and anchor(left_size, reference[j1 - 1]))
    right_anchor = bool(right_size and anchor(right_size, reference[j2]))
    frequency = frequencies[tuple(reference[j1:j2])]
    # A changed Han character can belong to a recurring name (杨 + 熊).
    for a, b in ((max(0, j1 - 1), j2), (j1, min(len(reference), j2 + 1))):
        if 2 <= b - a <= 6:
            frequency = max(frequency, frequencies[tuple(reference[a:b])])
    boost = min(0.16, 0.08 * math.log2(max(1, frequency)))
    lexical = difflib.SequenceMatcher(
        None, "".join(source[i1:i2]), "".join(reference[j1:j2]), autojunk=False
    ).ratio()
    compact = max(i2 - i1, j2 - j1) <= 12
    balanced = min(i2 - i1, j2 - j1) / max(i2 - i1, j2 - j1) >= 0.5
    base = 0.74 if left_anchor and right_anchor and compact and balanced else (
        0.58 if (left_anchor or right_anchor) and compact else 0.0
    )
    # A shared document edge supplies the second boundary for a short tail.
    edge_aligned = ((i2 == len(source) and j2 == len(reference) and left_size >= 4)
                    or (i1 == j1 == 0 and right_size >= 4))
    if edge_aligned and balanced and max(i2 - i1, j2 - j1) <= 4:
        base = max(base, 0.72)
    source_number = _written_number("".join(source[i1:i2]))
    reference_number = _written_number("".join(reference[j1:j2]))
    numeric = source_number is not None and source_number == reference_number
    score = 1.0 if numeric else min(1.0, base + lexical * 0.25 + boost)
    return {
        "score": round(score, 6), "reference_frequency": frequency,
        "source_token_range": [i1, i2 - 1], "reference_token_range": [j1, j2 - 1],
        "frequency_boost": round(boost, 6),
        "left_anchor": left_anchor, "right_anchor": right_anchor,
        "edge_aligned": edge_aligned,
        "accepted": numeric or (base > 0 and score >= 0.68),
    }


def _written_number(text: str) -> Decimal | None:
    text = unicodedata.normalize("NFKC", text).replace(",", "")
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return Decimal(text)
    digits = {char: value for value, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2, "兩": 2})
    scales = {"十": 10, "百": 100, "千": 1000, "万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000}
    if not text or any(char not in digits and char not in scales for char in text):
        return None
    if all(char in digits for char in text):
        return Decimal("".join(str(digits[char]) for char in text))
    total = section = number = 0
    for char in text:
        if char in digits:
            number = digits[char]
        elif scales[char] < 10000:
            section += (number or 1) * scales[char]
            number = 0
        elif scales[char] == 10000:
            total += (section + number) * 10000
            section = number = 0
        else:
            total = (total + section + number) * 100000000
            section = number = 0
    return Decimal(total + section + number)


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
    raw_opcodes = _phrase_reference_opcodes(matcher.get_opcodes(), reference)
    similarity = matcher.ratio()

    frequencies = _reference_frequency(right)
    local_scores = {
        pos: _local_reference_score(raw_opcodes, pos, source_values, right, frequencies)
        for pos, opcode in enumerate(raw_opcodes) if opcode[0] == "replace"
    }
    script_mismatch = _tokenization_diagnostics(reference_text, source_language)["script_mismatch"]
    consensus_candidates: dict[str, Counter[str]] = defaultdict(Counter)
    # Include exact uses as counterevidence to an otherwise repeated correction.
    for pos, (tag, i1, i2, j1, j2) in enumerate(raw_opcodes):
        if tag == "equal":
            for offset in range(i2 - i1):
                consensus_candidates[source_values[i1 + offset]][right[j1 + offset]] += 1
        elif i2 - i1 == j2 - j1 == 1 and local_scores[pos]["accepted"]:
            consensus_candidates[source_values[i1]][right[j1]] += 1
    lexical_consensus = {}
    for value, candidates in consensus_candidates.items():
        if len(candidates) == 1:
            target, count = candidates.most_common(1)[0]
            if value != target and count >= 3:
                lexical_consensus[value] = (target, count)

    changes: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    merges: list[dict[str, Any]] = []
    insertions: list[dict[str, Any]] = []
    retained_source: list[dict[str, Any]] = []
    reference_to_source: dict[int, int] = {}
    trusted_reference: set[int] = set()
    direct = 0

    def retain(start: int, end: int, reason: str) -> None:
        for index in range(start, end):
            retained_source.append({"source_index": index, "before": source[index]["text"], "reason": reason})

    def insert(start: int, end: int, anchor: int, reason: str) -> None:
        for index in range(start, end):
            insertions.append({"after_source_index": anchor, "reference_index": index,
                               "text": reference[index].text, "reason": reason})

    def replacement(i: int, j: int, *, evidence: dict[str, Any] | None = None) -> None:
        nonlocal direct
        exact = source_values[i] == right[j]
        direct += int(exact)
        reference_to_source[j] = i
        if not script_mismatch:
            trusted_reference.add(j)
        if source[i]["text"] != reference[j].text:
            replacements.append({"source_index": i, "reference_index": j,
                                 "before": source[i]["text"], "after": reference[j].text,
                                 "lexical_match": exact,
                                 "status": "suggested" if script_mismatch else "applied",
                                 **(evidence or {})})

    for pos, (tag, i1, i2, j1, j2) in enumerate(raw_opcodes):
        if tag == "equal":
            for offset in range(i2 - i1):
                replacement(i1 + offset, j1 + offset)
            continue
        changes.append({"kind": tag, "source_token_range": [i1, i2 - 1],
                        "reference_token_range": [j1, j2 - 1],
                        "source_text": layout_tokens(source_values[i1:i2], source_language),
                        "reference_text": layout_tokens((t.text for t in reference[j1:j2]), source_language)})
        if tag == "delete":
            retain(i1, i2, "reference_omitted")
            continue
        if tag == "insert":
            insert(j1, j2, i1 - 1, "reference_only")
            continue
        # Resolve a known term before considering an unequal phrase as a whole.
        # E.g. "kilometers" -> "kilometres further" corrects the spelling and
        # keeps "further" as a hidden addition, rather than swallowing both.
        if i2 - i1 != j2 - j1 and not script_mismatch:
            while i1 < i2 and j1 < j2:
                consensus = lexical_consensus.get(source_values[i1])
                spelling = (len(source_values[i1]) > 1 and len(right[j1]) > 1
                            and difflib.SequenceMatcher(None, source_values[i1], right[j1], autojunk=False).ratio() >= 0.72
                            and (local_scores[pos]["left_anchor"] or local_scores[pos]["right_anchor"]))
                if consensus is not None and consensus[0] == right[j1]:
                    replacement(i1, j1, evidence={"decision": "document_consensus", "evidence_count": consensus[1]})
                elif spelling:
                    replacement(i1, j1, evidence={"decision": "local_spelling"})
                else:
                    break
                i1 += 1
                j1 += 1
            if i1 == i2:
                insert(j1, j2, i1 - 1, "reference_only")
                continue
            if j1 == j2:
                retain(i1, i2, "reference_omitted")
                continue
            remaining_opcodes = list(raw_opcodes)
            remaining_opcodes[pos] = (tag, i1, i2, j1, j2)
            local_scores[pos] = _local_reference_score(remaining_opcodes, pos, source_values, right, frequencies)
        score = local_scores[pos]
        evidence = {"decision": "local_alignment", **score}
        if score["accepted"] and not script_mismatch:
            if i2 - i1 == j2 - j1:
                for offset in range(i2 - i1):
                    replacement(i1 + offset, j1 + offset, evidence=evidence)
            else:
                item = {"source_index": i1, "source_indexes": list(range(i1, i2)),
                        "reference_index": j1, "reference_indexes": list(range(j1, j2)),
                        "before": layout_tokens((t["text"] for t in source[i1:i2]), source_language),
                        "after": layout_tokens((t.text for t in reference[j1:j2]), source_language),
                        "lexical_match": False, "status": "applied", **evidence}
                (replacements if i2 - i1 == 1 else merges).append(item)
                for j in range(j1, j2):
                    reference_to_source[j] = i2 - 1
                    trusted_reference.add(j)
            continue
        # Repeated, conflict-free corrections may resolve the first word of an
        # unequal rewrite; the remaining source and reference stay inspectable.
        offset = 0
        while i1 + offset < i2 and j1 + offset < j2 and not script_mismatch:
            consensus = lexical_consensus.get(source_values[i1 + offset])
            if consensus is None or consensus[0] != right[j1 + offset]:
                break
            replacement(i1 + offset, j1 + offset, evidence={
                "decision": "document_consensus", "evidence_count": consensus[1], **score})
            offset += 1
        retain(i1 + offset, i2, "ambiguous_reference")
        insert(j1 + offset, j2, i1 + offset - 1, "ambiguous_reference")

    units = [
        {
            "index": index,
            "start": float(item["start"]),
            "end": float(item["end"]),
            "text": str(item["text"]),
            "speaker_id": item.get("speaker_id"),
            "timing": dict(item["timing"]),
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
        if reference_index not in trusted_reference or mapped is None or mapped >= len(source) - 1:
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
        if any(index in trusted_reference for index in range(left, right + 1))
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
    aligned_ratio = len(trusted_reference) / len(reference)
    if (
        aligned_ratio >= 0.85
        and segment_coverage >= 0.95
        and coverage >= 0.95
        and not script_mismatch
    ):
        quality = "good"
    elif (
        aligned_ratio >= 0.40
        and segment_coverage >= 0.50
        and coverage >= 0.80
        and not script_mismatch
    ):
        quality = "warning"
    else:
        quality = "failed"
    material = {
        "schema_version": "substar.segmentation-material.v1",
        "source_transcript": layout_tokens(
            (str(item["text"]) for item in units), source_language
        ),
        "units": units,
    }
    report = {
        "schema_version": "substar.reference-script-alignment.v1",
        "authority": "reference_assisted",
        "quality": quality,
        "break_symbols": symbols,
        "similarity": round(similarity, 6),
        "confidence": "high" if quality == "good" else "medium" if quality == "warning" else "low",
        "requires_review": quality != "good" or any(item["reason"] == "ambiguous_reference" for item in insertions),
        "matcher_version": REFERENCE_MATCHER_VERSION,
        "reference_sha256": hashlib.sha256(reference_text.encode("utf-8")).hexdigest(),
        "merges": merges,
        "local_decisions": list(local_scores.values()),
        "matched_token_ratio": round(matched_ratio, 6),
        "aligned_token_ratio": round(aligned_ratio, 6),
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
    reference_text: str, units: list[dict[str, Any]], source_language: str | None = None,
) -> dict[str, Any]:
    """Use the creation matcher, then bind lexical decisions to editor tokens."""
    timed = [{**unit, "start": position, "end": position + 1} for position, unit in enumerate(units)]
    material, _, report = materialize_reference_script(
        reference_text, timed, reference_break_symbols_for_language(source_language), source_language,
    )
    lexical = _timed_source_tokens(timed, source_language)
    owner_by_index = {int(unit["index"]): position for position, unit in enumerate(units)}
    owners = [owner_by_index[int(item["source_alignment_index"])] for item in lexical]
    rendered = [str(item["text"]) for item in lexical]
    changed_owners: set[int] = set()
    merge_ranges: list[tuple[int, int]] = []
    for item in report["replacements"]:
        if item["status"] == "applied":
            rendered[item["source_index"]] = item["after"]
            changed_owners.add(owners[item["source_index"]])
    extra_insertions = []
    for item in report["merges"]:
        indexes = item["source_indexes"]
        group_owners = sorted({owners[index] for index in indexes})
        cues = {units[owner].get("cue_id") for owner in group_owners}
        if len(cues) > 1:
            extra_insertions.append({"after_source_index": indexes[0] - 1,
                                     "reference_index": item["reference_index"],
                                     "text": item["after"], "reason": "existing_cue_boundary"})
            continue
        rendered[indexes[0]] = item["after"]
        for index in indexes[1:]:
            rendered[index] = ""
        changed_owners.update(group_owners)
        merge_ranges.append((group_owners[0], group_owners[-1]))
    lexical_owners = set(owners)
    for owner, unit in enumerate(units):
        if owner in lexical_owners or not str(unit.get("text", "")).strip():
            continue
        neighbor = owner - 1 if owner else owner + 1
        if neighbor in lexical_owners and units[neighbor].get("cue_id") == unit.get("cue_id"):
            # A manually inserted punctuation-only display token belongs to
            # the adjacent matched word's rendering, not a second punctuation.
            merge_ranges.append((min(owner, neighbor), max(owner, neighbor)))
            changed_owners.update((owner, neighbor))
    # Merge overlapping owner ranges: an editor token may contain several words.
    groups: list[list[int]] = []
    for left, right in sorted(merge_ranges + [(owner, owner) for owner in changed_owners]):
        if groups and left <= groups[-1][-1]:
            groups[-1] = list(range(groups[-1][0], max(right, groups[-1][-1]) + 1))
        else:
            groups.append(list(range(left, right + 1)))
    replacements, merges, edits, changes = [], [], [], []
    for group in groups:
        before = layout_tokens((units[owner]["text"] for owner in group), source_language)
        after = layout_tokens((text for index, text in enumerate(rendered)
                               if owners[index] in group and text), source_language)
        if before == after:
            continue
        indexes = [int(units[owner]["index"]) for owner in group]
        item = {"source_index": indexes[0], "source_indexes": indexes,
                "reference_index": group[0], "before": before, "after": after,
                "status": "applied", "lexical_match": False}
        (merges if len(group) > 1 else replacements).append(item)
        if len(group) == 1:
            edits.append({"index": indexes[0], "text": after})
        changes.append({"id": f"ref-{indexes[0]}", "type": "replace", "kind": "reference",
                        "source_indices": indexes, "original": before, "text": after, "status": "applied"})
    insertions = []
    for item in [*report["insertions"], *extra_insertions]:
        anchor = item["after_source_index"]
        owner_index = int(units[owners[anchor]]["index"]) if anchor >= 0 else -1
        projected = {**item, "after_source_index": owner_index}
        if anchor >= 0:
            group = next((group for group in groups if owners[anchor] in group), [owners[anchor]])
            prefix = layout_tokens((text for index, text in enumerate(rendered)
                                    if index <= anchor and owners[index] in group and text), source_language)
            projected["after_source_offset"] = len(prefix)
        insertions.append(projected)
    retained = []
    for item in report["retained_source"]:
        index = int(units[owners[item["source_index"]]]["index"])
        retained.append({**item, "source_index": index})
        changes.append({"id": f"ref-retained-{item['source_index']}", "type": "retained_source",
                        "kind": "reference", "source_indices": [index],
                        "original": item["before"], "text": "", "status": "retained"})
    projected = {**report, "replacements": replacements, "merges": merges,
                 "insertions": insertions, "retained_source": retained,
                 "requires_review": report["requires_review"] or bool(extra_insertions)}
    return {"schema_version": "substar.reference-editor.v1", "authority": "reference_assisted",
            "quality": report["quality"], "similarity": report["similarity"],
            "confidence": report["confidence"], "requires_review": projected["requires_review"],
            "edits": edits, "merges": merges, "insertions": insertions, "hidden": [],
            "reference_changes": changes, "diagnostics": report["changes"],
            "tokenization": report["tokenization"], "report": projected}
