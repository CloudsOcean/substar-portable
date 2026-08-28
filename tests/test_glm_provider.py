from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import substar_core.config as config
from substar_core.api_testing import test_chat as call_test_chat
from substar_core.api_testing import probe_chat_thinking_modes
from substar_core.config import DEFAULTS, save_credentials_from_settings, save_settings
from substar_core.config import infer_model_provider
from substar_core.credential_store import (
    SEGMENT_DEEPSEEK,
    TRANSLATE_DEEPSEEK,
    canonicalize_credentials,
)
from substar_core.reasoning_capabilities import (
    reasoning_capabilities,
    reasoning_effort_for_request,
    resolve_reasoning_effort,
    resolve_thinking_mode,
)
from substar_core.model_providers import MODEL_PROVIDER_IDS, normalize_provider_profiles
from substar_core.openai_compat import endpoint_url


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


class _EmptyChatResponse(_ChatResponse):
    @staticmethod
    def json() -> dict:
        return {
            "model": "glm-5.3-flash",
            "choices": [{"message": {"content": "", "reasoning_content": ""}}],
            "usage": {"completion_tokens": 32},
        }


class _ReasoningOnlyChatResponse(_ChatResponse):
    @staticmethod
    def json() -> dict:
        return {
            "model": "provider-reasoning-model",
            "choices": [{"message": {"content": "", "reasoning_content": "ok"}}],
            "usage": {},
        }


class GlmProviderTests(unittest.TestCase):
    def test_cloud_llm_provider_catalog_excludes_local_model_installers(self) -> None:
        self.assertEqual(
            MODEL_PROVIDER_IDS,
            (
                "deepseek", "glm", "openai", "azure_openai", "deerapi",
                "gemini", "siliconflow", "qwen", "custom",
            ),
        )
        self.assertNotIn("ollama", MODEL_PROVIDER_IDS)

    def test_azure_endpoint_preserves_api_version_query(self) -> None:
        base = (
            "https://example.openai.azure.com/openai/deployments/substar"
            "?api-version=2024-10-21"
        )
        self.assertEqual(
            endpoint_url(base, "/chat/completions"),
            "https://example.openai.azure.com/openai/deployments/substar/"
            "chat/completions?api-version=2024-10-21",
        )

    def test_provider_profiles_are_schema_normalized_and_legacy_qwen_is_migrated(self) -> None:
        profiles = normalize_provider_profiles({
            "aliyun": {
                "base_url": " https://dashscope.aliyuncs.com/compatible-mode/v1 ",
                "model": " qwen-plus ",
                "auth_mode": "unexpected",
                "timeout_seconds": 9999,
                "ignored": "not persisted",
            }
        })
        self.assertEqual(set(profiles), {"qwen"})
        self.assertEqual(profiles["qwen"]["model"], "qwen-plus")
        self.assertEqual(profiles["qwen"]["auth_mode"], "bearer")
        self.assertEqual(profiles["qwen"]["timeout_seconds"], 600)
        self.assertNotIn("ignored", profiles["qwen"])

    def test_provider_profiles_have_distinct_persistable_credential_roles(self) -> None:
        values = canonicalize_credentials({
            "model_provider:deepseek": "deepseek-test-key",
            "model_provider:glm": "glm-provider-key",
        })
        self.assertEqual(values["model_provider:deepseek"], "deepseek-test-key")
        self.assertEqual(values["model_provider:glm"], "glm-provider-key")
        self.assertEqual(
            infer_model_provider("https://open.bigmodel.cn/api/paas/v4"), "glm"
        )

    @patch("substar_core.config._write_credential_envelope")
    @patch(
        "substar_core.config.load_credentials",
        return_value={"model_provider:deepseek": "deepseek-test-key"},
    )
    def test_saving_glm_key_preserves_deepseek_key(self, _load, write) -> None:
        values = save_credentials_from_settings({
            "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "translation_api_key": "glm-provider-key",
            "clear_translation_api_key": False,
        })
        self.assertEqual(values["model_provider:deepseek"], "deepseek-test-key")
        self.assertEqual(values["model_provider:glm"], "glm-provider-key")
        write.assert_called_once()

    @patch("substar_core.config._write_credential_envelope")
    @patch("substar_core.config.load_credentials", return_value={})
    def test_saving_glm_key_does_not_create_deepseek_legacy_aliases(self, _load, write) -> None:
        values = save_credentials_from_settings({
            "active_model_provider": "glm",
            "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "translation_api_key": "glm-provider-key",
        })
        self.assertEqual(values["model_provider:glm"], "glm-provider-key")
        self.assertNotIn(SEGMENT_DEEPSEEK, values)
        self.assertNotIn(TRANSLATE_DEEPSEEK, values)
        write.assert_called_once()

    def test_polluted_glm_legacy_alias_does_not_mark_deepseek_configured(self) -> None:
        credentials = {
            "model_provider:glm": "shared-old-glm-key",
            SEGMENT_DEEPSEEK: "shared-old-glm-key",
            TRANSLATE_DEEPSEEK: "shared-old-glm-key",
        }
        with TemporaryDirectory() as directory, patch.object(
            config, "_unique_paths", return_value=(Path(directory) / "missing.json",)
        ), patch.object(config, "load_credentials", return_value=credentials):
            settings = config.load_settings(include_secret=True)
        self.assertTrue(settings["model_provider_key_set"]["glm"])
        self.assertFalse(settings["model_provider_key_set"]["deepseek"])

    @patch("substar_core.config.save_credentials_from_settings")
    @patch("substar_core.config.atomic_write_json")
    @patch("substar_core.config.load_settings")
    def test_provider_switch_updates_every_stage_model(self, load, write, _credentials) -> None:
        current = {
            **DEFAULTS,
            "active_model_provider": "deepseek",
            "translation_api_base_url": "https://api.deepseek.com",
            "translation_api_model": "deepseek-v4-flash",
        }
        load.side_effect = [current, current]
        save_settings({
            "active_model_provider": "glm",
            "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "translation_api_model": "glm-5.3-flash",
        })
        persisted = write.call_args.args[1]
        for stage in (
            "segmentation", "segmentation_repair", "translation",
            "translation_repair", "calibration", "audit_repair",
        ):
            self.assertEqual(persisted[f"stage_{stage}_model"], "glm-5.3-flash")

    @patch("substar_core.config._write_credential_envelope")
    @patch("substar_core.config.load_credentials", return_value={})
    def test_explicit_provider_id_owns_credential_even_for_custom_url(self, _load, write) -> None:
        values = save_credentials_from_settings({
            "active_model_provider": "deerapi",
            "translation_api_base_url": "https://gateway.example/v1",
            "translation_api_key": "deer-key",
        })
        self.assertEqual(values["model_provider:deerapi"], "deer-key")
        write.assert_called_once()
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

    @patch("substar_core.api_testing.post", return_value=_EmptyChatResponse())
    def test_empty_but_valid_chat_response_confirms_connectivity_for_every_provider(self, _post) -> None:
        providers = (
            ("https://api.deepseek.com", "deepseek-chat"),
            ("https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash"),
            ("https://api.openai.com/v1", "gpt-5-mini"),
            ("https://api.siliconflow.cn/v1", "vendor/model"),
        )
        for base_url, model in providers:
            with self.subTest(base_url=base_url, model=model):
                result = call_test_chat(
                    base_url=base_url,
                    model=model,
                    api_key="test-key",
                    auth_mode="bearer",
                    timeout=30,
                    thinking_mode="enabled",
                    reasoning_effort="high",
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["response_channel"], "empty")
                self.assertIn("已接受请求", result["message"])

    @patch("substar_core.api_testing.post", return_value=_ReasoningOnlyChatResponse())
    def test_reasoning_only_chat_response_confirms_connectivity_for_any_provider(self, _post) -> None:
        result = call_test_chat(
            base_url="https://compatible.example/v1",
            model="provider-reasoning-model",
            api_key="test-key",
            auth_mode="bearer",
            timeout=30,
            thinking_mode="enabled",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["response_channel"], "reasoning_content")

    @patch("substar_core.api_testing.post", return_value=_ChatResponse())
    def test_probe_verifies_only_thinking_modes(self, mocked_post) -> None:
        result = probe_chat_thinking_modes(
            base_url="https://open.bigmodel.cn/api/paas/v4",
            model="glm-5.3",
            api_key="glm-test-key",
            auth_mode="bearer",
            timeout=30,
            reasoning_effort="high",
        )
        self.assertEqual(result["accepted_thinking_modes"], ["disabled", "enabled"])
        self.assertEqual(mocked_post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
