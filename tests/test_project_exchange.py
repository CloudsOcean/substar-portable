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
    _checkpoint_hash,
    _external_generation_source_records,
    apply_external_generation_checkpoint,
    apply_external_prooftranslation,
    apply_external_split,
    export_subtitle_project,
    external_prooftranslation_files,
    external_split_files,
    import_subtitle_project,
    inspect_external_generation_checkpoint,
    inspect_external_prooftranslation,
    inspect_external_split,
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


def _multi_token_document():
    units = [
        {"index": 0, "text": "Hello", "start": 0.0, "end": 0.5, "speaker_id": "A"},
        {"index": 1, "text": "world", "start": 0.6, "end": 1.0, "speaker_id": "A"},
        {"index": 2, "text": "again", "start": 1.1, "end": 1.6, "speaker_id": "A"},
    ]
    material, breaks, report = materialize_reference_script(
        "Hello world again.", units, "."
    )
    return build_reference_script_document(
        material, source_asset_id="exchange-multi", display_breaks=breaks,
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


def test_external_ai_prooftranslation_uses_one_routed_two_stage_prompt(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "project", project_id="exchange_test")
    revision = store.save(
        _document(),
        provenance=ChangeProvenance(kind=ChangeKind.IMPORT, operation="test", actor="test"),
    )
    files = external_prooftranslation_files(
        "exchange_test", revision, source_language="en", target_language="zh-CN"
    )
    assert set(files) == {"01_外部AI校译任务.json", "02_执行提示词.md"}
    task = json.loads(files["01_外部AI校译任务.json"].decode("utf-8-sig"))
    assert task["revision_id"] == revision.revision_id
    assert task["task"] == "staged_correction_translation"
    assert task["source_language"] == "en"
    assert task["target_language"] == "zh-CN"
    assert task["prompt_snapshot"]["stages"]["translation"]["variant"] == "en_to_zh"
    prompt = files["02_执行提示词.md"].decode("utf-8-sig")
    assert "校准稿未经用户明确接受，不得翻译" in prompt
    assert "编号|译文" in prompt
    assert "C###[-C###]" not in prompt
    assert "显示编号始终递增" in prompt
    assert "局部反序" in prompt
    assert "English to Simplified Chinese" in prompt



def test_external_ai_split_round_trip_changes_only_cue_topology(tmp_path: Path) -> None:
    document = _multi_token_document()
    store = ProjectStore.create(tmp_path / "split-project", project_id="split_project")
    revision = store.save(
        document,
        provenance=ChangeProvenance(kind=ChangeKind.IMPORT, operation="test", actor="test"),
    )
    files = external_split_files(
        "split_project", revision, source_language="en", source_hard_limit=20
    )
    assert set(files) == {"01_外部AI切分任务.json", "02_执行提示词.md"}
    task = json.loads(files["01_外部AI切分任务.json"].decode("utf-8-sig"))
    assert task["task"] == "external_segmentation"
    assert task["prompt_snapshot"]["stages"]["segmentation"]["variant"] == "en"
    assert "只能选择附件中真实存在" in files["02_执行提示词.md"].decode("utf-8-sig")

    boundary_id = task["tokens"][0]["token_id"]
    payload = {
        **task["return_schema"],
        "boundaries": [{"after_token_id": boundary_id, "reason": "complete phrase"}],
    }
    inspection = inspect_external_split(
        document,
        payload,
        revision_id=revision.revision_id,
        document_hash=revision.document_hash,
        source_hard_limit=20,
    )
    assert inspection["summary"]["proposed_cues"] == 2
    updated = apply_external_split(document, inspection)
    assert len(updated.cues) == 2
    assert tuple(token.to_dict() for token in updated.source_tokens) == tuple(
        token.to_dict() for token in document.source_tokens
    )
    assert tuple(token.to_dict() for token in updated.display_tokens) == tuple(
        token.to_dict() for token in document.display_tokens
    )
    assert updated.changes[-1].operation == "external_ai_split"
    committed = store.save(
        updated,
        provenance=ChangeProvenance(
            kind=ChangeKind.IMPORT, operation="external_ai_split", actor="external-ai"
        ),
        expected_revision_id=revision.revision_id,
    )
    assert len(committed.document.cues) == 2


def test_external_ai_split_rejects_stale_or_invalid_boundaries(tmp_path: Path) -> None:
    document = _multi_token_document()
    store = ProjectStore.create(tmp_path / "split-project", project_id="split_project")
    revision = store.save(
        document,
        provenance=ChangeProvenance(kind=ChangeKind.IMPORT, operation="test", actor="test"),
    )
    files = external_split_files(
        "split_project", revision, source_language="en", source_hard_limit=20
    )
    task = json.loads(files["01_外部AI切分任务.json"].decode("utf-8-sig"))
    payload = {**task["return_schema"], "boundaries": [{"after_token_id": "unknown"}]}
    with pytest.raises(ProjectExchangeError, match="无效边界"):
        inspect_external_split(
            document, payload, revision_id=revision.revision_id,
            document_hash=revision.document_hash, source_hard_limit=20,
        )
    payload = {**task["return_schema"], "revision_id": "stale", "boundaries": []}
    with pytest.raises(ProjectExchangeError, match="项目版本已经变化"):
        inspect_external_split(
            document, payload, revision_id=revision.revision_id,
            document_hash=revision.document_hash, source_hard_limit=20,
        )


def test_external_ai_public_routes_use_prooftranslation_and_split_names() -> None:
    routes = {
        (route.path, method)
        for route in http_api.router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/api/projects/{project_id}/exchange/external-ai-prooftranslation", "GET") in routes
    assert ("/api/projects/{project_id}/external-ai-prooftranslation", "POST") in routes
    assert ("/api/projects/{project_id}/exchange/external-ai-split", "GET") in routes
    assert ("/api/projects/{project_id}/external-ai-split", "POST") in routes
    assert not any("external-ai-generate" in path or "external-ai-fix" in path for path, _ in routes)


def test_external_ai_split_never_rewrites_existing_translation() -> None:
    document = _multi_token_document()
    cue = document.cues[0]
    translated = apply_document_operation(document, {
        "operation_id": "translated",
        "type": "set_target",
        "payload": {"cue_id": cue.cue_id, "target_text": "已有译文", "language": "zh-CN"},
    })
    revision = type("Revision", (), {
        "document": translated,
        "revision_id": "rev",
        "document_hash": translated.content_hash(),
    })()
    with pytest.raises(ProjectExchangeError, match="不能丢弃或程序化重写既有译文"):
        external_split_files(
            "project", revision, source_language="en", source_hard_limit=20
        )


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


def test_external_ai_prooftranslation_preserves_asr_evidence_and_applies_matching_units() -> None:
    document = _document()
    cue = document.cues[0]
    token = next(item for item in document.display_tokens if item.token_id == cue.display_token_ids[0])
    source_before = tuple(item.to_dict() for item in document.source_tokens)
    exported = external_prooftranslation_files(
        "project", type("Revision", (), {
            "document": document, "revision_id": "rev", "document_hash": document.content_hash()
        })(), source_language="en", target_language="zh-CN"
    )
    task = json.loads(exported["01_外部AI校译任务.json"].decode("utf-8-sig"))
    source_hash = task["items"][0]["source_hash"]
    payload = {
        "schema_version": "substar.external-ai-prooftranslation.v1",
        "task": "staged_correction_translation",
        "revision_id": "rev",
        "source_language": "en",
        "target_language": "zh-CN",
        "source_results": [{
            "cue_id": cue.cue_id,
            "source_hash": source_hash,
            "tokens": [{"token_id": token.token_id, "text": token.text.upper()}],
        }],
        "translation_results": [{
            "cue_id": cue.cue_id,
            "source_hash": source_hash,
            "target_text": "你好。",
            "language": "zh-CN",
        }],
    }
    inspection = inspect_external_prooftranslation(document, payload)
    assert len(inspection["source"]["applicable"]) == 1
    assert len(inspection["translation"]["applicable"]) == 1
    updated = apply_external_prooftranslation(document, inspection)
    updated_token = next(item for item in updated.display_tokens if item.token_id == token.token_id)
    assert updated_token.text == token.text.upper()
    assert updated_token.provenance.operation == "external_ai_prooftranslation"
    assert updated.cues[0].target.target_text == "你好。"
    assert tuple(item.to_dict() for item in updated.source_tokens) == source_before


def test_external_ai_combined_result_keeps_independent_valid_track() -> None:
    document = _document()
    cue = document.cues[0]
    exported = external_prooftranslation_files(
        "project", type("Revision", (), {
            "document": document, "revision_id": "rev", "document_hash": document.content_hash()
        })(), source_language="Auto", target_language="auto_opposite"
    )
    task = json.loads(exported["01_外部AI校译任务.json"].decode("utf-8-sig"))
    assert task["source_language"] == "en"
    assert task["target_language"] == "zh-CN"
    source_hash = task["items"][0]["source_hash"]
    payload = {
        "schema_version": "substar.external-ai-prooftranslation.v1",
        "task": "staged_correction_translation",
        "revision_id": "rev",
        "source_language": "en",
        "target_language": "zh-CN",
        "source_results": [{
            "cue_id": cue.cue_id, "source_hash": source_hash,
            "tokens": [{"token_id": "wrong-token", "text": "Wrong"}],
        }],
        "translation_results": [{
            "cue_id": cue.cue_id, "source_hash": source_hash,
            "target_text": "你好。", "language": "zh-CN",
        }],
    }
    inspection = inspect_external_prooftranslation(document, payload)
    assert len(inspection["source"]["invalid"]) == 1
    assert len(inspection["translation"]["applicable"]) == 1
    updated = apply_external_prooftranslation(document, inspection)
    assert updated.cues[0].target.target_text == "你好。"


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


def test_external_translation_uses_production_prompt_and_original_times():
    from types import SimpleNamespace
    from substar_core.project_exchange import external_translation_text
    from substar_core.prompt_registry import render_prompt
    from substar_core.export import render_document_srt
    document = _document()
    for mode in ('many_to_many', 'one_to_one'):
        text = external_translation_text(SimpleNamespace(document=document), source_language='en',
            target_language='zh-CN', target_hard_limit=20, mapping_mode=mode)
        assert render_prompt('contextual_translation', variant='en_to_zh', mode=mode).text in text
        assert render_document_srt(document, 'source') in text
        assert 'TARGET_LIMIT：20' in text
        assert '只输出译文 SRT' in text
        assert '[OMIT]' in text


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
