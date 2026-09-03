from __future__ import annotations

import json
import sys
from pathlib import Path

import requests


def main() -> None:
    media = Path(sys.argv[1]).resolve()
    base = "http://127.0.0.1:8769"
    settings = requests.get(f"{base}/api/settings", timeout=20).json()
    settings.update({
        "recognition_profile_id": "qwen_cloud",
        "language": "en",
        "alignment_language": "English",
        "target_language_mode": "zh-CN",
        "segmentation_enabled": True,
        "translation_enabled": True,
        "calibration_enabled": False,
        "review_enabled": False,
        "split_workflow_mode": "one_step",
        "translation_workflow_mode": "one_step",
        "segmentation_strategy": "production_one_step",
    })
    with media.open("rb") as handle:
        response = requests.post(
            f"{base}/api/project-creations",
            data={
                "mode": "asr",
                "settings_json": json.dumps(settings, ensure_ascii=False),
                "debug_merged": "false",
            },
            files={"media": (media.name, handle, "video/mp4")},
            timeout=180,
        )
    response.raise_for_status()
    value = response.json()
    print(json.dumps({
        "id": value.get("id"), "status": value.get("status"),
        "display_name": value.get("display_name"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
