from argparse import Namespace
import copy
import json
from unittest.mock import patch

import pytest

from scripts.run_semantic_segmentation import (
    request_semantic_grouping_block, semantic_grouping_binding,
)
from substar_core.segmentation.material import AlignmentUnit


@pytest.mark.parametrize('repair_kind', ['overflow', 'invalid', 'valid'])
def test_single_repair_preserves_model_boundaries_on_delivery(tmp_path, repair_kind):
    units = [AlignmentUnit(index=i, start=i, end=i + .8, text=letter * 30)
             for i, letter in enumerate('ABCD')]
    _, binding = semantic_grouping_binding(units, 0, 3, 1,
                                          sentence_boundary_policy='unpunctuated')
    primary = {
        'schema_version': 'substar.semantic-grouping-result.v1', **binding,
        'meaning_groups': [{'alignment_start': 0, 'alignment_end': 3,
                            'line_breaks_after': [1, 3]}], 'exceptions': [],
    }
    repaired = copy.deepcopy(primary)
    repaired['meaning_groups'][0]['line_breaks_after'] = (
        [0, 2, 3] if repair_kind == 'overflow' else [0, 1, 2, 3])
    if repair_kind == 'invalid':
        repaired = {'invalid': True}
    args = Namespace(
        source_language='en', hard_limit=55, repair_attempts=7,
        output_dir=tmp_path, sentence_boundary_policy='unpunctuated',
        grouping_model='test', base_url='https://example.test', api_key='test',
        auth_mode='bearer', timeout=30, api_telemetry=[],
    )
    with patch('scripts.run_semantic_segmentation.model_cue_script',
               return_value=repaired) as model:
        result = request_semantic_grouping_block(
            units, (0, 3), 1, args, 'prompt', [], cached_value=primary)
    assert model.call_count == 1
    expected = {'overflow': {0, 2}, 'invalid': {1}, 'valid': {0, 1, 2}}
    assert result[4] == expected[repair_kind]
    assert bool(result[5]) == (repair_kind != 'valid')
    audit = json.loads((tmp_path / 'semantic_grouping_repair_c0001.json').read_text(encoding='utf-8'))
    assert audit['repair_attempts'] == 1
    assert audit['primary_finalizer_hard_limit_splits'] == []
    if repair_kind != 'valid':
        assert audit['delivery_source'] == 'model_response'
