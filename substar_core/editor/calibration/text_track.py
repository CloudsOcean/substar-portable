"""Revision-bound calibration for subtitle tracks stored as text."""
from __future__ import annotations

from dataclasses import replace
import re

from substar_core.ai_progress import ai_progress
from substar_core.artifacts import atomic_write_json
from substar_core.cue_script import output_contract, parse_cue_text, render_cue_request
from substar_core.domain import ChangeKind, ChangeProvenance, EntityState
from substar_core.document_operations import apply_document_operation
from substar_core.editor.application.publication import write_candidate
from substar_core.prompt_registry import frozen_prompt
from substar_core.translation_units import all_units, install_units

from .service import _run_editor_ai_blocks


def target_calibration_rows(document):
    active = {cue.cue_id for cue in document.cues if cue.state is EntityState.ACTIVE}
    units = all_units(document)
    rows = []
    for unit in units:
        evidence = [cue_id for cue_id in unit["source_cue_ids"] if cue_id in active]
        if evidence and unit["text"].strip():
            rows.append({
                "cue_id": unit["unit_id"], "source_text": unit["text"],
                "start": unit["start"], "language": unit.get("language") or "Auto",
                "evidence": evidence, "independent": True,
            })
    for cue in document.cues:
        if (
            cue.cue_id in active and cue.target and cue.target.editable
            and cue.target.target_text.strip() and not cue.mapping.get("target_unit_ids")
        ):
            rows.append({
                "cue_id": cue.cue_id, "source_text": cue.target.target_text,
                "start": cue.start, "language": cue.target.language or "Auto",
                "evidence": [cue.cue_id], "independent": False,
            })
    return sorted(rows, key=lambda row: (row["start"], row["cue_id"]))


def source_calibration_rows(document, language):
    return [
        {
            "cue_id": cue.cue_id, "source_text": cue.source_text,
            "start": cue.start, "language": language,
            "evidence": [cue.cue_id],
        }
        for cue in document.cues
        if cue.state is EntityState.ACTIVE and cue.source_text and cue.source_text.strip()
        and cue.mapping.get("subtitle_track") != "overlay"
    ]


def _text_blocks(rows):
    blocks = {}
    block = []
    for row in rows:
        if not block or len(block) >= 48 or (
            row["start"] - block[0]["start"] >= 90 or row["language"] != block[0]["language"]
        ):
            block = []
            blocks[f"text_{len(blocks) + 1:04d}"] = block
        block.append(row)
    return blocks


_LINE_BREAK = "␤"
_NUMERIC_TEXT = re.compile(r"[+−-]?\d+(?:[.,:/]\d+)*")


def _text_wire(cues, repair_feedback, track):
    wire_rows = []
    for cue in cues:
        original = cue["source_text"]
        marker = _LINE_BREAK
        while marker in original:
            marker += _LINE_BREAK
        wire_rows.append({
            **cue,
            "line_break": marker,
            "source_text": original.replace("\n", marker).replace("\t", " ").strip(),
        })
    wire, ledger = render_cue_request(wire_rows, task="CALIBRATE", instructions="", repair_feedback=repair_feedback)
    return f"TRACK\t{track.upper()}\nLANGUAGE\t{cues[0]['language']}\n{wire}", ledger


def _text_result(raw, ledger):
    corrected = parse_cue_text(raw, "CALIBRATE", ledger)
    changes = {}
    for alias, text in corrected.items():
        row = ledger.cues[alias]
        original = row["source_text"]
        if text == original:
            continue
        marker = row["line_break"]
        if text.count(marker) != original.count(marker):
            raise ValueError(f"{alias} 校准必须保留字幕内部的换行标记")
        if _NUMERIC_TEXT.findall(original) != _NUMERIC_TEXT.findall(text):
            raise ValueError(f"{alias} 校准不得改动数字")
        if len(text) > min(20000, max(len(original) + 32, len(original) * 2)):
            raise ValueError(f"{alias} 校准文本长度超出范围")
        changes[row["cue_id"]] = text.replace(marker, "\n")
    return {"changes": changes}


def _apply_target_text(document, rows_by_id, changes, provenance):
    units = all_units(document)
    updated_units = False
    for unit in units:
        unit_id = unit["unit_id"]
        if unit_id in changes:
            unit.update(text=changes[unit_id], provenance=provenance.to_dict())
            updated_units = True
    if updated_units:
        document = install_units(document, units, provenance)
    legacy = {key: text for key, text in changes.items() if not rows_by_id[key]["independent"]}
    cues = tuple(
        replace(cue, target=replace(cue.target,
            target_text=legacy[cue.cue_id],
            original_text=cue.target.original_text or cue.target.target_text,
            provenance=provenance,
        )) if cue.cue_id in legacy else cue
        for cue in document.cues
    )
    return replace(document, cues=cues, changes=(*document.changes, provenance))


def compute_text_calibration(
    latest, payload, task_id, *, project_id, project_root, artifact_directory, settings, progress_sink,
):
    track = payload.track
    rows = (target_calibration_rows(latest.document) if track == "target" else
            source_calibration_rows(latest.document, settings.get("language") or "Auto"))
    if not rows:
        raise ValueError("当前轨道没有可校准的字幕")
    rows_by_id = {row["cue_id"]: row for row in rows}
    blocks = _text_blocks(rows)
    track_label = "译文" if track == "target" else "原文"
    suffix = (
        f"\n\n本次处理{track_label}轨，按请求所列语言修订。不要重译。"
        f"保留每条内已有的 {_LINE_BREAK} 换行标记，位置尽量与原文一致。"
    )
    if payload.instruction.strip():
        suffix += "\n用户补充要求：" + payload.instruction.strip()
    suffix += "\n\n" + output_contract("CALIBRATE")
    primary = frozen_prompt(settings, "calibration", variant="generic").text + suffix
    repair = frozen_prompt(settings, "calibration_repair", variant="generic").text + suffix
    primary_done = repair_planned = repair_done = repair_accepted = 0

    def report(phase, failed=0):
        progress = ai_progress(
            kind="calibration", phase=phase, unit_label="块",
            planned=len(blocks), completed=primary_done,
            accepted=max(0, primary_done - repair_planned) + repair_accepted,
            failed=failed, repair_planned=repair_planned, repair_completed=repair_done,
            repair_accepted=repair_accepted, repair_failed=failed if phase == "completed" else 0,
            problem_count=failed,
        )
        if progress_sink:
            progress_sink(progress)
        return progress

    def primary_progress(done, total):
        nonlocal primary_done
        primary_done = done
        report("executing")

    def repair_progress(phase, done, planned, accepted):
        nonlocal repair_planned, repair_done, repair_accepted
        repair_planned, repair_done, repair_accepted = planned, done, accepted
        if planned:
            report(phase)

    report("executing")
    results = _run_editor_ai_blocks(
        settings=settings, system_prompt=primary, repair_system_prompt=repair, blocks=blocks,
        failure_key="changes", stage_name="calibration", retry_stage="audit_repair",
        request_renderer=lambda cues, feedback: _text_wire(cues, feedback, track),
        response_finalizer=_text_result,
        progress_callback=primary_progress, phase_callback=repair_progress,
        cache_directory=project_root / "calibration" / "block_cache",
        cache_scope=f"calibration-{track}-text-v1", wire_protocol="substar-calibration-lines.v2-numeric-pipe",
    )
    failed = [key for key, _, metadata in results if metadata.get("error")]
    changes = {
        key: text
        for _, result, metadata in results if not metadata.get("error")
        for key, text in result["changes"].items()
    }
    problem_cues = list(dict.fromkeys(
        evidence for key in failed for row in blocks[key] for evidence in row["evidence"]
    ))
    audit = {
        "schema_version": "substar.calibration-audit.v2", "project_id": project_id,
        "task_id": task_id, "based_on_revision_id": latest.revision_id, "track": track,
        "blocks": [
            {"block_id": key, "raw_response": result, "request_metadata": metadata}
            for key, result, metadata in results
        ],
    }
    if len(failed) == len(blocks):
        atomic_write_json(artifact_directory / "audit.json", audit)
        raise RuntimeError(f"{track_label}校准所有执行块均失败：{results[0][2]['error']}")
    report("materializing")
    provenance = ChangeProvenance(
        kind=ChangeKind.AI, operation="ai_calibration_apply", actor="ai-calibration",
        metadata={"task_id": task_id, "track": track, "replacement_count": len(changes)},
    )
    document = latest.document
    if track == "target":
        document = _apply_target_text(document, rows_by_id, changes, provenance)
    else:
        for cue_id, text in changes.items():
            document = apply_document_operation(document, {
                "operation_id": f"{task_id}:{cue_id}", "type": "set_source_text",
                "payload": {"cue_id": cue_id, "text": text,
                            "expected_source_text": rows_by_id[cue_id]["source_text"],
                            "provenance": provenance.to_dict()},
            })
    revision = write_candidate(artifact_directory, latest, document, provenance).to_dict()
    audit["result_revision_id"] = revision["revision_id"]
    atomic_write_json(artifact_directory / "audit.json", audit)
    atomic_write_json(artifact_directory / "latest.json", {
        "schema_version": "substar.calibration-result.v2", "project_id": project_id,
        "task_id": task_id, "based_on_revision_id": latest.revision_id,
        "track": track, "actions": [],
        "text_edits": [
            {"id": key, "before": rows_by_id[key]["source_text"], "after": text}
            for key, text in changes.items()
        ],
    })
    return {
        "revision": revision, "failed_blocks": failed, "problem_block_ids": failed,
        "problem_cue_ids": problem_cues, "ai_progress": report("completed", len(failed)),
    }
