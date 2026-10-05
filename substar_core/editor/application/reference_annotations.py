"""Keep punctuation outside wholly unspoken reference annotations visible."""
from dataclasses import replace

from substar_core.domain import ChangeProvenance, EditorDocument


def repair_deleted_reference_annotations(document: EditorDocument) -> EditorDocument:
    tokens = {token.token_id: token for token in document.display_tokens}
    ordered = list(dict.fromkeys(tid for cue in document.cues for tid in cue.display_token_ids))
    chars = [(tid, offset, char) for tid in ordered
             for offset, char in enumerate(tokens[tid].text)]
    pairs = {"(": ")", "（": "）", "[": "]", "【": "】"}
    stack = []
    removals: dict[str, set[int]] = {}
    suffixes: dict[str, str] = {}
    prefixes: dict[str, str] = {}
    for position, (tid, offset, char) in enumerate(chars):
        if char in pairs:
            stack.append(position)
            continue
        if not stack or char != pairs[chars[stack[-1]][2]]:
            continue
        opening = stack.pop()
        interior = chars[opening + 1:position]
        hidden_reference = any(
            tokens[item_id].state.value == "deleted"
            and tokens[item_id].provenance.operation == "reference_manuscript_insert"
            for item_id, _, _ in interior
        )
        spoken = any(tokens[item_id].state.value == "active" and letter.isalnum()
                     for item_id, _, letter in interior)
        if not hidden_reference or spoken:
            continue
        for item_id, char_offset, _ in (chars[opening], chars[position]):
            if tokens[item_id].state.value == "active":
                removals.setdefault(item_id, set()).add(char_offset)
        # Keep the deleted suggestion paired as well. The opening delimiter
        # often belongs to the preceding spoken token, not to the annotation.
        opening_id, _, opening_char = chars[opening]
        if tokens[opening_id].state.value == "active":
            first_hidden = next((item_id for item_id, _, _ in interior
                                 if tokens[item_id].state.value == "deleted"), None)
            if first_hidden:
                prefixes[first_hidden] = prefixes.get(first_hidden, "") + opening_char
        if tokens[tid].state.value == "active":
            last_hidden = next((item_id for item_id, _, _ in reversed(interior)
                                if tokens[item_id].state.value == "deleted"), None)
            if last_hidden:
                suffixes[last_hidden] = suffixes.get(last_hidden, "") + char
        # The closing token can also own the sentence's comma/full stop. It
        # stays in the audit ledger; copy only that external punctuation out.
        closing = tokens[tid]
        if closing.state.value == "deleted":
            tail = closing.text[offset + 1:].strip()
            if tail and all(c in ",.;:!?，。；：！？" for c in tail):
                anchor = next((item_id for item_id, _, _ in reversed(chars[:opening])
                               if tokens[item_id].state.value == "active"), None)
                if anchor:
                    suffixes[anchor] = suffixes.get(anchor, "") + tail
                    removals.setdefault(tid, set()).update(range(offset + 1, len(closing.text)))
    edits = {}
    for tid in removals.keys() | suffixes.keys() | prefixes.keys():
        text = prefixes.get(tid, "") + "".join(c for i, c in enumerate(tokens[tid].text)
                       if i not in removals.get(tid, set()))
        tail = suffixes.get(tid, "")
        if tail and not text.rstrip().endswith(tail):
            text += tail
        if text and text != tokens[tid].text:
            edits[tid] = text
    if not edits:
        return document
    audit = ChangeProvenance(kind="import", operation="reference_annotation_punctuation_repair",
                             actor="reference-manuscript",
                             metadata={"replacements": [
                                 {"token_id": tid, "before": tokens[tid].text, "after": text}
                                 for tid, text in sorted(edits.items())]})
    return replace(document,
                   display_tokens=tuple(replace(t, text=edits[t.token_id],
                                               provenance=t.provenance if t.state.value == "deleted" else audit)
                                        if t.token_id in edits else t for t in document.display_tokens),
                   changes=(*document.changes, audit))
