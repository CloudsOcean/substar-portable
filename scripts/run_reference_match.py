"""One isolated reference alignment; no project write permissions in the protocol."""
from pathlib import Path
import base64
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from substar_core.manuscript_matching import editor_reference_operations, extract_reference_text

if __name__ == "__main__":
    try:
        value = json.loads(sys.stdin.buffer.read())
        text = extract_reference_text(base64.b64decode(value["payload"], validate=True), value["filename"])
        result = editor_reference_operations(text, value["units"], value["language"])
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    except Exception as exc:
        sys.stderr.buffer.write(str(exc).encode("utf-8"))
        raise SystemExit(1)
