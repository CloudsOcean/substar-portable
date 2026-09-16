from substar_core.subtitle_exports import BilingualBlock, render_track
from substar_core.editor.application.srt_import import parse_srt
from substar_core.export import render_document_srt
from substar_core.editor.application.subtitle_project_import import preview_subtitle_project, build_subtitle_document


def test_legacy_and_document_exports_are_complete_a_then_complete_b():
    timing = ['00:00:01,000 --> 00:00:02,000', '00:00:03,000 --> 00:00:04,000']
    blocks = [BilingualBlock(i + 1, t, f'English {i}', f'中文{i}') for i, t in enumerate(timing)]
    bilingual = render_track(blocks, mode='ab_two_line')
    document = build_subtitle_document(preview_subtitle_project(bilingual, mode='bilingual-lines'), 'export-fixture')
    for text in (render_track(blocks, mode='ab_inline'), render_document_srt(document, 'ab-single')):
        rows = parse_srt(text)
        assert [r['number'] for r in rows] == ['1', '2', '3', '4']
        assert [r['text'] for r in rows] == ['English 0', 'English 1', '中文0', '中文1']
        assert [r['start'] for r in rows] == [1000, 3000, 1000, 3000]
        assert [r['end'] for r in rows] == [2000, 4000, 2000, 4000]
