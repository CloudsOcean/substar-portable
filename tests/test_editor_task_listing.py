from __future__ import annotations

from types import SimpleNamespace

from substar_core.editor import http_api


class _TaskService:
    def list_tasks(self, **_kwargs):
        return [{
            "schema_version": "substar.task.v2",
            "task_id": "tsk_" + "a" * 32,
            "task_type": "translation",
            "project_id": "corrupt-project",
            "state": "running",
            "progress_message": "翻译处理中",
            "created_at": "2026-08-31T00:00:00Z",
            "updated_at": "2026-08-31T00:00:01Z",
        }]


def test_editor_task_feed_uses_runtime_when_project_metadata_is_unreadable(monkeypatch) -> None:
    monkeypatch.setattr(
        http_api,
        "load_task_info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid v1 project")),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(task_service=_TaskService()))
    )

    value = http_api.list_editor_tasks(request)

    assert value["schema_version"] == "substar.editor-task-list.v2"
    assert value["tasks"][0]["display_name"] == "corrupt-project"
    assert value["tasks"][0]["status"] == "running"
