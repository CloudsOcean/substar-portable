"""Display word timing overlays; immutable ASR evidence stays untouched."""
from dataclasses import replace
import json

def apply_word_timing_patch(before, after, values):
    if values is None:
        return after
    if not isinstance(values, dict) or len(values) > 100000:
        raise ValueError('字词时间数据无效')
    owners={token_id:cue for cue in after.cues for token_id in cue.display_token_ids}
    for token_id, value in values.items():
        cue=owners.get(token_id)
        if cue is None or not isinstance(value,dict):
            raise ValueError('字词不存在')
        a,b=value.get('start'),value.get('end')
        if type(a) is not int or type(b) is not int or a<0 or b<=a:
            raise ValueError('字词时间必须为有效整数毫秒')
        if a < round(cue.start*1000) or b > round(cue.end*1000):
            raise ValueError('字词时间不能超出字幕范围')
    timeline=json.loads(after.properties.timeline_json or '{}')
    timeline['word_timings']={**timeline.get('word_timings',{}),**values}
    return replace(after,properties=replace(after.properties,timeline_json=json.dumps(timeline,ensure_ascii=False)))
