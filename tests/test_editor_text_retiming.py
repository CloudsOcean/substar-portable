from dataclasses import replace

import pytest

from substar_core.domain import ChangeProvenance, DisplayCue, DisplayToken, EditorDocument, SourceToken
from substar_core.document_operations import apply_document_operation, apply_document_batch
from substar_core.editor.application.selective_restore import split_token
from substar_core.storage.project_store import ProjectStore


def document(gap=0):
    sources = [SourceToken.create(index=i, text=word, start=i+gap*(i>0), end=i+1)
               for i, word in enumerate(["hello", "world", "again"])]
    provenance = ChangeProvenance(kind="source", operation="asr")
    tokens = [DisplayToken.create(position=i, text=s.text, source_token_ids=[s.token_id], provenance=provenance)
              for i, s in enumerate(sources)]
    cue = DisplayCue.create(index=0, display_token_ids=[t.token_id for t in tokens], start=0, end=3)
    return EditorDocument.create(document_key="retiming", source_tokens=sources, display_tokens=tokens, cues=[cue])


def edit(doc, text, token=None, number=1):
    ids = doc.cues[0].display_token_ids
    operation = {"operation_id":f"edit-{number}", "type":"retime_text",
                 "payload":{"text":text, "cue_id":doc.cues[0].cue_id,
                            "token_id":token or ids[0], "token_ids":[token] if token else list(ids),
                            "provenance":{"kind":"manual", "operation":"inline_replace" if token else "whole_cue_edit"}}}
    return apply_document_operation(doc, operation)


def active(doc):
    displays = {t.token_id:t for t in doc.display_tokens}
    sources = {s.token_id:s for s in doc.source_tokens}
    return [(t.text, min(sources[s].start for s in t.source_token_ids),
             max(sources[s].end for s in t.source_token_ids))
            for tid in doc.cues[0].display_token_ids
            if (t:=displays[tid]).state.value == "active"]


def test_whole_edit_preserves_unchanged_words_and_divides_only_changed_span():
    old = document()
    new = edit(old, "hello good morning again")
    assert active(new) == [("hello",0,1),("good",1,1.5),("morning",1.5,2),("again",2,3)]
    assert old.display_tokens[0] in new.display_tokens
    assert old.display_tokens[2] in new.display_tokens
    assert (new.cues[0].start, new.cues[0].end) == (0,3)
    assert [s for s in new.source_tokens if s.timing][0].timing["kind"] == "subdivided"


def test_inline_edit_multiple_words_can_split_cue_and_split_token_again():
    old = document()
    new = edit(old, "good morning everyone", old.display_tokens[1].token_id)
    parts = active(new)[1:4]
    assert [p[0] for p in parts] == ["good","morning","everyone"]
    assert [p[2]-p[1] for p in parts] == pytest.approx([1/3]*3)
    tid = new.cues[0].display_token_ids[1]
    split = apply_document_operation(new, {"operation_id":"split", "type":"split_cue",
         "payload":{"cue_id":new.cues[0].cue_id, "after_token_id":tid}})
    assert split.cues[0].end == pytest.approx(4/3)
    again = split_token(new, tid, 2)
    assert [p[0] for p in active(again)][1:3] == ["go","od"]


def test_insertion_uses_gap_and_preserves_neighbors():
    old = document(gap=.2)
    new = edit(old, "hello dear world again")
    assert active(new) == [("hello",0,1),("dear",1,1.2),("world",1.2,2),("again",2.2,3)]
    assert all(t in new.display_tokens for t in old.display_tokens)


def test_split_preserves_silence_between_words():
    old = document(gap=.2)
    split = apply_document_operation(old, {"operation_id": "split-gap", "type": "split_cue",
        "payload": {"cue_id": old.cues[0].cue_id,
                    "after_token_id": old.display_tokens[0].token_id}})
    assert split.cues[0].end == 1
    assert split.cues[1].start == 1.2


def test_split_ignores_deleted_word_timing():
    old = document(gap=.2)
    deleted = apply_document_operation(old, {"operation_id": "delete-word", "type": "delete",
        "payload": {"token_ids": [old.display_tokens[1].token_id]}})
    split = apply_document_operation(deleted, {"operation_id": "split-deleted", "type": "split_cue",
        "payload": {"cue_id": old.cues[0].cue_id,
                    "after_token_id": old.display_tokens[1].token_id}})
    assert split.cues[0].end == 1
    assert split.cues[1].start == 2.2


@pytest.mark.parametrize("text,expected", [
    ("hello dear world again", [("hello",0,.5),("dear",.5,1),("world",1,2),("again",2,3)]),
    ("dear hello world again", [("dear",0,.5),("hello",.5,1),("world",1,2),("again",2,3)]),
    ("hello world again dear", [("hello",0,1),("world",1,2),("again",2,2.5),("dear",2.5,3)])])
def test_gapless_insertion_borrows_one_neighbor(text, expected):
    assert active(edit(document(), text)) == expected


def test_deletion_and_repeated_edits_remain_addressable():
    first = edit(document(), "hello again")
    assert active(first) == [("hello",0,1),("again",2,3)]
    second = edit(first, "hello beautiful again", number=2)
    assert active(second) == [("hello",0,1),("beautiful",1,2),("again",2,3)]
    third = edit(second, "hello very beautiful again", number=3)
    assert [p[0] for p in active(third)] == ["hello","very","beautiful","again"]


def test_chinese_and_zero_width_provider_span():
    old = document()
    token = old.display_tokens[1]
    new = edit(old, "你好世界", token.token_id)
    assert [p[0] for p in active(new)][1:5] == list("你好世界")
    assert [p[2]-p[1] for p in active(new)[1:5]] == [.25]*4
    zero = replace(old, source_tokens=tuple(replace(s, end=s.start) for s in old.source_tokens))
    rebuilt = edit(zero, "new words now")
    assert [p[0] for p in active(rebuilt)] == ["new","words","now"]
    assert [p[2]-p[1] for p in active(rebuilt)] == pytest.approx([2/3]*3)
    assert (rebuilt.cues[0].start,rebuilt.cues[0].end) == (0,3)


def test_history_preserves_original_timing_and_unrelated_cue(tmp_path):
    old = document()
    source = SourceToken.create(index=3,text="other",start=4,end=5)
    token = DisplayToken.create(position=3,text="other",source_token_ids=[source.token_id],provenance=old.display_tokens[0].provenance)
    cue = DisplayCue.create(index=1,display_token_ids=[token.token_id],start=4,end=5)
    old = replace(old,source_tokens=(*old.source_tokens,source),display_tokens=(*old.display_tokens,token),cues=(*old.cues,cue))
    store = ProjectStore.create(tmp_path/"project", project_id="retiming")
    before = store.save(old,provenance=ChangeProvenance(kind="manual",operation="checkpoint"))
    new = edit(old, "hello good morning again")
    after = store.save(new,provenance=new.changes[-1],expected_revision_id=before.revision_id)
    assert store.load_revision(before.revision_id).document == old
    assert store.load_revision(after.revision_id).document == new
    assert new.cues[1] == old.cues[1]
    assert new.source_tokens[-1].start == source.start


def test_inline_edit_batch_preserves_identity_and_timing():
    old = document()
    operation = {"operation_id":"batch-inline", "type":"replace", "payload":{
        "token_id":old.display_tokens[1].token_id, "text":"good morning",
        "expected_text":"world", "provenance":{"kind":"manual", "operation":"inline_replace"}}}
    result = apply_document_batch(old,[operation])
    assert result.source_tokens == old.source_tokens
    assert result.cues == old.cues
    assert active(result) == [("hello",0,1),("good morning",1,2),("again",2,3)]
