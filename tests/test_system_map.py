from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts import system_map


ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "docs" / "architecture" / "system-map.json"


class SystemMapContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.value = json.loads(MAP_PATH.read_text(encoding="utf-8"))

    def test_machine_map_is_valid_and_generated_views_are_current(self) -> None:
        system_map.validate(self.value)
        system_map.generate(check=True)

    def test_every_browser_api_caller_has_an_explicit_backend_boundary(self) -> None:
        modules = self.value["modules"]
        expected = {
            "split_ui": {"composition_root", "editor_api"},
            "editor_api_client": {"editor_api", "composition_root"},
            "settings_ui": {
                "settings_service",
                "provider_test_service",
                "local_environment_service",
            },
            "glossary_ui": {"glossary_service"},
        }
        for module_id, required_calls in expected.items():
            module = modules[module_id]
            self.assertEqual("frontend_connector", module["layer"])
            self.assertTrue(module["inputs"], module_id)
            self.assertTrue(module["outputs"], module_id)
            self.assertTrue(required_calls.issubset(set(module["calls"])), module_id)

    def test_creation_worker_results_reach_their_finalizers(self) -> None:
        modules = self.value["modules"]
        expected = {
            "transcription_worker": ("transcription_result", "transcription_finalizer"),
            "segmentation_worker": ("segmentation_result", "segmentation_finalizer"),
        }
        for worker_id, (contract_id, finalizer_id) in expected.items():
            self.assertIn(contract_id, modules[worker_id]["outputs"])
            self.assertIn(contract_id, modules[finalizer_id]["inputs"])
            self.assertEqual("finalizer", modules[finalizer_id]["layer"])

    def test_primary_creation_flow_includes_provider_runtime_and_commit_boundaries(self) -> None:
        flow = self.value["flows"]["subtitle_creation"]["modules"]
        for module_id in (
            "split_ui",
            "composition_root",
            "scheduler",
            "worker_supervisor",
            "qwen_connector",
            "transcription_finalizer",
            "segmentation_model_connector",
            "segmentation_finalizer",
            "project_store",
            "editor_ui",
        ):
            self.assertIn(module_id, flow)


if __name__ == "__main__":
    unittest.main()
