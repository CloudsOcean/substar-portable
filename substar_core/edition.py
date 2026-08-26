from __future__ import annotations

import os
from typing import Any


VALID_EDITIONS = {"standard", "full", "slim"}


def current_edition() -> str:
    value = os.getenv("SUBSTAR_EDITION", "standard").strip().lower()
    return value if value in VALID_EDITIONS else "standard"


def is_slim() -> bool:
    return current_edition() == "slim"


def capabilities() -> dict[str, Any]:
    slim = is_slim()
    return {
        "edition": current_edition(),
        "jianying": not slim,
        "local_recognition": not slim,
        "recognition_profiles": ["qwen_cloud"] if slim else None,
        "split_workflows": ["disabled", "one_step"],
        "translation_workflows": ["disabled", "one_step"],
    }


def constrain_settings(settings: dict[str, Any]) -> dict[str, Any]:
    value = dict(settings)
    value["split_workflow_mode"] = "one_step"
    value["translation_workflow_mode"] = "one_step"
    value["segmentation_strategy"] = "semantic"
    if not is_slim():
        return value
    value["recognition_profile_id"] = "qwen_cloud"
    value["transcript_source"] = "qwen_cloud"
    value["alignment_source"] = "qwen_cloud_native"
    return value
