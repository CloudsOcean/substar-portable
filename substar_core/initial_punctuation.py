"""One-time cleanup of editor text; recognition evidence stays unchanged."""
import re
import unicodedata
from dataclasses import replace

_PROTECTED = re.compile(r"(?:https?://|www\.)[^\s<>\"“”，。！？；：]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:[A-Za-z]\.){2,}")


def clean_sentence_punctuation(text: str) -> str:
    protected = set()
    for match in _PROTECTED.finditer(text):
        end = match.end()
        while not re.fullmatch(r'(?:[A-Za-z]\.){2,}', match.group()) and end > match.start() and text[end-1] in '.,，。!?！？;；:：':
            end -= 1
        protected.update(range(match.start(), end))
    result = []
    for i, char in enumerate(text):
        before = text[i-1] if i else ''
        after = text[i+1] if i+1 < len(text) else ''
        lexical = char in "'’ʼ-‐‑_/&" and before.isalnum() and after.isalnum()
        lexical = lexical or (char in '*#@' and (before.isalnum() or after.isalnum()))
        numeric = (char in '.,，:/-–' and before.isdigit() and after.isdigit()) or (char in '-+%' and (before.isdigit() or after.isdigit())) or (char == '％' and before.isdigit())
        if i in protected or lexical or numeric or not unicodedata.category(char).startswith('P'):
            result.append(char)
    return ''.join(result).strip()


def clean_initial_document(document):
    from substar_core.domain import EntityState
    tokens = []
    raw = document.display_tokens
    for i, token in enumerate(raw):
        text = clean_sentence_punctuation(token.text)
        if not text and i and i+1 < len(raw) and token.text in {'.', ',', ':', '/', '-', '’', "'"}:
            left, right = raw[i-1].text[-1:], raw[i+1].text[:1]
            if (left.isdigit() and right.isdigit()) or (token.text in {'’', "'", '-'} and left.isalnum() and right.isalnum()):
                text = token.text
        tokens.append(replace(token, text=text) if text else replace(token, state=EntityState.DELETED))
    tokens = tuple(tokens)
    active = {token.token_id for token in tokens if token.state is EntityState.ACTIVE}
    cues = []
    for cue in document.cues:
        source = clean_sentence_punctuation(cue.source_text) if cue.source_text is not None else None
        target = cue.target
        if target:
            text = clean_sentence_punctuation(target.target_text)
            target = replace(target, target_text=text, original_text=target.original_text or target.target_text,
                             **({"translation_status":"manual_required", "issue_code":"translation_unresolved", "editable":True} if not text else {}))
        state = cue.state
        if (source is not None and not source) or (source is None and not active.intersection(cue.display_token_ids)):
            state = EntityState.DELETED
            if source is not None and not source and target is None:
                source = cue.source_text  # Retain the hidden punctuation-only evidence.
        cues.append(replace(cue, source_text=source, target=target, state=state))
    return replace(document, display_tokens=tokens, cues=tuple(cues))
