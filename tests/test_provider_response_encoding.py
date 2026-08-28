from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

from substar_core.stage2 import _response_json_utf8


ROOT = Path(__file__).resolve().parents[1]


class _MislabeledJsonResponse:
    def __init__(self, value: dict) -> None:
        self.content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.encoding = "iso-8859-1"


class ProviderResponseEncodingTests(unittest.TestCase):
    def test_stage2_ignores_mislabeled_charset_for_json(self) -> None:
        response = _MislabeledJsonResponse({"message": "中文审阅正常"})
        self.assertEqual(
            _response_json_utf8(response),
            {"message": "中文审阅正常"},
        )

    def test_editor_request_child_uses_the_same_utf8_contract(self) -> None:
        path = ROOT / "scripts" / "run_editor_model_request.py"
        spec = importlib.util.spec_from_file_location("editor_model_request", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        response = _MislabeledJsonResponse({"message": "兼容接口中文"})
        self.assertEqual(
            module._response_json_utf8(response),
            {"message": "兼容接口中文"},
        )
        wire = module._wire_json({"message": "管道中文不乱码"})
        wire.encode("ascii")
        self.assertEqual(json.loads(wire), {"message": "管道中文不乱码"})

if __name__ == "__main__":
    unittest.main()
