from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .glossary import active_glossary


def build_translation_context(
    *,
    job_dir: Path,
    project_name: str,
    translation_style: str,
    target_language_mode: str,
) -> dict[str, Any]:
    master_path = job_dir / "master_transcript.txt"
    master = master_path.read_text(encoding="utf-8") if master_path.exists() else ""
    folded = master.casefold()
    glossary = active_glossary(project_name)
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for item in glossary:
        terms = [item["source"], *item.get("aliases", [])]
        found = any(
            (term in master if item.get("case_sensitive") else term.casefold() in folded)
            for term in terms
            if term
        )
        (matched if found else unmatched).append(item)
    return {
        "schema_version": "substar.translation-context.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_name": project_name,
        "translation_style": translation_style,
        "target_language_mode": target_language_mode,
        "matched_glossary": matched,
        "available_glossary_count": len(glossary),
        "matched_glossary_count": len(matched),
        "unmatched_glossary_ids": [item["id"] for item in unmatched],
    }


def write_translation_context(job_dir: Path, settings: dict[str, Any]) -> Path:
    context = build_translation_context(
        job_dir=job_dir,
        project_name=str(settings.get("project_name", "")),
        translation_style=str(settings.get("translation_style", "corporate_broadcast")),
        target_language_mode=str(settings.get("target_language_mode", "auto_opposite")),
    )
    path = job_dir / "stage03T_translation_context.json"
    path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

