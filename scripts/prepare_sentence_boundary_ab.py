from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sentence_ranges(raw: dict) -> list[tuple[float, float, int]]:
    result = []
    number = 0
    for transcript in raw.get("transcripts", []):
        for sentence in transcript.get("sentences", []):
            result.append((
                float(sentence.get("begin_time", 0)) / 1000,
                float(sentence.get("end_time", 0)) / 1000,
                number,
            ))
            number += 1
    return result


def attach_sentence_metadata(alignment: dict, raw: dict) -> dict:
    units = alignment["units"]
    ranges = sentence_ranges(raw)
    memberships: dict[int, list[int]] = {number: [] for _, _, number in ranges}
    for position, unit in enumerate(units):
        midpoint = (float(unit["start"]) + float(unit["end"])) / 2
        candidates = [
            (max(0.0, min(float(unit["end"]), end) - max(float(unit["start"]), start)), number)
            for start, end, number in ranges
        ]
        overlap, number = max(candidates, default=(0.0, -1))
        if overlap <= 0:
            number = min(ranges, key=lambda row: abs(midpoint - (row[0] + row[1]) / 2))[2]
        memberships[number].append(position)
        unit["sentence_id"] = number
        unit["sentence_start"] = False
        unit["sentence_end"] = False
    for positions in memberships.values():
        if positions:
            units[positions[0]]["sentence_start"] = True
            units[positions[-1]]["sentence_end"] = True
    return alignment


def material(alignment: dict, master: str) -> str:
    rows = []
    for unit in alignment["units"]:
        text = str(unit["text"]).replace("\t", " ").replace("\n", " ")
        rows.append("\t".join([
            str(unit["index"]), f'{float(unit["start"]):.3f}',
            f'{float(unit["end"]):.3f}', text,
            str(unit.get("sentence_id", "-")),
            "1" if unit.get("sentence_start") else "0",
            "1" if unit.get("sentence_end") else "0",
            str(unit.get("speaker_id") or "-"),
            f'{float(unit.get("speaker_confidence", 0) or 0):.3f}',
        ]))
    return (
        "# Substar A/B material\n\n## MASTER_TRANSCRIPT\n\n```text\n"
        + master.strip() + "\n```\n\n## ALIGNMENT\n\n```tsv\n"
        + "\n".join(rows) + "\n```\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("projects_root", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    raw = read_json(source / "ingest_chunks" / "qwen_cloud_result.json")
    alignment = attach_sentence_metadata(read_json(source / "alignment.json"), raw)
    master = (source / "master_transcript.txt").read_text(encoding="utf-8")
    original_manifest = read_json(source / "run_manifest.json")
    original_status = read_json(source / "job_status.json")
    variants = [
        ("20260813_ab_a_sentence_reference", "AB-A｜Qwen句界·软参考", "reference"),
        ("20260813_ab_b_word_reconstruct", "AB-B｜词级时间·从零重建", "reconstruct"),
        ("20260813_ab_c_unpunctuated", "AB-C｜纯词级时间·无标点重建", "unpunctuated"),
    ]
    for project_id, display_name, policy in variants:
        target = (args.projects_root / project_id).resolve()
        target.mkdir(parents=True, exist_ok=True)
        (target / "input").mkdir(exist_ok=True)
        source_media = Path(original_manifest["source_path"])
        shutil.copy2(source_media, target / "input" / source_media.name)
        manifest = dict(original_manifest)
        manifest["source_path"] = str((target / "input" / source_media.name).resolve())
        manifest["sentence_boundary_policy"] = policy
        write_json(target / "run_manifest.json", manifest)
        write_json(target / "alignment.json", alignment)
        (target / "master_transcript.txt").write_text(master, encoding="utf-8")
        (target / "chatbox_material.md").write_text(material(alignment, master), encoding="utf-8")
        completed = (
            (target / "project_v2" / "manifest.json").is_file()
            and (target / "stage1_experiment" / "stage1_result.json").is_file()
        )
        status = {
            "id": project_id,
            "filename": source_media.name,
            "display_name": display_name,
            "workflow_mode": "sentence_boundary_ab",
            "settings_overrides": original_status.get("settings_overrides", {}),
            "status": "awaiting_edit" if completed else "running",
            "message": (
                "实验工程已生成，可以进入编辑模式"
                if completed else f"正在运行 {display_name}"
            ),
            "progress": 1.0 if completed else 0.1,
            "error": "",
            "files": [
                {"name": path.name, "size": path.stat().st_size}
                for path in sorted(target.iterdir()) if path.is_file()
            ],
            "attempt": 1,
        }
        write_json(target / "job_status.json", status)
        write_json(target / "ab_experiment.json", {
            "schema_version": "substar.sentence-boundary-ab.v1",
            "source_project_id": source.name,
            "policy": policy,
            "display_name": display_name,
            "native_sentence_count": len(sentence_ranges(raw)),
            "alignment_unit_count": len(alignment["units"]),
        })
        print(f"{project_id}\t{policy}\t{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
