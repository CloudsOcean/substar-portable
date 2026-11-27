from __future__ import annotations

import json
import asyncio
from io import BytesIO
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from fastapi import UploadFile

from substar_core.document_operations import DocumentOperationError, apply_document_operation
from substar_core.manuscript_matching import materialize_reference_script
from substar_core.project_exchange import (
    ProjectExchangeError,
    export_subtitle_project,
    import_subtitle_project,
)
from substar_core.segmentation.document_builder import build_reference_script_document
from substar_core.storage import ProjectStore
from substar_core.domain import ChangeKind, ChangeProvenance
from substar_core.editor import http_api


def _document():
    units = [{"index": 0, "text": "Hello", "start": 0.0, "end": 1.0, "speaker_id": None}]
    material, breaks, report = materialize_reference_script("Hello.", units, ".")
    return build_reference_script_document(
        material, source_asset_id="exchange-fixture", display_breaks=breaks,
        reference_report=report,
    )


def test_manual_cue_rejects_overlap_but_accepts_tiny_gap() -> None:
    document = _document()
    with pytest.raises(DocumentOperationError, match="overlaps existing cue"):
        apply_document_operation(document, {
            "operation_id": "overlap", "type": "insert_cue",
            "payload": {"start": .5, "end": .6, "text": "bad"},
        })
    inserted = apply_document_operation(document, {
        "operation_id": "tiny", "type": "insert_cue",
        "payload": {"start": 1.001, "end": 1.002, "text": "ok"},
    })
    assert any(cue.end - cue.start < .04 for cue in inserted.cues)


def test_subtitle_project_round_trip_registers_one_new_project(tmp_path: Path) -> None:
    source_job = tmp_path / "source"
    (source_job / "input").mkdir(parents=True)
    (source_job / "input" / "clip.mp3").write_bytes(b"media")
    (source_job / "audio_16k_mono.wav").write_bytes(b"RIFF-audio")
    (source_job / "task_info.json").write_text(
        json.dumps({
            "schema_version": "substar.task-info.v1",
            "project_id": "source_project",
            "display_name": "Clip task",
            "language": "en",
            "target_language_mode": "zh-CN",
            "source_hard_limit": 44,
            "target_hard_limit": 20,
            "updated_at": "2026-08-23T00:00:00+00:00",
        }),
        encoding="utf-8",
    )
    store = ProjectStore.create(source_job / "project", project_id="source_project")
    revision = store.save(
        _document(),
        provenance=ChangeProvenance(kind=ChangeKind.IMPORT, operation="test", actor="test"),
    )
    package = tmp_path / "project.zip"
    export_subtitle_project(
        package, project_id="source_project", job_dir=source_job, revision=revision
    )
    destination = tmp_path / "destination"
    with package.open("rb") as handle:
        imported_id = import_subtitle_project(handle, projects_root=destination)
    imported = destination / imported_id
    assert ProjectStore.open(imported / "project").load_latest().document.content_hash() == revision.document.content_hash()
    state = json.loads((imported / "creation_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "awaiting_edit"
    assert (imported / "input" / "clip.mp3").read_bytes() == b"media"
    task_info = json.loads((imported / "task_info.json").read_text(encoding="utf-8"))
    assert task_info["source_hard_limit"] == 44
    assert task_info["display_name"] == "Clip task"
    assert json.loads((imported / "project_creation.json").read_text(encoding="utf-8"))["input_mode"] == "subtitle_project_import"
    import app
    from unittest.mock import patch
    # Both UI import entries share this importer. Registration must survive restart.
    with patch.object(app, "_relay_output_root", return_value=destination), patch.object(app, "JOBS", {}):
        app._restore_persisted_jobs(imported_id)
        assert app.JOBS[imported_id].status == "awaiting_edit"
        # Previously imported packages did not carry a creation recipe.
        (imported / "project_creation.json").unlink()
        app.JOBS.clear()
        # Real packages with usable media enter tutorial registration here too.
        with patch.object(app, "_editor_ready", return_value=True):
            app._restore_persisted_jobs(imported_id)
        assert imported_id in app.JOBS
        result = app.delete_workbench_split_job(imported_id)
        assert result["deleted"] == imported_id
        app._restore_persisted_jobs()
        assert imported_id not in app.JOBS
        assert not imported.exists()


def test_subtitle_project_rejects_path_traversal(tmp_path: Path) -> None:
    package = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../escape", b"bad")
        archive.writestr("manifest.json", b"{}")
    with package.open("rb") as handle, pytest.raises(ProjectExchangeError, match="不安全路径"):
        import_subtitle_project(handle, projects_root=tmp_path / "destination")


def test_project_task_info_is_one_project_scoped_authority(tmp_path: Path, monkeypatch) -> None:
    project_id = "line_limit_project"
    ProjectStore.create(tmp_path / project_id / "project", project_id=project_id)
    monkeypatch.setattr(http_api, "_projects_root", lambda: tmp_path)
    result = http_api.set_project_task_info(
        project_id,
        http_api.ProjectTaskInfoRequest(
            display_name="Renamed task", language="en", target_language_mode="zh-CN",
            source_hard_limit=48, target_hard_limit=22
        ),
    )
    assert result["display_name"] == "Renamed task"
    assert result["source_hard_limit"] == 48
    assert result["target_hard_limit"] == 22
    assert http_api.get_project_task_info(project_id)["source_hard_limit"] == 48
    assert not (tmp_path / project_id / "editor_preferences.json").exists()


def test_model_omission_is_accepted_without_changing_source_or_exporting_marker():
    from substar_core.cue_script import render_translation_request, finalize_translation
    from substar_core.editor.translation.contextual import complete_results, materialize_presentation
    from substar_core.export import render_document_srt
    document = _document()
    cue = document.cues[0]
    groups = [{'group_id':'g1', 'cues':[{'cue_id':cue.cue_id,'source_text':'um','hard_limit':20}]}]
    _, ledger = render_translation_request(groups, mapping_mode='one_to_one')
    response = finalize_translation('1|[OMIT]', groups, ledger, mapping_mode='one_to_one')
    assert not response['_cue_script_issues']
    plans, report = complete_results(settings={}, repair_prompt='', groups=groups,
        response=response, mapping_mode='one_to_one')
    assert not report['invalid_group_ids']
    candidate, report = materialize_presentation(document, plans, 'zh-CN')
    active = [c for c in candidate.cues if c.state.value == 'active']
    assert len(active) == 1
    assert active[0].target.target_text == ''
    assert active[0].target.provenance.metadata['omitted_filler'] is True
    assert active[0].target.translation_status == 'translated'
    assert (active[0].start,active[0].end,active[0].display_token_ids) == (cue.start,cue.end,cue.display_token_ids)
    assert candidate.source_tokens == document.source_tokens
    assert render_document_srt(candidate,'target') == ''
    assert '[OMIT]' not in render_document_srt(candidate,'ab-double')

def test_retired_external_generation_routes_are_absent():
    from app import app

    paths = app.openapi()["paths"]
    retired = (
        "external-ai-prooftranslation", "external-ai-split", "external-ai-edit",
        "external-ai-generation", "external-calibration", "external-translation",
        "import-srt-translation",
    )
    assert not any(path.endswith(retired) for path in paths)
    assert "/api/project-imports/subtitle-project" in paths
    assert "/api/projects/{project_id}/exchange/subtitle-project" in paths
