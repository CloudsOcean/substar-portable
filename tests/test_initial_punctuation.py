import pytest
from substar_core.initial_punctuation import clean_sentence_punctuation, clean_initial_document
from substar_core.editor.application.subtitle_project_import import preview_subtitle_project, build_subtitle_document


@pytest.mark.parametrize('raw,expected',[
    ('“你好，世界！”好吗？','你好世界好吗'),
    ("Hello, world! Don't re-sign.","Hello world Don't re-sign"),
    ('价格-5.25美元，占12.5%。','价格-5.25美元占12.5%'),
    ('时间12:30，日期2026-11-02。','时间12:30日期2026-11-02'),
    ('https://example.com/a?q=1&x=2，邮箱a.b@test.com。','https://example.com/a?q=1&x=2邮箱a.b@test.com'),
    ('U.S.A.：“好”。','U.S.A.好'),
    ('（测试）【完成】……','测试完成'),
    ('2*3=6；C#、AT&T、and/or、snake_case。','2*3=6C#AT&Tand/orsnake_case'),
])
def test_cleanup_preserves_lexical_symbols(raw, expected):
    assert clean_sentence_punctuation(raw) == expected


def test_import_cleaned_once_and_original_preview_unchanged():
    raw='1\n00:00:00,000 --> 00:00:01,000\n“Hello, world!”\n'
    preview=preview_subtitle_project(raw,mode='single')
    original=build_subtitle_document(preview,'p')
    cleaned=clean_initial_document(original)
    assert cleaned.cues[0].source_text == 'Hello world'
    assert original.cues[0].source_text == '“Hello, world!”'
    # Cleanup is an explicit creation step, not a save/load or rendering filter.
    from dataclasses import replace
    edited=replace(cleaned,cues=(replace(cleaned.cues[0],source_text='Hello, world!'),))
    assert edited.to_dict()['cues'][0]['source_text'] == 'Hello, world!'


def test_punctuation_only_import_remains_valid_and_hidden():
    raw='1\n00:00:00,000 --> 00:00:01,000\n？！\n'
    cleaned=clean_initial_document(build_subtitle_document(preview_subtitle_project(raw,mode='single'),'p'))
    assert cleaned.cues[0].state.value == 'deleted'


def test_token_cleanup_keeps_evidence_and_identity():
    from substar_core.domain import SourceToken,DisplayToken,DisplayCue,EditorDocument,ChangeProvenance,ChangeKind
    evidence=SourceToken.create(index=0,text='Hello,',start=0,end=1)
    token=DisplayToken.create(position=0,text='Hello,',source_token_ids=(evidence.token_id,),provenance=ChangeProvenance(ChangeKind.SOURCE,'fixture'))
    original=EditorDocument('p',(evidence,),(token,),(DisplayCue('cue',0,(token.token_id,),0,1),))
    cleaned=clean_initial_document(original)
    assert cleaned.source_tokens == original.source_tokens
    assert cleaned.display_tokens[0].text == 'Hello'
    assert cleaned.display_tokens[0].original_text == 'Hello,'
    assert cleaned.display_tokens[0].token_id == token.token_id
    assert cleaned.cues == original.cues
