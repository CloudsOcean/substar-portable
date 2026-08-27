from __future__ import annotations

from array import array
import tempfile
from pathlib import Path
import unittest
import wave

from substar_core.editor.domain.cue_timing import smart_snap_search_minimum
from substar_core.media.waveform_cache import smart_forward_snap


def _write_onset_fixture(
    path: Path,
    *,
    onset: float = 0.5,
    duration: float = 1.2,
    amplitude: int = 12_000,
) -> None:
    rate = 16_000
    samples = array(
        "h",
        (
            0
            if frame < int(onset * rate)
            else (amplitude if frame % 16 < 8 else -amplitude)
            for frame in range(int(duration * rate))
        ),
    )
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(samples.tobytes())


class SmartForwardSnapTests(unittest.TestCase):
    def test_touching_cues_allow_shared_boundary_search(self) -> None:
        self.assertEqual(
            smart_snap_search_minimum(
                previous_start=1.0,
                previous_end=2.0,
                current_start=2.0,
            ),
            1.04,
        )

    def test_real_gap_and_manual_cue_remain_hard_barriers(self) -> None:
        self.assertEqual(
            smart_snap_search_minimum(
                previous_start=1.0,
                previous_end=2.0,
                current_start=2.2,
            ),
            2.0,
        )
        self.assertEqual(
            smart_snap_search_minimum(
                previous_start=1.0,
                previous_end=2.0,
                current_start=2.0,
                previous_is_manual=True,
            ),
            2.0,
        )

    def test_finds_onset_well_before_the_old_150ms_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "onset.wav"
            _write_onset_fixture(audio)
            result = smart_forward_snap(
                audio,
                [{"cue_id": "cue-1", "start": 0.72, "minimum_start": 0.0}],
            )

        self.assertEqual(result["search_window_ms"], 1000)
        self.assertEqual(result["pre_roll_ms"], 40)
        self.assertEqual(result["sensitivity"], 50)
        self.assertEqual(len(result["changes"]), 1)
        change = result["changes"][0]
        self.assertGreaterEqual(change["snapped_start"], 0.42)
        self.assertLessEqual(change["snapped_start"], 0.49)
        self.assertGreater(change["offset_ms"], 200)

    def test_adds_bounded_preroll_without_crossing_previous_cue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "onset.wav"
            _write_onset_fixture(audio)
            result = smart_forward_snap(
                audio,
                [{"cue_id": "cue-1", "start": 0.72, "minimum_start": 0.48}],
            )

        self.assertEqual(result["changes"][0]["snapped_start"], 0.48)

    def test_preroll_and_sensitivity_are_adjustable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "quiet-onset.wav"
            _write_onset_fixture(audio, amplitude=50)
            default_result = smart_forward_snap(
                audio,
                [{"cue_id": "cue-1", "start": 0.72, "minimum_start": 0.0}],
            )
            sensitive_result = smart_forward_snap(
                audio,
                [{"cue_id": "cue-1", "start": 0.72, "minimum_start": 0.0}],
                pre_roll_ms=0,
                sensitivity=100,
            )

        self.assertEqual(default_result["changes"], [])
        self.assertEqual(sensitive_result["pre_roll_ms"], 0)
        self.assertEqual(sensitive_result["sensitivity"], 100)
        self.assertEqual(len(sensitive_result["changes"]), 1)
        self.assertGreaterEqual(sensitive_result["changes"][0]["snapped_start"], 0.49)


if __name__ == "__main__":
    unittest.main()
