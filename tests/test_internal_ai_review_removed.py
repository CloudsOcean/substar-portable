from __future__ import annotations

import json
import unittest
from pathlib import Path

from app import app


ROOT = Path(__file__).resolve().parents[1]


class InternalAiReviewRemovalTests(unittest.TestCase):
    def test_openapi_has_no_internal_review_routes_or_settings(self) -> None:
        schema = app.openapi()
        self.assertFalse(
            any("ai-review" in path for path in schema.get("paths", {}))
        )
        settings = schema["components"]["schemas"]["SettingsPayload"]["properties"]
        self.assertNotIn("review_enabled", settings)
        self.assertFalse(any(name.startswith("stage_review_") for name in settings))

    def test_prompt_registry_has_no_internal_review_family(self) -> None:
        registry = json.loads(
            (ROOT / "prompts" / "production" / "registry.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("editor_review", registry["prompts"])
        self.assertFalse((ROOT / "prompts" / "production" / "editor" / "review.md").exists())

    def test_editor_ai_task_contract_has_no_review_kind(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "editor-ai-task.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            schema["properties"]["kind"]["enum"],
            ["calibration", "translation"],
        )


if __name__ == "__main__":
    unittest.main()
