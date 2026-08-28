from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from substar_core import glossary


class GlossaryLibraryTests(unittest.TestCase):
    def test_legacy_project_entries_migrate_without_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "glossary.json"
            path.write_text(json.dumps([
                {"source": "OpenAI", "target": "开放人工智能", "scope": "global"},
                {"source": "Codex", "target": "代码助手", "scope": "project", "project": "第一期", "notes": "保留备注"},
            ], ensure_ascii=False), encoding="utf-8")
            with patch.object(glossary, "GLOSSARY_FILE", path), patch.object(glossary, "APP_DATA_DIR", root):
                library = glossary.load_glossary_library()
                project = next(item for item in library["collections"] if item["kind"] == "project")
                migrated = next(item for item in library["entries"] if item["source"] == "Codex")
                self.assertEqual(project["name"], "第一期")
                self.assertEqual(migrated["glossary_id"], project["id"])
                self.assertEqual(migrated["target"], "代码助手")
                self.assertEqual(migrated["notes"], "保留备注")

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
