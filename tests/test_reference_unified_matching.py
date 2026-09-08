from __future__ import annotations

import asyncio
import io
from unittest.mock import patch

from starlette.datastructures import UploadFile

from substar_core.manuscript_matching import (
    editor_reference_operations, materialize_reference_alignment,
    materialize_reference_script,
)
from substar_core.segmentation.document_builder import (
    apply_reference_report, apply_semantic_display_projection,
    attach_semantic_reference_audit, build_reference_script_document,
)
from substar_core.segmentation.input_contract import build_segmentation_material_with_reference_projection
from substar_core.contracts.editor_document import build_editor_document, source_tokens_from_asr
from substar_core.document_operations import apply_document_operation
from substar_core.export import SubtitleExportMode, render_document_srt


def units(texts):
    return [dict(index=i, text=text, start=i * .4, end=(i + 1) * .4, speaker_id=None)
            for i, text in enumerate(texts)]


def document(material, breaks=None):
    return build_editor_document(
        source_tokens=source_tokens_from_asr(material['units'], source_asset_id='test'),
        source_kind='asr', source_asset_id='test',
        execution_plan={'blocks': [], 'boundaries_after': [], 'skipped_reason': None},
        semantic_grouping={'protections': [], 'meaning_groups': [], 'review_regions': []},
        cue_layout={'display_breaks': breaks or []},
    )


def visible(doc):
    by_id = {token.token_id: token for token in doc.display_tokens}
    return ''.join(by_id[token_id].text for cue in doc.cues if cue.state.value == 'active'
                   for token_id in cue.display_token_ids if by_id[token_id].state.value == 'active')


def script_doc(reference, source, language='zh'):
    material, breaks, report = materialize_reference_script(reference, source, '，。.!?', language)
    return build_reference_script_document(material, source_asset_id='test', display_breaks=breaks,
                                           reference_report=report), report


def semantic_doc(reference, source, language='zh'):
    master, aligned, report = materialize_reference_alignment(reference, {'units': source}, language)
    material, projection, suggestions = build_segmentation_material_with_reference_projection(master, aligned)
    doc = apply_semantic_display_projection(document(material), projection)
    return attach_semantic_reference_audit(doc, report, suggestions), report


def rematch_doc(reference, source, language='zh', breaks=None):
    doc = document({'units': source}, breaks)
    cue_by_token = {token_id: cue.cue_id for cue in doc.cues for token_id in cue.display_token_ids}
    source = [{**unit, 'cue_id': cue_by_token[token.token_id]}
              for unit, token in zip(source, doc.display_tokens)]
    result = editor_reference_operations(reference, source, language)
    return apply_reference_report(doc, result['report'],
                                  {i: token.token_id for i, token in enumerate(doc.display_tokens)}), result


def test_all_entries_preserve_asr_extras_and_hide_reference_extras():
    reference = '我们今天一起去美丽公园散步然后回家吃晚饭'
    source = units('我们今天一起去公园散步嗯然后回家吃晚饭')
    for build in (script_doc, semantic_doc, rematch_doc):
        doc, _ = build(reference, source)
        assert visible(doc) == '我们今天一起去公园散步嗯然后回家吃晚饭'
        hidden = [t for t in doc.display_tokens if t.state.value == 'deleted']
        assert ''.join(t.text for t in hidden) == '美丽'
        assert all(not t.source_token_ids for t in hidden)
        assert '美丽' not in render_document_srt(doc, SubtitleExportMode.SOURCE)
        assert any(row['type'] == 'retained_source' for row in doc.changes[-1].metadata['reference_changes'])
        restored = apply_document_operation(doc, {'operation_id': 'restore-reference', 'type': 'restore',
            'payload': {'token_ids': [t.token_id for t in hidden]}})
        assert '美丽' in render_document_srt(restored, SubtitleExportMode.SOURCE)
        assert [(c.cue_id, c.start, c.end) for c in restored.cues] == [(c.cue_id, c.start, c.end) for c in doc.cues]


def test_identical_chinese_words_are_a_noop():
    result = editor_reference_operations('我们来到重庆。', units(['我们', '来到', '重庆。']), 'zh')
    assert result['edits'] == []
    assert result['merges'] == []
    assert result['insertions'] == []


def test_unrelated_reference_has_no_boundaries_or_fake_coverage():
    doc, report = script_doc('甲。乙。丙。', units('丁戊己'))
    assert report['quality'] == 'failed'
    assert report['segment_coverage'] == 0
    assert report['reference_breaks'] == []
    assert visible(doc) == '丁戊己'
    assert ''.join(t.text for t in doc.display_tokens if t.state.value == 'deleted') == '甲。乙。丙。'


def test_locally_anchored_replacement_does_not_need_high_global_similarity():
    for build in (script_doc, semantic_doc, rematch_doc):
        doc, _ = build('快速进攻梁地。', units('快速支援梁地。'))
        assert visible(doc) == '快速进攻梁地。'


def test_many_to_one_uses_whole_source_envelope_and_is_reversible():
    source = units(['we', 'visit', 'new', 'york', 'today', 'together'])
    for build in (script_doc, semantic_doc, rematch_doc):
        doc, _ = build('we visit NYC today together', source, 'en')
        assert 'NYC' in visible(doc)
        assert 'new' not in visible(doc) and 'york' not in visible(doc)
        assert doc.cues[0].start == 0 and doc.cues[-1].end == source[-1]['end']
        row = next(row for row in doc.changes[-1].metadata['reference_changes'] if row['after'] == 'NYC')
        token = next(t for t in doc.display_tokens if t.token_id == row['token_ids'][0])
        reverted = apply_document_operation(doc, {'operation_id': 'revert', 'type': 'replace',
            'payload': {'token_id': token.token_id, 'text': row['before']}})
        assert 'new york' in visible(reverted)


def test_reference_repetition_increases_local_score_and_acceptance():
    once = editor_reference_operations('brand today together', units(['noise', 'today', 'together']), 'en')
    repeated = editor_reference_operations('brand today together brand now again brand next week',
        units(['noise', 'today', 'together', 'brand', 'now', 'again', 'brand', 'next', 'week']), 'en')
    assert once['edits'] == []
    assert repeated['edits'] == [{'index': 0, 'text': 'brand'}]
    score = repeated['report']['local_decisions'][0]
    assert score['reference_frequency'] == 3 and score['frequency_boost'] > 0


def test_repeated_sentences_keep_distinct_source_positions():
    source = units('甲乙来了甲乙来了甲乙走了')
    _, _, report = materialize_reference_script('甲乙来了。甲乙来了。甲乙走了。', source, '。', 'zh')
    positions = [r['source_index'] for r in report['provenance']]
    assert positions == list(range(12))
    assert report['reference_breaks'] == [3, 7]


def test_rematch_does_not_merge_across_existing_cues():
    source = units(['we', 'visit', 'new', 'york', 'today', 'together'])
    doc, result = rematch_doc('we visit NYC today together', source, 'en', [2])
    assert result['merges'] == []
    assert 'newyork' in visible(doc)
    assert any(t.text == 'NYC' and t.state.value == 'deleted' for t in doc.display_tokens)
    assert len(doc.cues) == 2 and doc.cues[0].end == source[2]['end']


def test_multichar_owner_keeps_asr_only_word():
    result = editor_reference_operations('我们今天出发。', units(['我们嗯今天', '出发。']), 'zh')
    assert all(edit['text'] != '我们今天' for edit in result['edits'])
    doc, _ = rematch_doc('我们今天出发。', units(['我们嗯今天', '出发。']))
    assert visible(doc) == '我们嗯今天出发。'


def test_hidden_insertions_survive_serialization():
    from substar_core.domain import EditorDocument
    doc, _ = rematch_doc('甲乙新丙丁', units('甲乙丙丁'))
    loaded = EditorDocument.from_dict(doc.to_dict())
    assert visible(loaded) == '甲乙丙丁'
    assert any(t.text == '新' and t.state.value == 'deleted' for t in loaded.display_tokens)


def test_numeric_variants_align_in_both_directions():
    for reference, source in [('二十', units(['20'])), ('20', units(['二', '十']))]:
        for build in (script_doc, semantic_doc, rematch_doc):
            doc, _ = build(reference, source)
            assert visible(doc) == reference
            assert doc.cues[0].start == source[0]['start']
            assert doc.cues[-1].end == source[-1]['end']
            row = next(row for row in doc.changes[-1].metadata['reference_changes'] if row['type'] == 'replace')
            reverted = apply_document_operation(doc, {'operation_id': 'revert-number', 'type': 'replace',
                'payload': {'token_id': row['token_ids'][0], 'text': row['before']}})
            assert visible(reverted) == ''.join(unit['text'] for unit in source)


def test_reference_frequency_is_capped_and_cannot_override_missing_context():
    result = editor_reference_operations('brand ' * 100, units(['noise']), 'en')
    assert result['edits'] == []
    assert result['report']['local_decisions'][0]['frequency_boost'] <= .16


def test_rematching_reuses_existing_hidden_insertions():
    reference = '甲乙新丙丁'
    doc, _ = rematch_doc(reference, units('甲乙丙丁'))
    original_hidden = [t.token_id for t in doc.display_tokens if t.state.value == 'deleted']
    active = [t for t in doc.display_tokens if t.state.value == 'active']
    result = editor_reference_operations(reference, [{'index': i, 'text': t.text} for i, t in enumerate(active)], 'zh')
    rematched = apply_reference_report(doc, result['report'], {i: t.token_id for i, t in enumerate(active)},
                                       operation_prefix='second-rematch')
    assert [t.token_id for t in rematched.display_tokens if t.state.value == 'deleted'] == original_hidden


def test_hidden_words_restore_inside_multichar_editor_token_in_order():
    doc, _ = rematch_doc('我们新今天再出发', units(['我们今天出发']))
    assert visible(doc) == '我们今天出发'
    hidden = [t.token_id for t in doc.display_tokens if t.state.value == 'deleted']
    restored = apply_document_operation(doc, {'operation_id': 'restore-interior', 'type': 'restore',
                                             'payload': {'token_ids': hidden}})
    assert visible(restored) == '我们新今天再出发'
    assert restored.source_tokens == doc.source_tokens
    assert [(c.start, c.end) for c in restored.cues] == [(0, .4)]


def test_semantic_projection_preserves_native_timing_lineage():
    source = units(['我们', '重青', '今天'])
    source[1]['index'] = 17
    source[1]['timing'] = {'kind': 'native', 'native_token_ids': ['qwen-17'],
                            'native_start': .4, 'native_end': .8}
    _, aligned, _ = materialize_reference_alignment('我们重庆今天', {'units': source}, 'zh')
    corrected = next(t for t in aligned['units'] if t['text'] == '庆')
    assert corrected['timing']['native_token_ids'] == ['qwen-17']
    assert corrected['timing']['native_start'] == .4
    assert corrected['timing']['native_end'] == .8


def test_isolated_matching_subprocess_returns_shared_report():
    from substar_core.editor.application.reference import match_reference
    class Request:
        async def is_disconnected(self):
            return False
    result = asyncio.run(match_reference('甲乙新丙丁'.encode(), 'reference.txt',
                                         units('甲乙丙丁'), 'zh', Request()))
    assert result['edits'] == []
    assert result['insertions'][0]['text'] == '新'
    assert result['report']['matcher_version'] == 'local-reference-v4'


def test_editor_endpoint_commits_hidden_insertions_atomically():
    from types import SimpleNamespace
    from substar_core.editor import http_api
    original = document({'units': units('甲乙丙丁')})
    latest = SimpleNamespace(document=original, revision_id='rev-test')
    store = SimpleNamespace(load_latest=lambda: latest)
    captured = {}
    def save(*args, **kwargs):
        captured.update(kwargs)
        return {'revision_id': 'rev-next'}
    async def match(payload, filename, source, language, request):
        return editor_reference_operations(payload.decode(), source, language)
    with patch.object(http_api, 'open_project_store', return_value=store), \
         patch.object(http_api, 'get_project_task_info', return_value={'language': 'zh'}), \
         patch.object(http_api, '_save_document', side_effect=save), \
         patch('substar_core.editor.application.reference.match_reference', side_effect=match):
        result = asyncio.run(http_api.match_project_reference_manuscript(
            'project-test', SimpleNamespace(), expected_revision_id='rev-test',
            file=UploadFile(io.BytesIO('甲乙新丙丁'.encode()), filename='reference.txt')))
    assert result['revision']['revision_id'] == 'rev-next'
    assert captured['expected_revision_id'] == 'rev-test'
    assert visible(captured['document']) == '甲乙丙丁'
    assert any(t.text == '新' and t.state.value == 'deleted' for t in captured['document'].display_tokens)


def test_reordered_short_phrase_uses_reference_in_all_entries():
    source = units('沟通河北与中原两地的白马围津两大渡口')
    reference = '沟通中原梁地与河北的白马围津两大渡口'
    for build in (script_doc, semantic_doc, rematch_doc):
        doc, _ = build(reference, source)
        assert visible(doc) == reference
        assert not any(t.state.value == 'deleted' for t in doc.display_tokens)


def test_short_document_tail_uses_left_context_and_shared_end():
    source = units(['秦', '楚', '攻', '守', '一', '行。'])
    for build in (script_doc, semantic_doc, rematch_doc):
        doc, _ = build('秦楚攻守异形！！！', source)
        assert visible(doc) == '秦楚攻守异形！！！'
        assert not any(t.state.value == 'deleted' for t in doc.display_tokens)


def test_rematch_removes_superseded_hidden_candidates():
    doc, _ = rematch_doc('甲乙新丙丁', units('甲乙丙丁'))
    active = [t for t in doc.display_tokens if t.state.value == 'active']
    result = editor_reference_operations('甲乙丙丁', [{'index': i, 'text': t.text} for i,t in enumerate(active)], 'zh')
    changed = apply_reference_report(doc, result['report'], {i:t.token_id for i,t in enumerate(active)}, operation_prefix='new-reference')
    assert visible(changed) == '甲乙丙丁'
    assert not any(t.state.value == 'deleted' for t in changed.display_tokens)
    assert any(t.state.value == 'deleted' for t in doc.display_tokens)


def test_independent_added_sentence_is_not_swallowed_by_phrase_rule():
    doc, _ = script_doc('甲乙来了。新增整句。丙丁走了。', units(['甲乙来了。', '丙丁走了。']))
    assert visible(doc) == '甲乙来了。丙丁走了。'
    assert any(t.state.value == 'deleted' for t in doc.display_tokens)
