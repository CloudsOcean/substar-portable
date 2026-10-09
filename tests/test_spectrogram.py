import struct
import wave

import numpy as np
import pytest

from substar_core.media.spectrogram import spectral_levels, spectrogram_png


def make_audio(path):
    rate = 16000
    samples = np.zeros(rate * 2)
    samples[rate:] = .5 * np.sin(2 * np.pi * 2000 * np.arange(rate) / rate)
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, rate, 0, "NONE", ""))
        output.writeframes((samples * 32767).astype("<i2").tobytes())


def test_frequency_and_time_positions(tmp_path):
    audio = tmp_path / "tone.wav"
    make_audio(audio)
    levels = spectral_levels(audio, 0, 2, 200, 128)
    assert levels[:, :90].max() == 0
    band = levels[:, 120:180].mean(axis=1).argmax()
    assert abs(band - 95) <= 2  # 2 kHz, one quarter of the 8 kHz Nyquist range
    cropped = spectral_levels(audio, 1.2, 1.8, 200, 128)
    assert abs(cropped.mean(axis=1).argmax() - band) <= 1
    png = spectrogram_png(audio, 0, 2)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack("!II", png[16:24]) == (1200, 192)
    assert png == spectrogram_png(audio, 0, 2)


def test_bounds_and_beyond_end(tmp_path):
    audio = tmp_path / "tone.wav"
    make_audio(audio)
    for start, end in [(0, 61), (2, 1), (0, float("inf")), (float("nan"), 2)]:
        with pytest.raises(ValueError):
            spectrogram_png(audio, start, end)
    assert not spectral_levels(audio, 3, 4, 128, 128).any()

