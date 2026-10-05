import pytest
from substar_core.task_info import load_task_info, save_task_info

BASE = {"display_name": "test", "language": "en", "target_language_mode": "zh-CN"}

def test_defaults_and_project_persistence(tmp_path):
    info = save_task_info(tmp_path, "p", BASE)
    assert (info["autosave_delay_seconds"], info["autosave_retry_count"]) == (1, 3)
    save_task_info(tmp_path, "p", {**info, "autosave_delay_seconds": 7, "autosave_retry_count": 0})
    loaded = load_task_info(tmp_path, "p")
    assert (loaded["autosave_delay_seconds"], loaded["autosave_retry_count"]) == (7, 0)

@pytest.mark.parametrize("key,value", [("autosave_delay_seconds", -1), ("autosave_delay_seconds", 31), ("autosave_delay_seconds", 1.5), ("autosave_retry_count", 6), ("autosave_retry_count", True)])
def test_invalid_settings_rejected(tmp_path, key, value):
    with pytest.raises(ValueError):
        save_task_info(tmp_path, "p", {**BASE, key: value})

def test_older_client_update_preserves_autosave_settings(tmp_path, monkeypatch):
    from substar_core.editor import http_api
    from substar_core.storage.project_store import ProjectStore
    project_id = "autosave-project"
    job_dir = tmp_path / project_id
    ProjectStore.create(job_dir / "project", project_id=project_id)
    save_task_info(job_dir, project_id, {**BASE, "autosave_delay_seconds": 9, "autosave_retry_count": 1})
    monkeypatch.setattr(http_api, "_projects_root", lambda: tmp_path)
    updated = http_api.set_project_task_info(project_id, http_api.ProjectTaskInfoRequest(
        **BASE, source_hard_limit=25, target_hard_limit=25))
    assert (updated["autosave_delay_seconds"], updated["autosave_retry_count"]) == (9, 1)
