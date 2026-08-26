from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class StageProgress:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {
            "schema_version": "substar.stage-progress.v1",
            "stages": {},
        }
        if path is not None and path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = loaded
            except (OSError, json.JSONDecodeError):
                pass

    def plan(
        self,
        stage: str,
        planned: int,
        *,
        additive: bool = False,
        block_ids: list[str] | None = None,
    ) -> None:
        with self.lock:
            with self._file_guard():
                self._refresh_from_disk()
                row = self._row(stage)
                row["planned"] = (
                    int(row.get("planned", 0)) + int(planned)
                    if additive
                    else int(planned)
                )
                row["status"] = "running"
                for block_id in block_ids or []:
                    self._block(row, block_id)
                self._write()

    def event(
        self,
        stage: str,
        event: str,
        amount: int = 1,
        *,
        block_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        key = {
            "sent": "sent",
            "response": "responses",
            "accepted": "accepted",
            "retry": "retries",
            "failed": "failed",
        }.get(event)
        if key is None:
            raise ValueError(f"未知进度事件：{event}")
        with self.lock:
            with self._file_guard():
                self._refresh_from_disk()
                row = self._row(stage)
                row[key] = int(row.get(key, 0)) + int(amount)
                row["status"] = "running"
                if block_id:
                    block = self._block(row, block_id)
                    block[key] = int(block.get(key, 0)) + int(amount)
                    if event == "sent":
                        block["status"] = (
                            "repairing"
                            if int(block.get("retries", 0))
                            else "running"
                        )
                    elif event == "retry":
                        block["status"] = "repairing"
                    elif event == "accepted":
                        block["status"] = (
                            "manual_review"
                            if int(block.get("failed", 0))
                            else "completed"
                        )
                    elif event == "failed":
                        block["status"] = "manual_review"
                    if detail:
                        block["detail"] = detail
                self._write()

    def finish(
        self,
        stage: str,
        *,
        failed: bool = False,
        with_review: bool = False,
    ) -> None:
        with self.lock:
            with self._file_guard():
                self._refresh_from_disk()
                row = self._row(stage)
                with_review = with_review or any(
                    block.get("status") == "manual_review"
                    for block in row.get("blocks", {}).values()
                )
                row["status"] = (
                    "failed"
                    if failed
                    else ("completed_with_review" if with_review else "completed")
                )
                self._write()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._refresh_from_disk()
            return json.loads(json.dumps(self.data))

    def _refresh_from_disk(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            self.data = loaded

    @contextmanager
    def _file_guard(self):
        if self.path is None:
            yield
            return
        lock_dir = self.path.with_suffix(self.path.suffix + ".lock")
        deadline = time.monotonic() + 10.0
        while True:
            try:
                lock_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    try:
                        age = time.time() - lock_dir.stat().st_mtime
                        if age > 30:
                            lock_dir.rmdir()
                            continue
                    except OSError:
                        pass
                    raise TimeoutError(f"等待进度账本文件锁超时：{lock_dir}")
                time.sleep(0.01)
        try:
            yield
        finally:
            try:
                lock_dir.rmdir()
            except OSError:
                pass

    def _row(self, stage: str) -> dict[str, Any]:
        stages = self.data.setdefault("stages", {})
        return stages.setdefault(
            stage,
            {
                "planned": 0,
                "sent": 0,
                "responses": 0,
                "accepted": 0,
                "retries": 0,
                "failed": 0,
                "status": "pending",
                "blocks": {},
            },
        )

    @staticmethod
    def _block(row: dict[str, Any], block_id: str) -> dict[str, Any]:
        blocks = row.setdefault("blocks", {})
        return blocks.setdefault(
            str(block_id),
            {
                "sent": 0,
                "responses": 0,
                "accepted": 0,
                "retries": 0,
                "failed": 0,
                "status": "pending",
                "detail": {},
            },
        )

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(
            self.path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Windows readers (status polling, antivirus, indexers) can briefly
        # hold the destination open. Progress reporting is auxiliary and must
        # never abort the subtitle pipeline because of a sharing violation.
        delay = 0.01
        for attempt in range(9):
            try:
                os.replace(temporary, self.path)
                return
            except PermissionError:
                if attempt == 8:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return
                time.sleep(delay)
                delay = min(delay * 2, 0.25)
