from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.pipeline import _chatbox_material  # noqa: E402
from substar_core.qwen_backend import _join_text  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从已完成的独立 ASR 分块重建带正确边界空格的主稿"
    )
    parser.add_argument("--source-job", required=True, type=Path)
    parser.add_argument("--output-job", required=True, type=Path)
    args = parser.parse_args()

    source = args.source_job.resolve()
    output = args.output_job.resolve()
    alignment = json.loads((source / "alignment.json").read_text(encoding="utf-8"))
    chunks = alignment.get("chunks", [])
    if not chunks:
        raise SystemExit("alignment.json 不包含可重建的 ASR chunks")
    master = _join_text([str(chunk.get("text", "")) for chunk in chunks]).strip()
    if not master:
        raise SystemExit("ASR chunks 不能重建非空主稿")

    output.mkdir(parents=True, exist_ok=True)
    alignment["master_text"] = master
    (output / "alignment.json").write_text(
        json.dumps(alignment, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "master_transcript.txt").write_text(master + "\n", encoding="utf-8")
    (output / "chatbox_material.md").write_text(
        _chatbox_material(master, alignment),
        encoding="utf-8",
    )
    for name in ("alignment.tsv", "audio_16k_mono.wav", "run_manifest.json"):
        source_path = source / name
        if source_path.exists():
            shutil.copy2(source_path, output / name)
    manifest_path = output / "run_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["master_character_count"] = len(master)
        manifest["master_rebuilt_from_chunks"] = True
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    shutil.copytree(PROJECT_ROOT / "prompts", output / "prompts", dirs_exist_ok=True)
    print(f"master_characters={len(master)}")
    print(f"output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
