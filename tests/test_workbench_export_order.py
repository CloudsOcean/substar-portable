from pathlib import Path
from types import SimpleNamespace

import app


class _FakeStore:
    def __init__(self, display_order: str | None) -> None:
        self.display_order = display_order

    def load_latest(self):
        if self.display_order is None:
            return None
        return SimpleNamespace(
            document=SimpleNamespace(
                presentation=SimpleNamespace(
                    display_order=SimpleNamespace(value=self.display_order)
                )
            )
        )


def test_workbench_export_reads_source_above_target_from_project(monkeypatch) -> None:
    monkeypatch.setattr(app.ProjectStore, "open", lambda _path: _FakeStore("source_above_target"))
    assert app._translation_top_line_role(Path("unused")) == "source"


def test_workbench_export_reads_target_above_source_from_project(monkeypatch) -> None:
    monkeypatch.setattr(app.ProjectStore, "open", lambda _path: _FakeStore("target_above_source"))
    assert app._translation_top_line_role(Path("unused")) == "target"


def test_workbench_export_defaults_to_source_without_a_revision(monkeypatch) -> None:
    monkeypatch.setattr(app.ProjectStore, "open", lambda _path: _FakeStore(None))
    assert app._translation_top_line_role(Path("unused")) == "source"
