from __future__ import annotations

from substar_core.editor.calibration.worker import _active_progress_units


def test_calibration_worker_reads_primary_counts_from_common_progress_units() -> None:
    assert _active_progress_units({
        "phase": "executing",
        "units": {"completed": 3, "planned": 9},
    }) == (3, 9)


def test_calibration_worker_switches_to_repair_counts_during_repair() -> None:
    assert _active_progress_units({
        "phase": "repair",
        "units": {
            "completed": 9,
            "planned": 9,
            "repair_completed": 1,
            "repair_planned": 2,
        },
    }) == (1, 2)
