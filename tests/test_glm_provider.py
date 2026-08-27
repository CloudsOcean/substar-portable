from __future__ import annotations

import unittest
from unittest.mock import patch

from substar_core.api_testing import test_chat as call_test_chat
from substar_core.config import apply_declared_model_capabilities
from substar_core.reasoning_capabilities import (
    reasoning_capabilities,
    reasoning_effort_for_request,
    resolve_reasoning_effort,
    resolve_thinking_mode,
)


class _ChatResponse:
    ok = True
    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict:
        return {
            "model": "glm-5.3",
            "choices": [{"message": {"content": '{"substar_ok":true}'}}],
            "usage": {},
        }


class GlmProviderTests(unittest.TestCase):
    def test_reasoning_contract_recognizes_bigmodel(self) -> None:
        capability = reasoning_capabilities(
            "https://open.bigmodel.cn/api/paas/v4", "glm-5.3"
        )
        self.assertEqual(capability["provider"], "glm")
        self.assertEqual(capability["supported_efforts"], ["low", "high", "max"])
        self.assertEqual(
            capability["effort_wire_map"],
            {"low": "low", "high": "high", "max": "max"},
        )
        self.assertTrue(capability["forced_thinking"])
        self.assertEqual(
            resolve_thinking_mode(
                "https://open.bigmodel.cn/api/paas/v4", "glm-5.3", "disabled"
            ),
            "enabled",
        )
        self.assertEqual(
            resolve_reasoning_effort(
                "https://open.bigmodel.cn/api/paas/v4", "glm-5.3", "low"
            ),
            "low",
        )
        self.assertEqual(
            resolve_reasoning_effort(
                "https://open.bigmodel.cn/api/paas/v4", "glm-5.3", "xhigh"
            ),
            "max",
        )
        self.assertEqual(
            reasoning_effort_for_request(
                "https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash", "low"
            ),
            "low",
        )

    def test_settings_are_normalized_from_the_declared_capability(self) -> None:
        settings = {
            "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "translation_api_model": "glm-5.3",
            "stage_review_model": "glm-5.3",
            "stage_review_thinking_mode": "disabled",
            "stage_review_reasoning_effort": "low",
        }
        apply_declared_model_capabilities(settings)
        self.assertEqual(settings["stage_review_thinking_mode"], "enabled")
        self.assertEqual(settings["stage_review_reasoning_effort"], "low")

    @patch("substar_core.api_testing.post", return_value=_ChatResponse())
    def test_connection_uses_official_chat_endpoint_and_bearer_key(self, mocked_post) -> None:
        result = call_test_chat(
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model="glm-5.3",
            api_key="glm-test-key",
            auth_mode="bearer",
            timeout=30,
            thinking_mode="enabled",
            reasoning_effort="low",
        )
        self.assertTrue(result["ok"])
        args, kwargs = mocked_post.call_args
        self.assertEqual(
            args[0], "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer glm-test-key")
        self.assertEqual(kwargs["json"]["model"], "glm-5.3")
        self.assertEqual(kwargs["json"]["thinking"], {"type": "enabled"})
        self.assertEqual(kwargs["json"]["reasoning_effort"], "low")
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
