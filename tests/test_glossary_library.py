from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from substar_core import glossary


class GlossaryLibraryTests(unittest.TestCase):
    def test_pre_v2_glossary_is_not_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "glossary.json"
            path.write_text(json.dumps([
                {"source": "OpenAI", "target": "开放人工智能", "scope": "global"},
                {"source": "Codex", "target": "代码助手", "scope": "project", "project": "第一期", "notes": "保留备注"},
            ], ensure_ascii=False), encoding="utf-8")
            with patch.object(glossary, "GLOSSARY_FILE", path), patch.object(glossary, "APP_DATA_DIR", root):
                library = glossary.load_glossary_library()
                self.assertEqual(library["schema_version"], glossary.GLOSSARY_SCHEMA_VERSION)
                self.assertEqual(library["collections"], [{"id": "global", "name": "全局词库", "kind": "global"}])
                self.assertEqual(library["entries"], [])

    def test_selected_project_glossary_overrides_global(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "glossary.json"
            collections = [{"id": "show", "name": "节目词库", "kind": "project"}]
            entries = [
                {"source": "Nova", "target": "诺瓦", "glossary_id": "global"},
                {"source": "Nova", "target": "新星", "glossary_id": "show"},
                {"source": "OpenAI", "target": "OpenAI", "glossary_id": "global"},
            ]
            with patch.object(glossary, "GLOSSARY_FILE", path), patch.object(glossary, "APP_DATA_DIR", root):
                glossary.save_glossary_library(collections, entries)
                active = glossary.active_glossary("show")
                self.assertEqual({item["source"]: item["target"] for item in active}, {"OpenAI": "OpenAI", "Nova": "新星"})
                self.assertEqual([item["source"] for item in glossary.active_glossary()], ["Nova", "OpenAI"])

    def test_single_language_prompt_does_not_expose_translation(self) -> None:
        entry = glossary.normalize_entry({"source": "Codex", "target": "代码助手"})
        prompt = glossary.glossary_prompt([entry], include_target=False)
        self.assertIn("单语处理", prompt)
        self.assertNotIn("代码助手", prompt)
        self.assertNotIn('"target"', prompt)


if __name__ == "__main__":
    unittest.main()
