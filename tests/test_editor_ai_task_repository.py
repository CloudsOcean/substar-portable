from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from substar_core.editor.tasks.contracts import EditorAiTaskKind, EditorAiTaskState
from substar_core.editor.tasks.repository import (
    EditorAiTaskCancelled,
    EditorAiTaskConflict,
    assert_editor_write_allowed,
    finish_task,
    load_task,
    raise_if_task_cancelled,
    request_task_cancellation,
    start_task,
    task_context,
)


class EditorAiTaskRepositoryTests(unittest.TestCase):
    def test_active_task_exclusively_locks_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = start_task(
                root,
                project_id="project-1",
                kind=EditorAiTaskKind.CALIBRATION,
                based_on_revision_id="revision-1",
            )
            with self.assertRaises(EditorAiTaskConflict):
                start_task(
                    root,
                    project_id="project-1",
                    kind=EditorAiTaskKind.REVIEW,
                    based_on_revision_id="revision-1",
                )
            with self.assertRaises(EditorAiTaskConflict):
                assert_editor_write_allowed(root)
            with task_context(task["task_id"]):
                assert_editor_write_allowed(root)

    def test_terminal_task_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = start_task(
                root,
                project_id="project-1",
                kind=EditorAiTaskKind.TRANSLATION,
                based_on_revision_id="revision-1",
            )
            finished = finish_task(
                root,
                task["task_id"],
                EditorAiTaskState.SUCCEEDED,
                result_revision_id="revision-2",
            )
            self.assertFalse(finished["locks_editor"])
            assert_editor_write_allowed(root)
            self.assertEqual(load_task(root)["result_revision_id"], "revision-2")

    def test_cancellation_signal_is_visible_to_owned_provider_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = start_task(
                root,
                project_id="project-1",
                kind=EditorAiTaskKind.CALIBRATION,
                based_on_revision_id="revision-1",
            )
            cancelling = request_task_cancellation(root)
            self.assertEqual(cancelling["state"], "cancelling")
            self.assertTrue(cancelling["locks_editor"])
            self.assertIsNotNone(cancelling["cancel_requested_at"])
            with task_context(task["task_id"]):
                with self.assertRaises(EditorAiTaskCancelled):
                    raise_if_task_cancelled()
            cancelled = finish_task(
                root, task["task_id"], EditorAiTaskState.CANCELLED
            )
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertFalse(cancelled["locks_editor"])


if __name__ == "__main__":
    unittest.main()
