from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def overlay_relay_profile(settings: dict[str, Any], stage1_dir: Path) -> dict[str, Any]:
    path = stage1_dir / "relay_profile.json"
    if not path.is_file():
        return settings
    profile = json.loads(path.read_text(encoding="utf-8"))
    merged = dict(settings)
    merged.update(
        {
            "display_order": (
                "source_target"
                if profile.get("top_line_role", "source") == "source"
                else "target_source"
            ),
            "top_raised_punctuation": profile.get("top_raised_punctuation", merged.get("top_raised_punctuation")),
            "top_baseline_punctuation": profile.get("top_baseline_punctuation", merged.get("top_baseline_punctuation")),
            "bottom_raised_punctuation": profile.get("bottom_raised_punctuation", merged.get("bottom_raised_punctuation")),
            "bottom_baseline_punctuation": profile.get("bottom_baseline_punctuation", merged.get("bottom_baseline_punctuation")),
            "english_hard_limit": profile.get("english_hard_limit", merged.get("english_hard_limit")),
            "chinese_hard_limit": profile.get("chinese_hard_limit", merged.get("chinese_hard_limit")),
            "mixed_hard_limit": profile.get("mixed_hard_limit", merged.get("mixed_hard_limit")),
            "japanese_hard_limit": profile.get("japanese_hard_limit", merged.get("japanese_hard_limit")),
            "korean_hard_limit": profile.get("korean_hard_limit", merged.get("korean_hard_limit")),
        }
    )
    return merged
