"""Atomic review sidecar carried by the existing project/review package path."""
import json
import threading
from substar_core.artifacts import atomic_write_json

_lock = threading.RLock()


def read_reviews(path):
    if not path.exists():
        return {"schema_version": "substar.review-notes.v1", "version": 0, "notes": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != "substar.review-notes.v1":
        raise ValueError("不支持的批注文件版本")
    return value


def update_reviews(path, expected_version, mutate):
    with _lock:
        value = read_reviews(path)
        if value["version"] != expected_version:
            raise ValueError("批注已被其他窗口修改，请刷新后重试")
        mutate(value["notes"])
        value["version"] += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, value)
        return value
