from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace

from substar_core.editor.translation.service import _source_rows
from substar_core.editor.http_api import TranslationStartRequest


class EditorTranslationBindingTests(unittest.TestCase):
    def test_translation_request_carries_explicit_source_language(self) -> None:
        request = TranslationStartRequest(
            expected_revision_id="revision-1",
            source_language="en",
            target_language="zh-CN",
        )
        self.assertEqual(request.source_language, "en")

    def test_editor_submits_user_confirmed_source_language(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "editor.html").read_text(encoding="utf-8")
        script = (root / "web" / "editor.js").read_text(encoding="utf-8")
        self.assertIn('id="translationSourceLanguage"', html)
        self.assertIn("configureTranslationLanguageDefaults(taskInfo)", script)
        self.assertIn('api(projectPath("/task-info"))', script)
        self.assertIn("source_language:sourceLanguage", script)

    def test_each_translation_cue_is_bound_to_source_projection_hash(self) -> None:
        active = SimpleNamespace(value="active")
        document = SimpleNamespace(
            display_tokens=(
                SimpleNamespace(token_id="t1", text="Hello", state=active),
                SimpleNamespace(token_id="t2", text="world.", state=active),
            ),
            cues=(
                SimpleNamespace(
                    cue_id="cue-1", state=active, display_token_ids=("t1", "t2")
                ),
            ),
        )
        rows = _source_rows(document)
        self.assertEqual(rows, [{
            "cue_id": "cue-1",
            "source_hash": hashlib.sha256(b"Hello world.").hexdigest(),
        }])


if __name__ == "__main__":
    unittest.main()
