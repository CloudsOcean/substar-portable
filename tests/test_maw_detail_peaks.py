import base64
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace
import pytest
from substar_core.editor.maw_detail_peaks import decode_container, generate_container, get_detail_waveform


def container(channels=2, rate=16000, div=53):
    return b'QPK1'+bytes([channels,1])+struct.pack('<III',rate,0,0)+struct.pack('<ii',div,1)+b''.join(struct.pack('<hh',hi,lo) for hi,lo in [(0,0),(30000,-16000)][:channels])


def test_finest_peaks_keep_exact_fractional_time_and_all_channels():
    payload=decode_container(container())
    assert payload['sample_rate']/payload['division']==16000/53
    assert payload['peaks_per_second']==16000/53
    low,high=struct.unpack('bb',base64.b64decode(payload['data']))
    assert low<0 and high>100  # Speech only in the right channel must survive.


@pytest.mark.parametrize('data',[b'',b'QPK1',container()[:-1],b'FAKE'+container()[4:]])
def test_truncated_or_unknown_containers_fail(data):
    with pytest.raises(ValueError):decode_container(data)


def test_native_generation_retains_sub_millisecond_transient(tmp_path):
    pytest.importorskip('substar_core.vendor.quapeaks._native')
    source=tmp_path/'source.wav'
    with wave.open(str(source),'wb') as audio:
        audio.setparams((1,2,16000,0,'NONE',''))
        values=[0]*16000;values[7000]=30000
        audio.writeframes(struct.pack('<16000h',*values))
    target=tmp_path/'wave.quapeaks'
    assert generate_container(source,target)==1000
    payload=decode_container(target.read_bytes())
    assert payload['peaks_per_second']==2000
    high=base64.b64decode(payload['data'])[1::2]
    assert max(high)>100
    assert high.index(max(high))==875


def test_cache_reuses_valid_source_and_rebuilds_after_change(tmp_path,monkeypatch):
    import sys
    from substar_core.editor import maw_detail_peaks as module
    source=tmp_path/'source.wav';source.write_bytes(b'first')
    cache=tmp_path/'cache'
    monkeypatch.setitem(sys.modules,'substar_core.editor.http_api',SimpleNamespace(_project_media_source=lambda _: (source,{}),project_store_path=lambda _:cache))
    calls=[]
    def generate(src,target):
        calls.append(1);target.write_bytes(container());return 1000
    monkeypatch.setattr(module,'generate_container',generate)
    first=get_detail_waveform('test');second=get_detail_waveform('test')
    assert not first['cache_hit'] and second['cache_hit'] and len(calls)==1
    source.write_bytes(b'changed source')
    assert not get_detail_waveform('test')['cache_hit'] and len(calls)==2
