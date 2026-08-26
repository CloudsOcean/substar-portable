from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from substar_core.editor.tasks.contracts import EditorAiTaskKind
from substar_core.editor.tasks.repository import (
    EditorAiTaskCancelled,
    request_task_cancellation,
    start_task,
    task_context,
)
from substar_core.stage2 import _cancellable_editor_post


class Stage2EditorCancellationTests(unittest.TestCase):
    def test_inflight_provider_process_is_terminated_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = start_task(
                root,
                project_id="project-1",
                kind=EditorAiTaskKind.CALIBRATION,
                based_on_revision_id="revision-1",
            )
            timer = threading.Timer(0.2, request_task_cancellation, args=(root,))
            timer.start()
            started = time.perf_counter()
            try:
                with task_context(task["task_id"]), patch(
                    "substar_core.stage2.python_script_command",
                    return_value=[sys.executable, "-c", "import time; time.sleep(60)"],
                ):
                    with self.assertRaises(EditorAiTaskCancelled):
                        _cancellable_editor_post(
                            url="https://provider.invalid/chat/completions",
                            headers={},
                            payload={"model": "test"},
                            timeout=300,
                        )
            finally:
                timer.cancel()
            self.assertLess(time.perf_counter() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
