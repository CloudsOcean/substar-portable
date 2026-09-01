from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from substar_core.config import settings_for_model_provider
from substar_core.editor.http_api import _editor_ai_idempotency_key
from substar_core.task_info import load_task_info, save_task_info


class ProjectLlmSelectionTests(unittest.TestCase):
    def test_provider_selection_changes_editor_task_identity(self) -> None:
        base = {
            "schema_version": "substar.editor.translation-input.v2",
            "expected_revision_id": "rev-1",
            "provider_id": "deepseek",
            "settings": {"translation_api_model": "deepseek-v4-flash"},
        }
        changed = {
            **base,
            "provider_id": "glm",
            "settings": {"translation_api_model": "glm-5.3-flash"},
        }
        self.assertNotEqual(
            _editor_ai_idempotency_key("translation", base),
            _editor_ai_idempotency_key("translation", changed),
        )

    def test_provider_profile_overrides_every_editor_llm_stage(self) -> None:
        base = {
            "active_model_provider": "deepseek",
            "translation_api_base_url": "https://api.deepseek.com",
            "translation_api_model": "deepseek-old",
            "model_provider_profiles": {
                "glm": {
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "model": "glm-5.3-flash",
                    "auth_mode": "bearer",
                    "timeout_seconds": 120,
                }
            },
        }
        with patch(
            "substar_core.config.load_credentials",
            return_value={"model_provider:glm": "glm-test-key"},
        ):
            selected = settings_for_model_provider(
                "glm", include_secret=True, base_settings=base
            )
        self.assertEqual(selected["translation_api_key"], "glm-test-key")
        self.assertEqual(selected["translation_api_model"], "glm-5.3-flash")
        self.assertEqual(selected["stage_calibration_model"], "glm-5.3-flash")
        self.assertEqual(selected["stage_translation_model"], "glm-5.3-flash")

    def test_task_info_persists_project_provider(self) -> None:
        with TemporaryDirectory() as folder:
            job_dir = Path(folder)
            saved = save_task_info(job_dir, "project-1", {
                "display_name": "Demo",
                "language": "en",
                "target_language_mode": "zh-CN",
                "source_hard_limit": 55,
                "target_hard_limit": 25,
                "glossary_id": "",
                "llm_provider_id": "glm",
            })
            self.assertEqual(saved["llm_provider_id"], "glm")
            self.assertEqual(
                load_task_info(job_dir, "project-1")["llm_provider_id"], "glm"
            )


if __name__ == "__main__":
    unittest.main()
