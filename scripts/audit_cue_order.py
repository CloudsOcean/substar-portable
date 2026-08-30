from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from substar_core.config import load_settings
from substar_core.storage import ProjectIntegrityError, ProjectStore


def _issues(cues: tuple[Any, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for position, cue in enumerate(cues):
        if cue.index != position:
            result.append(
                {
                    "kind": "index_mismatch",
                    "cue_id": cue.cue_id,
                    "position": position,
                    "index": cue.index,
                }
            )
        if position:
            previous = cues[position - 1]
            if (cue.start, cue.end) < (previous.start, previous.end):
                result.append(
                    {
                        "kind": "time_order",
                        "position": position,
                        "previous_cue_id": previous.cue_id,
                        "previous_time": [previous.start, previous.end],
                        "cue_id": cue.cue_id,
                        "time": [cue.start, cue.end],
                    }
                )
    return result


def audit(projects_root: Path, *, include_history: bool = False) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    for store_path in sorted(projects_root.glob("*/project_v2")):
        project_id = store_path.parent.name
        try:
            manifest_path = store_path / "manifest.json"
            store = ProjectStore.open(store_path)
            manifest = store.load_manifest()
            rows = manifest.get("revisions", [])
            if not include_history and rows:
                rows = [rows[-1]]
            for row in rows:
                revision = store.load_revision(str(row["revision_id"]))
                issues = _issues(revision.document.cues)
                if issues:
                    projects.append(
                        {
                            "project_id": project_id,
                            "revision_id": revision.revision_id,
                            "revision_number": revision.revision_number,
                            "issue_count": len(issues),
                            "issues": issues,
                        }
                    )
        except (OSError, ProjectIntegrityError, KeyError, ValueError) as exc:
            unreadable.append(
                {"project_id": project_id, "error": f"{type(exc).__name__}: {exc}"}
            )
    return {
        "schema_version": "substar.cue-order-audit.v1",
        "projects_root": str(projects_root.resolve()),
        "include_history": include_history,
        "affected_revision_count": len(projects),
        "affected": projects,
        "unreadable": unreadable,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit of Editor V2 cue order")
    parser.add_argument(
        "projects_root",
        nargs="?",
        type=Path,
        default=Path(load_settings(include_secret=False)["output_dir"]),
    )
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            audit(args.projects_root, include_history=args.history),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
