"""Run against the copied release tree; never borrow workers from a checkout."""
import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME_ENTRIES = (
    "run_transcription_worker.py", "run_segmentation_worker.py",
    "run_semantic_segmentation.py", "run_translation_worker.py",
    "run_calibration_worker.py", "run_reference_match.py",
)


async def verify():
    for name in RUNTIME_ENTRIES:
        if not (ROOT / "scripts" / name).is_file():
            raise RuntimeError(f"Missing runtime entry: {name}")
    from substar_core.editor.application.reference import match_reference
    class Request:
        async def is_disconnected(self):
            return False
    result = await match_reference(b"Hello world.", "reference.txt", [
        {"index":0,"text":"Hello","start":0.0,"end":0.5},
        {"index":1,"text":"world","start":0.5,"end":1.0},
    ], "en", Request())
    if not isinstance(result, dict):
        raise RuntimeError("Reference worker did not return a result")
    print("Runtime entries and reference matching: OK")


if __name__ == "__main__":
    asyncio.run(verify())
