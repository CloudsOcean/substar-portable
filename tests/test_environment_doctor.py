from __future__ import annotations

import unittest
from unittest.mock import patch

from substar_core import environment_doctor


class EnvironmentDoctorTests(unittest.TestCase):
    def test_cloud_profile_is_ready_without_optional_torch(self) -> None:
        asset_report = {
            "assets": [],
            "model_root": "models",
            "download_source": {},
            "download_sources": [],
        }
        profile = {
            "id": "qwen_cloud",
            "label": "Qwen Cloud",
            "description": "Cloud transcription",
        }

        with (
            patch.object(environment_doctor, "_command_output", return_value=""),
            patch.object(environment_doctor, "_find_tool", side_effect=lambda name: name),
            patch.object(environment_doctor.shutil, "disk_usage") as disk_usage,
            patch.object(environment_doctor, "model_asset_status", return_value=asset_report),
            patch.object(environment_doctor, "list_recognition_profiles", return_value=[profile]),
        ):
            disk_usage.return_value.free = 10 * 1024**3
            report = environment_doctor.environment_status()

        self.assertTrue(report["core_runtime_ready"])
        self.assertTrue(report["ready"])
        self.assertEqual(report["ready_profile_count"], 1)


if __name__ == "__main__":
    unittest.main()
