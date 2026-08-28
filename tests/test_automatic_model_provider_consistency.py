from __future__ import annotations

import unittest
from unittest.mock import patch

from app import (
    AutomaticTaskPayload,
    _automatic_settings_from_payload,
    _resolve_worker_credentials,
)
from substar_core.config import DEFAULTS
from substar_core.runtime.model import InvalidTaskError
from substar_core.segmentation.contracts import (
    build_segmentation_request,
    canonical_sha256,
    segmentation_credential_ref,
    validate_segmentation_request,
)


class AutomaticModelProviderConsistencyTests(unittest.TestCase):
    def test_glm_segmentation_requests_own_glm_credential(self) -> None:
        request = build_segmentation_request(
            transcription_task_id="tsk_" + "a" * 32,
            transcription_input_fingerprint="b" * 64,
            media_sha256="c" * 64,
            source_asset_id="project-1",
            language="zh-CN",
            segmentation_enabled=True,
            reference_document=None,
            prompt_snapshot={
                "relative_path": "task_inputs/segmentation_prompts",
                "sha256": "d" * 64,
                "file_count": 1,
            },
            glossary_snapshot=[],
            settings={
                "active_model_provider": "glm",
                "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
                "translation_api_model": "glm-5.3-flash",
            },
        )
        self.assertEqual(request["provider"]["id"], "glm")
        self.assertEqual(
            segmentation_credential_ref(request["provider"]), "model_provider:glm"
        )
        legacy = {
            **request,
            "provider": {
                key: value for key, value in request["provider"].items() if key != "id"
            },
        }
        legacy_without_fingerprint = {
            key: value for key, value in legacy.items() if key != "input_fingerprint"
        }
        legacy["input_fingerprint"] = canonical_sha256(legacy_without_fingerprint)
        validated_legacy = validate_segmentation_request(legacy)
        self.assertNotIn("id", validated_legacy["provider"])
        self.assertEqual(
            segmentation_credential_ref(validated_legacy["provider"]),
            "model_provider:glm",
        )

        mismatched = {
            **request,
            "provider": {**request["provider"], "id": "deepseek"},
        }
        mismatched_without_fingerprint = {
            key: value for key, value in mismatched.items() if key != "input_fingerprint"
        }
        mismatched["input_fingerprint"] = canonical_sha256(
            mismatched_without_fingerprint
        )
        with self.assertRaisesRegex(InvalidTaskError, "does not own"):
            validate_segmentation_request(mismatched)

    @patch("app.load_credentials", return_value={"model_provider:glm": "glm-secret-key"})
    def test_worker_resolves_provider_scoped_glm_key(self, _load_credentials) -> None:
        self.assertEqual(
            _resolve_worker_credentials(("model_provider:glm",)),
            {"model_provider:glm": "glm-secret-key"},
        )

    @patch("app.glossary_collection_exists", return_value=True)
    @patch("app.load_settings")
    def test_glm_task_rejects_frozen_deepseek_stage_defaults(
        self, load_settings, _glossary_exists
    ) -> None:
        load_settings.return_value = {
            **DEFAULTS,
            "active_model_provider": "glm",
            "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "translation_api_model": "glm-5.3-flash",
        }
        stale = {
            "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
            "translation_api_model": "glm-5.3-flash",
        }
        for stage in (
            "segmentation", "segmentation_repair", "translation",
            "translation_repair", "calibration", "audit_repair",
        ):
            stale[f"stage_{stage}_model"] = "deepseek-v4-flash"

        settings, _profile = _automatic_settings_from_payload(
            AutomaticTaskPayload(job_name="provider-consistency", settings_overrides=stale)
        )

        for stage in (
            "segmentation", "segmentation_repair", "translation",
            "translation_repair", "calibration", "audit_repair",
        ):
            self.assertEqual(settings[f"stage_{stage}_model"], "glm-5.3-flash")
        self.assertEqual(settings["active_model_provider"], "glm")
        self.assertEqual(settings["translation_api_auth_mode"], "bearer")

    @patch("app.glossary_collection_exists", return_value=True)
    @patch("app.load_settings")
    def test_deepseek_task_rejects_frozen_glm_stage_defaults(
        self, load_settings, _glossary_exists
    ) -> None:
        load_settings.return_value = {
            **DEFAULTS,
            "active_model_provider": "deepseek",
            "translation_api_base_url": "https://api.deepseek.com",
            "translation_api_model": "deepseek-v4-flash",
        }
        stale = {
            "translation_api_base_url": "https://api.deepseek.com",
            "translation_api_model": "deepseek-v4-flash",
            "stage_segmentation_model": "glm-5.3-flash",
            "stage_segmentation_repair_model": "glm-5.3-flash",
        }

        settings, _profile = _automatic_settings_from_payload(
            AutomaticTaskPayload(job_name="provider-consistency", settings_overrides=stale)
        )

        self.assertEqual(settings["stage_segmentation_model"], "deepseek-v4-flash")
        self.assertEqual(
            settings["stage_segmentation_repair_model"], "deepseek-v4-flash"
        )


if __name__ == "__main__":
    unittest.main()
