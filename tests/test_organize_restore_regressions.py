from dataclasses import replace
from pathlib import Path

import pytest

from test_subtitle_text_tasks import document
from substar_core.domain import ChangeProvenance, ChangeKind
from substar_core.domain.editor_document import SemanticGroup
from substar_core.editor.text_task_runtime import build_text_task_handler
from substar_core.runtime.worker_protocol import WorkerMessage, WorkerMessageType, utc_now
from substar_core.storage import ProjectStore
from substar_core.storage.project_store import _document_patch, _apply_document_patch


@pytest.mark.parametrize("kind", ["organize", "punctuation"])
@pytest.mark.parametrize("phase", ["planning", "executing", "validating", "materializing"])
def test_text_progress_satisfies_scheduler_contract(kind, phase):
    handler = build_text_task_handler(kind, Path('.'), Path('.'))
    message = WorkerMessage(task_id="test", attempt=1, sequence=1,
        message_type=WorkerMessageType.PROGRESS, occurred_at=utc_now(),
        data={"phase": phase, "completed": 1, "total": 4}, progress=.25, step=kind)
    result = handler.handle_worker_event(None, message)
    assert {"progress", "message", "step", "wait_reason"} <= result.keys()
    assert result["wait_reason"] is None
    assert result["phase"] == ("delivery" if phase == "materializing" else "primary")


def test_group_reorder_persists_across_reopen_and_history(tmp_path):
    original = grouped_document()
    assert len(original.groups) >= 2
    store = ProjectStore.create(tmp_path / "project", project_id="test")
    provenance = ChangeProvenance(kind=ChangeKind.MANUAL, operation="group_reorder_test")
    versions = [store.save(original, provenance=provenance)]
    current = original
    for _ in range(4):
        current = replace(current, groups=tuple(reversed(current.groups)))
        versions.append(store.save(current, provenance=provenance,
            expected_revision_id=versions[-1].revision_id))
    ProjectStore.clear_memory_cache(store.root)
    reopened = ProjectStore.open(store.root)
    for expected in versions:
        actual = reopened.load_revision(expected.revision_id)
        assert actual.document == expected.document
        assert actual.document.content_hash() == expected.document_hash
    assert reopened.load_latest().document == current


def test_old_group_patch_without_order_splice_is_readable():
    before = grouped_document()
    after = replace(before, groups=tuple(replace(g, dirty_flags=("test",)) for g in before.groups))
    patch = _document_patch(before, after)
    patch["groups"].pop("order_splice")
    assert _apply_document_patch(before.to_dict(), patch)["groups"] == after.to_dict()["groups"]


def grouped_document():
    base = document()
    groups = tuple(SemanticGroup(group_id=f"g{i}", origin="manual", provenance=ChangeProvenance(kind=ChangeKind.MANUAL, operation="test")) for i in range(3))
    return replace(base, groups=groups, cues=tuple(replace(c, group_id=groups[i].group_id) for i,c in enumerate(base.cues)))
