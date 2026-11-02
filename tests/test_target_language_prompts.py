import pytest
from substar_core.prompt_registry import render_prompt, translation_variant
from substar_core.editor.translation.regroup import translation_prompt_variant
from substar_core.editor.translation.contextual import _translation_system_prompt

TARGETS=['zh-CN','en','ja','ko','th','ar','fr','de','es','pt','ru','it']

@pytest.mark.parametrize('target',TARGETS)
@pytest.mark.parametrize('mode',['many_to_many','one_to_one'])
def test_target_rule_is_independent_of_source_and_shared_by_repair(target,mode):
    prompts=[render_prompt('contextual_translation',variant=translation_variant(source,target),mode=mode)
             for source in ['en','zh-CN','mixed','ja','ar']]
    assert len({p.sha256 for p in prompts})==1
    p=prompts[0]
    assert len([f for f in p.files if '/target/' in f])==1
    assert '/target/'+('zh' if target=='zh-CN' else target)+'.md' in p.files[-1]
    assert not any('/cases/' in f or 'approved_en_zh' in f for f in p.files)
    repair=render_prompt('contextual_translation_repair',variant=p.variant,mode=mode)
    assert repair.files[-1]==p.files[-1]
    assert '默认每个编号各写一行' not in p.text
    if mode=='one_to_one':
        assert '不转移到相邻 cue' in p.text
        assert '相邻时间槽之间重分配' not in p.text

def test_bidirectional_prompt_loads_only_actual_targets():
    groups=[{'cues':[{'target_language':'zh-CN'},{'target_language':'en'}]}]
    variant=translation_prompt_variant({'translation_layout':{'lower_target':'zh-CN'}},'mixed','zh-CN',groups)
    p=render_prompt('contextual_translation',variant=variant,mode='many_to_many')
    assert {f for f in p.files if '/target/' in f}=={
        'production/translation/target/zh.md','production/translation/target/en.md'}
    assert '只适用于 TARGET_LANGUAGE 对应的条目' in p.text

def test_punctuation_policy_keeps_spaces_and_symbols_for_non_chinese():
    clean=_translation_system_prompt('base','target en',[],preserve_punctuation=False)
    preserve=_translation_system_prompt('base','target en',[],preserve_punctuation=True)
    assert '不添加句子标点' in clean
    assert '必要词间空格' in clean
    assert '词内撇号' in clean
    assert '使用目标语言自然的句子标点' in preserve
    assert '不添加句子标点' not in preserve
