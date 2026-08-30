from __future__ import annotations

import json
import unittest

from substar_core.model_gateway.gateway import _response_json_utf8


class _MislabeledJsonResponse:
    def __init__(self, value: dict) -> None:
        self.content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.encoding = "iso-8859-1"


class ProviderResponseEncodingTests(unittest.TestCase):
    def test_model_gateway_ignores_mislabeled_charset_for_json(self) -> None:
        response = _MislabeledJsonResponse({"message": "中文审阅正常"})
        self.assertEqual(
            _response_json_utf8(response),
            {"message": "中文审阅正常"},
        )

if __name__ == "__main__":
    unittest.main()
