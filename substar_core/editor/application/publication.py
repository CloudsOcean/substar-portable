"""Candidate artifacts and the single durable project publication boundary."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from substar_core.artifacts import atomic_write_json
from substar_core.domain import ChangeProvenance, DocumentRevision, EditorDocument
from substar_core.storage import ProjectStore

CANDIDATE_SCHEMA = "substar.editor-candidate.v1"


def recover_publications(task_service: Any, projects_root: Path) -> None:
    for task in task_service.list_tasks(states=["running", "cancelling", "interrupted"], limit=500):
        recover_publication(task_service, projects_root, str(task["task_id"]))


def recover_publication(task_service: Any, projects_root: Path, task_id: str) -> bool:
    task = task_service.get_task(task_id)
    if task["task_type"] not in {"translation", "calibration"}:
        return False
    project = (projects_root / str(task.get("project_id") or "")).resolve()
    if projects_root.resolve() not in project.parents or not (project / "project" / "manifest.json").exists():
        return False
    result = ProjectStore.open(project / "project").publication_result(task_id)
    if result is None:
        return False
    task_service.store.reconcile_publication(task_id, result)
    return True


def write_candidate(directory: Path, source: DocumentRevision, document: EditorDocument,
                    provenance: ChangeProvenance) -> DocumentRevision:
    document.validate()
    if document.complete:
        document = replace(document, properties=replace(document.properties, complete=False))
    candidate = DocumentRevision.create(
        revision_number=source.revision_number + 1, document=document,
        parent_revision_id=source.revision_id, provenance=provenance,
    )
    atomic_write_json(directory / "candidate.json", {
        "schema_version": CANDIDATE_SCHEMA, "source_revision_id": source.revision_id,
        "document_hash": document.content_hash(), "revision": candidate.to_dict(),
    })
    return candidate


def publish_candidate(store: ProjectStore, directory: Path, *, task_id: str,
                      expected_revision_id: str, summary: Mapping[str, Any]) -> DocumentRevision:
    path = directory / "candidate.json"
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    receipt_id = f"publication:{task_id}"
    existing = store.find_receipt(receipt_id, digest)
    if existing is not None:
        return existing
    value = json.loads(raw)
    if value.get("schema_version") != CANDIDATE_SCHEMA or value.get("source_revision_id") != expected_revision_id:
        raise ValueError("candidate source identity mismatch")
    candidate = DocumentRevision.from_dict(value["revision"])
    if candidate.document.content_hash() != value.get("document_hash"):
        raise ValueError("candidate document hash mismatch")
    if candidate.revision_id != summary.get("result_revision_id"):
        raise ValueError("candidate revision identity mismatch")
    latest = store.load_latest()
    if latest is None or latest.revision_id != expected_revision_id:
        raise ValueError("candidate source revision changed")
    expected = DocumentRevision.create(
        revision_number=latest.revision_number + 1, document=candidate.document,
        parent_revision_id=latest.revision_id, provenance=candidate.provenance,
    )
    if expected.revision_id != candidate.revision_id:
        raise ValueError("candidate revision does not belong to source")
    return store.save(candidate.document, provenance=candidate.provenance,
                      expected_revision_id=expected_revision_id,
                      receipt={"id": receipt_id, "hash": digest, "result": dict(summary)})
