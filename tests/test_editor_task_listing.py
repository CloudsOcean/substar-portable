from __future__ import annotations

from substar_core.editor import http_api
from substar_core.storage import ProjectIntegrityError


def test_editor_task_feed_isolates_a_corrupt_project(tmp_path, monkeypatch) -> None:
    project = tmp_path / "corrupt-project"
    (project / "project").mkdir(parents=True)
    (project / "project" / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(http_api, "_projects_root", lambda: tmp_path)
    monkeypatch.setattr(
        http_api,
        "load_translation_status",
        lambda _directory: (_ for _ in ()).throw(
            ProjectIntegrityError("revision document checksum mismatch")
        ),
    )

    assert http_api.list_editor_tasks() == {
        "schema_version": "substar.editor-task-list.v1",
        "tasks": [],
    }
