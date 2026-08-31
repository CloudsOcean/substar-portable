from __future__ import annotations

import json
from pathlib import Path

from substar_core.editor import http_api
from substar_core.storage import ProjectStore
from substar_core.task_info import load_task_info


def test_advanced_tutorial_commits_packaged_stages_without_provider_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(http_api, "_projects_root", lambda: tmp_path)
    monkeypatch.setattr(
        http_api, "call_translation_model",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("provider must not run")),
    )

    launch = http_api.launch_tutorial_example("advanced-ai-v1")
    assert launch["project_id"] == "tutorial_advanced_ai_v1"
    assert launch["simulated"] is True
    project_id = launch["project_id"]
    store = ProjectStore.open(tmp_path / project_id / "project")
    baseline = store.load_latest()
    assert baseline is not None
    assert len(baseline.document.cues) == 34
    assert all(cue.target is None for cue in baseline.document.cues)
    task_info = load_task_info(tmp_path / project_id, project_id)
    assert task_info["source_hard_limit"] == 55
    assert task_info["target_hard_limit"] == 25
    assert (tmp_path / project_id / "audio_16k_mono.wav").is_file()
    creation_path = tmp_path / project_id / "project_creation.json"
    creation = json.loads(creation_path.read_text(encoding="utf-8"))
    assert creation["schema_version"] == "substar.project-creation.v2"
    assert creation["tutorial_case_id"] == "advanced-ai-v1"
    assert http_api.get_project_waveform(project_id, start=None, end=None, points=128)["peaks"]

    calibrated = http_api.apply_tutorial_stage(
        project_id, "calibration",
        http_api.TutorialStageRequest(expected_revision_id=baseline.revision_id),
    )
    calibrated_revision = store.load_revision(calibrated["revision_id"])
    assert calibrated["simulated"] is True
    assert any(
        token.provenance.operation == "ai_calibration_apply"
        for token in calibrated_revision.document.display_tokens
    )

    translated = http_api.apply_tutorial_stage(
        project_id, "translation",
        http_api.TutorialStageRequest(expected_revision_id=calibrated["revision_id"]),
    )
    translated_revision = store.load_revision(translated["revision_id"])
    assert all(cue.target is not None for cue in translated_revision.document.cues)
    assert any(cue.mapping.get("mapping_type") == "N:M" for cue in translated_revision.document.cues)

    reset = http_api.reset_tutorial_project(project_id)
    reset_revision = store.load_revision(reset["revision_id"])
    assert all(cue.target is None for cue in reset_revision.document.cues)
    listed = http_api.list_projects()["projects"]
    assert listed[0]["display_name"] == "进阶教程"
    assert listed[0]["tutorial_case_id"] == "advanced-ai-v1"

    # Relaunching an existing fixed tutorial also reconciles stale metadata.
    task_info_path = tmp_path / project_id / "task_info.json"
    task_info_path.write_text(
        task_info_path.read_text(encoding="utf-8").replace(
            '"target_hard_limit": 25', '"target_hard_limit": 16'
        ),
        encoding="utf-8",
    )
    http_api.launch_tutorial_example("advanced-ai-v1")
    assert load_task_info(tmp_path / project_id, project_id)["target_hard_limit"] == 25
    (tmp_path / project_id / "audio_16k_mono.wav").unlink()
    http_api.launch_tutorial_example("advanced-ai-v1")
    assert (tmp_path / project_id / "audio_16k_mono.wav").is_file()
    assert json.loads(creation_path.read_text(encoding="utf-8"))["schema_version"] == (
        "substar.project-creation.v2"
    )


def test_packaged_tutorial_examples_are_independent_of_user_data():
    root = Path(__file__).resolve().parents[1] / "assets" / "examples" / "tutorials"
    assert (root / "beginner" / "manifest.json").is_file()
    assert (root / "advanced-ai" / "manifest.json").is_file()
    assert not any("data" in path.parts for path in root.rglob("*"))
