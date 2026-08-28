from __future__ import annotations

import unittest
from unittest.mock import patch

from app import AutomaticTaskPayload, _automatic_settings_from_payload
from substar_core.config import DEFAULTS


class AutomaticModelProviderConsistencyTests(unittest.TestCase):
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
