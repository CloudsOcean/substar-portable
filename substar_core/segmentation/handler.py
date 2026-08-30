from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from substar_core.artifacts import atomic_write_json
from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    EditorDocument,
)
from substar_core.process_command import python_script_command
from substar_core.manuscript_matching import (
    extract_reference_text,
    materialize_reference_alignment,
    materialize_reference_script,
)
from substar_core.runtime.model import InvalidTaskError
from substar_core.runtime.registry import TaskHandler, TaskWorkContext, WorkerLaunch
from substar_core.runtime.supervisor import WorkerCompletion
from substar_core.runtime.worker_protocol import WorkerMessage
from substar_core.storage import ProjectStore
from substar_core.transcription.contracts import (
    recognition_source_from_evidence,
    validate_recognition_evidence,
)
from substar_core.segmentation.input_contract import build_segmentation_material

from .contracts import (
    SEGMENTATION_CANDIDATE_SCHEMA,
    SEGMENTATION_INPUT_SCHEMA,
    SEGMENTATION_MANIFEST_SCHEMA,
    SEGMENTATION_RESULT_SCHEMA,
    SEGMENTATION_VALIDATION_SCHEMA,
    sha256_file,
    sha256_tree,
    segmentation_credential_ref,
    validate_segmentation_candidate,
    validate_segmentation_request,
)


_PROGRESS_MESSAGES = {
    "segmentation.input_prepare": "正在准备听写证据",
    "segmentation.semantic_grouping": "模型处理",
    "segmentation.cue_layout": "正在生成字幕 Cue",
    "segmentation.validation": "结果验收 · 正在校验字幕结构",
    "segmentation.repair": "修复 · 正在修复无效字幕结构",
    "segmentation.document_build": "生成可编辑结果 · 正在生成初始编辑文档",
    "segmentation.project_finalize": "交付产物 · 正在发布可编辑项目",
}
_ARTIFACT_CONTRACTS = {
    "segmentation_request.json": (
        "segmentation_request",
        SEGMENTATION_INPUT_SCHEMA,
    ),
    "segmentation_candidate.json": (
        "segmentation_candidate",
        SEGMENTATION_CANDIDATE_SCHEMA,
    ),
    "editor_document_candidate.json": (
        "editor_document_candidate",
        "substar.editor-document.v2",
    ),
    "segmentation_validation.json": (
        "segmentation_validation",
        SEGMENTATION_VALIDATION_SCHEMA,
    ),
    "segmentation_manifest.json": (
        "segmentation_manifest",
        SEGMENTATION_MANIFEST_SCHEMA,
    ),
    "reference_match.json": (
        "reference_match",
        "substar.reference-match.v1",
    ),
}


def _contained(root: Path, relative: str) -> Path:
    raw = str(relative).replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise InvalidTaskError("segmentation path is invalid")
    candidate = root.joinpath(*path.parts).resolve()
    if root != candidate and root not in candidate.parents:
        raise InvalidTaskError("segmentation path escapes its project")
    return candidate


def _project(projects_root: Path, project_id: str) -> Path:
    if not project_id or Path(project_id).name != project_id:
        raise InvalidTaskError("segmentation project_id is invalid")
    project = (projects_root / project_id).resolve()
    if projects_root not in project.parents or not project.is_dir():
        raise InvalidTaskError("segmentation project does not exist")
    return project


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(
        destination.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_rows(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise InvalidTaskError("segmentation worker artifacts must be an array")
    rows: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {
            "artifact_type", "relative_path", "schema_version", "sha256", "byte_size"
        }:
            raise InvalidTaskError("segmentation worker artifact fields are invalid")
        name = PurePosixPath(str(raw["relative_path"]).replace("\\", "/"))
        if name.is_absolute() or ".." in name.parts or len(name.parts) != 1:
            raise InvalidTaskError("segmentation worker artifact path is invalid")
        rendered = name.as_posix()
        if rendered in rows:
            raise InvalidTaskError("segmentation worker artifact path is duplicated")
        rows[rendered] = dict(raw)
    if set(rows) != set(_ARTIFACT_CONTRACTS):
        raise InvalidTaskError("segmentation worker artifact set is invalid")
    for name, row in rows.items():
        if (row["artifact_type"], row["schema_version"]) != _ARTIFACT_CONTRACTS[name]:
            raise InvalidTaskError(f"segmentation artifact contract changed: {name}")
        digest = str(row["sha256"]).lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise InvalidTaskError("segmentation artifact digest is invalid")
        size = row["byte_size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise InvalidTaskError("segmentation artifact size is invalid")
    return rows


def _worker_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "artifacts", "summary"
    }:
        raise InvalidTaskError("segmentation worker result fields are invalid")
    if value["schema_version"] != SEGMENTATION_RESULT_SCHEMA:
        raise InvalidTaskError("segmentation worker result schema is unsupported")
    rows = _artifact_rows(value["artifacts"])
    summary = value["summary"]
    if not isinstance(summary, Mapping) or set(summary) != {
        "mode",
        "cue_count",
        "source_token_count",
        "review_required_count",
        "input_fingerprint",
        "document_sha256",
    }:
        raise InvalidTaskError("segmentation worker summary is invalid")
    if summary["mode"] not in {"semantic", "sentence_boundaries", "reference_script"}:
        raise InvalidTaskError("segmentation worker summary mode is invalid")
    for field, minimum in (
        ("cue_count", 1),
        ("source_token_count", 1),
        ("review_required_count", 0),
    ):
        number = summary[field]
        if isinstance(number, bool) or not isinstance(number, int) or number < minimum:
            raise InvalidTaskError(f"segmentation worker summary {field} is invalid")
    for field in ("input_fingerprint", "document_sha256"):
        digest = str(summary[field]).lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise InvalidTaskError(f"segmentation worker summary {field} is invalid")
    return {"schema_version": SEGMENTATION_RESULT_SCHEMA, "artifacts": rows, "summary": dict(summary)}


def _validate_source_document(
    document: EditorDocument,
    expected_units: list[Mapping[str, Any]],
    source_asset_id: str,
) -> None:
    if not source_asset_id:
        raise InvalidTaskError("editor candidate source asset is missing")
    if len(document.source_tokens) != len(expected_units):
        raise InvalidTaskError("editor candidate source-token coverage changed")
    for position, (token, unit) in enumerate(zip(document.source_tokens, expected_units)):
        if (
            token.index != int(unit["index"])
            or token.text != str(unit["text"]).strip()
            or abs(token.start - float(unit["start"])) > 1e-6
            or abs(token.end - float(unit["end"])) > 1e-6
        ):
            raise InvalidTaskError(
                "editor candidate changed its validated source projection at "
                f"position {position}: candidate="
                f"({token.index!r}, {token.text!r}, {token.start!r}, {token.end!r}); "
                "expected="
                f"({unit['index']!r}, {str(unit['text']).strip()!r}, "
                f"{unit['start']!r}, {unit['end']!r})"
            )
    # EditorDocument intentionally hashes its identity. Exact token, timing,
    # media and task-fingerprint checks are the authoritative source binding.


def build_segmentation_handler(
    projects_root: Path,
    application_root: Path,
) -> TaskHandler:
    projects_root = projects_root.resolve()
    application_root = application_root.resolve()

    def validate_input(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_segmentation_request(payload)

    def prepare(context: TaskWorkContext) -> WorkerLaunch:
        request = validate_segmentation_request(context.input_payload)
        project = _project(projects_root, str(context.task.get("project_id") or ""))
        evidence = _contained(
            project, request["transcription"]["evidence_relative_path"]
        )
        if not evidence.is_file():
            raise InvalidTaskError("recognition evidence is not available")
        prompt_root = _contained(project, request["prompt_snapshot"]["relative_path"])
        if not prompt_root.is_dir():
            raise InvalidTaskError("segmentation prompt snapshot is not available")
        prompt_digest, prompt_count = sha256_tree(prompt_root)
        if (
            prompt_digest != request["prompt_snapshot"]["sha256"]
            or prompt_count != request["prompt_snapshot"]["file_count"]
        ):
            raise InvalidTaskError("segmentation prompt snapshot changed")
        reference_path: Path | None = None
        if request["reference_document"] is not None:
            reference_path = _contained(
                project, request["reference_document"]["relative_path"]
            )
            if (
                not reference_path.is_file()
                or reference_path.stat().st_size
                != request["reference_document"]["byte_size"]
                or sha256_file(reference_path)
                != request["reference_document"]["sha256"]
            ):
                raise InvalidTaskError("segmentation reference document changed")
        previous_artifacts: Path | None = None
        if int(context.task.get("attempt") or 0) > 1:
            candidate = (
                context.attempt_directory.parent
                / str(int(context.task["attempt"]) - 1)
                / "artifacts"
            ).resolve()
            if all((candidate / name).is_file() for name in _ARTIFACT_CONTRACTS):
                previous_artifacts = candidate
        credentials = (
            (segmentation_credential_ref(request["provider"]),)
            if request["mode"] == "semantic"
            and os.environ.get("SUBSTAR_MOCK_SEGMENTATION", "") != "1"
            and previous_artifacts is None
            else ()
        )
        return WorkerLaunch(
            argv=tuple(python_script_command("scripts/run_segmentation_worker.py")),
            cwd=application_root,
            project_root=project,
            worker_input={
                "request": request,
                "resolved_evidence_path": str(evidence),
                "resolved_reference_path": str(reference_path) if reference_path else None,
                "resolved_prompt_root": str(prompt_root),
                "application_root": str(application_root),
                "resolved_recovery_artifact_directory": (
                    str(previous_artifacts) if previous_artifacts else None
                ),
            },
            credential_refs=credentials,
            timeout_seconds=float(request["constraints"]["task_timeout_seconds"] + 120),
        )

    def handle_worker_event(
        _context: TaskWorkContext, message: WorkerMessage
    ) -> Mapping[str, Any]:
        step = str(message.step or "")
        if step not in _PROGRESS_MESSAGES:
            raise InvalidTaskError("segmentation worker progress step is unsupported")
        display_message = _PROGRESS_MESSAGES[step]
        if step == "segmentation.semantic_grouping":
            planned = int(message.data.get("planned", 0) or 0)
            if planned > 0:
                responses = min(planned, int(message.data.get("responses", 0) or 0))
                completed = min(planned, int(message.data.get("completed", 0) or 0))
                repairing = max(0, int(message.data.get("repairing", 0) or 0))
                failed = max(0, int(message.data.get("failed", 0) or 0))
                display_message = f"模型处理 {completed}/{planned} 块"
                if repairing:
                    display_message += f" · 修复中 {repairing} 块"
                if failed:
                    display_message += f" · 待人工 {failed} 块"
        return {
            "progress": float(message.progress or 0.0),
            "message": display_message,
            "step": step,
            "wait_reason": None,
            "phase": (
                "repair" if step == "segmentation.repair" else
                "validation" if step == "segmentation.validation" else
                "delivery" if step in {"segmentation.document_build", "segmentation.project_finalize"} else
                "primary"
            ),
            "completed_units": (
                int(message.data.get("completed", 0) or 0)
                if step == "segmentation.semantic_grouping" else None
            ),
            "total_units": (
                int(message.data.get("planned", 0) or 0)
                if step == "segmentation.semantic_grouping" else None
            ),
        }

    def finalize(
        context: TaskWorkContext, completion: WorkerCompletion
    ) -> Mapping[str, Any]:
        result = _worker_result(completion.result)
        request = validate_segmentation_request(context.input_payload)
        project = _project(projects_root, str(context.task.get("project_id") or ""))
        for name, row in result["artifacts"].items():
            source = (context.artifact_directory / name).resolve()
            if context.artifact_directory.resolve() not in source.parents or not source.is_file():
                raise InvalidTaskError(f"segmentation artifact is missing: {name}")
            if source.stat().st_size != row["byte_size"] or sha256_file(source) != row["sha256"]:
                raise InvalidTaskError(f"segmentation artifact changed before finalization: {name}")

        request_artifact = json.loads(
            (context.artifact_directory / "segmentation_request.json").read_text(
                encoding="utf-8"
            )
        )
        if request_artifact != request:
            raise InvalidTaskError("segmentation request artifact differs from task input")
        candidate = validate_segmentation_candidate(
            json.loads(
                (context.artifact_directory / "segmentation_candidate.json").read_text(
                    encoding="utf-8"
                )
            ),
            request,
        )
        document = EditorDocument.from_dict(
            json.loads(
                (
                    context.artifact_directory / "editor_document_candidate.json"
                ).read_text(encoding="utf-8")
            )
        )
        validation = json.loads(
            (context.artifact_directory / "segmentation_validation.json").read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(validation, Mapping) or validation.get("schema_version") != SEGMENTATION_VALIDATION_SCHEMA:
            raise InvalidTaskError("segmentation validation artifact is invalid")
        manifest = json.loads(
            (context.artifact_directory / "segmentation_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != SEGMENTATION_MANIFEST_SCHEMA:
            raise InvalidTaskError("segmentation manifest artifact is invalid")
        reference_match = json.loads(
            (context.artifact_directory / "reference_match.json").read_text(
                encoding="utf-8"
            )
        )
        if (
            not isinstance(reference_match, Mapping)
            or reference_match.get("schema_version") != "substar.reference-match.v1"
            or bool(reference_match.get("applied"))
            != (request["reference_document"] is not None)
        ):
            raise InvalidTaskError("reference-match artifact is invalid")
        if request["reference_document"] is not None and (
            reference_match.get("reference_sha256")
            != request["reference_document"]["sha256"]
        ):
            raise InvalidTaskError("reference-match artifact belongs to another document")
        summary = result["summary"]
        if (
            summary["input_fingerprint"] != request["input_fingerprint"]
            or summary["mode"] != request["mode"]
            or summary["cue_count"] != len(document.cues)
            or summary["source_token_count"] != len(document.source_tokens)
            or summary["document_sha256"] != document.content_hash()
            or validation.get("document_sha256") != document.content_hash()
            or candidate["validation"].get("document_sha256") != document.content_hash()
        ):
            raise InvalidTaskError("segmentation result summary does not match candidate")
        evidence = validate_recognition_evidence(
            json.loads(
                _contained(
                    project, request["transcription"]["evidence_relative_path"]
                ).read_text(encoding="utf-8")
            )
        )
        if (
            evidence["request"]["input_fingerprint"]
            != request["transcription"]["input_fingerprint"]
            or evidence["media"]["sha256"]
            != request["transcription"]["media_sha256"]
        ):
            raise InvalidTaskError("segmentation finalizer source evidence changed")
        expected_master = str(evidence["master_text"]).strip()
        expected_alignment = recognition_source_from_evidence(evidence)
        if request["reference_document"] is not None:
            reference_path = _contained(
                project, request["reference_document"]["relative_path"]
            )
            reference_text = extract_reference_text(
                reference_path.read_bytes(), reference_path.name
            )
            if request["mode"] == "reference_script":
                expected_material, _expected_breaks, expected_report = (
                    materialize_reference_script(
                        reference_text,
                        expected_alignment.get("units", []),
                        str(request["constraints"]["reference_break_symbols"]),
                        str(request.get("language") or "Auto"),
                    )
                )
            else:
                expected_master, expected_alignment, expected_report = (
                    materialize_reference_alignment(
                        reference_text,
                        expected_alignment,
                        str(request.get("language") or "Auto"),
                    )
                )
            if reference_match.get("report") != expected_report:
                raise InvalidTaskError(
                    "reference-match audit differs from deterministic projection"
                )
        if request["mode"] == "reference_script":
            expected_units = list(expected_material["units"])
        else:
            expected_units = list(
                build_segmentation_material(expected_master, expected_alignment)["units"]
            )
        _validate_source_document(
            document,
            expected_units,
            request["source_asset_id"],
        )

        project_store_path = project / "project"
        if (project_store_path / "manifest.json").is_file():
            store = ProjectStore.open(project_store_path)
            latest = store.load_latest()
        else:
            store = ProjectStore.create(
                project_store_path, project_id=str(context.task["project_id"])
            )
            latest = None
        if latest is not None:
            if latest.document.content_hash() != document.content_hash():
                raise InvalidTaskError(
                    "initial editor document conflicts with an existing project revision"
                )
            revision = latest
        else:
            revision = store.save(
                document,
                provenance=ChangeProvenance(
                    kind=ChangeKind.IMPORT,
                    operation="segmentation_initial_document",
                    actor="segmentation-finalizer",
                    metadata={
                        "task_id": completion.task_id,
                        "attempt": completion.attempt,
                        "input_fingerprint": request["input_fingerprint"],
                        "candidate_sha256": result["artifacts"]["segmentation_candidate.json"]["sha256"],
                        "source_transcription_task_id": request["transcription"]["task_id"],
                    },
                ),
                expected_revision_id=None,
            )

        publish_root = project / "segmentation"
        for name in result["artifacts"]:
            _copy_atomic(context.artifact_directory / name, publish_root / name)
        pointer = {
            "schema_version": "substar.editor-revision-pointer.v1",
            "project_id": str(context.task["project_id"]),
            "revision_id": revision.revision_id,
            "revision_number": revision.revision_number,
            "document_hash": document.content_hash(),
            "task_id": completion.task_id,
            "attempt": completion.attempt,
        }
        atomic_write_json(publish_root / "editor_revision.json", pointer)
        return {
            "schema_version": SEGMENTATION_RESULT_SCHEMA,
            "candidate": {
                "sha256": result["artifacts"]["segmentation_candidate.json"]["sha256"],
                "byte_size": result["artifacts"]["segmentation_candidate.json"]["byte_size"],
            },
            "revision": pointer,
            "summary": summary,
            "needs_attention": bool(summary["review_required_count"]),
        }

    return TaskHandler(
        task_type="segmentation",
        validate_input=validate_input,
        handle_worker_event=handle_worker_event,
        prepare=prepare,
        finalize=finalize,
        # Provider work is safe across distinct projects. ProjectStore uses a
        # per-project transaction for the short Finalizer commit, so a global
        # write claim must not serialize the whole model request.
        resources=("worker", "provider_io"),
    )
