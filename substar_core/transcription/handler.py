from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from substar_core.artifacts import atomic_write_json, atomic_write_text
from substar_core.credential_store import ASR_GENERIC, ASR_QWEN
from substar_core.process_command import python_script_command
from substar_core.runtime import TaskHandler, TaskWorkContext, WorkerLaunch
from substar_core.runtime.model import InvalidTaskError
from substar_core.runtime.supervisor import WorkerCompletion
from substar_core.runtime.worker_protocol import WorkerMessage
from substar_core.segmentation.input_contract import SEGMENTATION_MATERIAL_SCHEMA

from .contracts import (
    RECOGNITION_EVIDENCE_SCHEMA,
    TRANSCRIPTION_INPUT_SCHEMA,
    TRANSCRIPTION_RESULT_SCHEMA,
    sha256_file,
    validate_recognition_evidence,
    validate_transcription_request,
)


_REQUIRED_ARTIFACTS = {
    "recognition_evidence.json": ("recognition_evidence", RECOGNITION_EVIDENCE_SCHEMA),
    "master_transcript.txt": ("recognition_transcript", None),
    "alignment.tsv": ("recognition_alignment_table", None),
    "segmentation_material.json": (
        "segmentation_material",
        SEGMENTATION_MATERIAL_SCHEMA,
    ),
    "audio_16k_mono.wav": ("prepared_audio", None),
    "recognition_request.json": ("recognition_request", TRANSCRIPTION_INPUT_SCHEMA),
    "run_manifest.json": ("recognition_manifest", "substar.run.v1"),
    "provider_submission_audit.json": (
        "provider_submission_audit",
        "substar.provider-submission-audit.v1",
    ),
}
_OPTIONAL_ARTIFACTS = {
    "asr_ingest_report.json": ("recognition_audit", "substar.asr-ingest-report.v1"),
    "provider_response.json": ("provider_response_private", None),
}
_PRIVATE_ARTIFACTS = {"provider_response.json"}
_PROGRESS_MESSAGES = {
    "transcription.media_probe": "Reading media metadata.",
    "transcription.audio_prepare": "Preparing transcription audio.",
    "transcription.provider_audio_encode": "Encoding transcription audio.",
    "transcription.provider_upload": "Uploading transcription audio.",
    "transcription.provider_run": "The recognition provider is transcribing.",
    "transcription.evidence_normalize": "Normalizing word-level recognition evidence.",
    "transcription.artifact_finalize": "Validating transcription artifacts.",
}


def _contained_project(projects_root: Path, project_id: str) -> Path:
    root = projects_root.resolve()
    candidate = (root / project_id).resolve()
    if root not in candidate.parents or not candidate.is_dir():
        raise InvalidTaskError("transcription project does not exist")
    return candidate


def _resolved_media(project: Path, request: Mapping[str, Any]) -> Path:
    relative = PurePosixPath(str(request["media"]["relative_path"]))
    candidate = project.joinpath(*relative.parts).resolve()
    if project not in candidate.parents or not candidate.is_file():
        raise InvalidTaskError("transcription media does not exist inside its project")
    if candidate.stat().st_size != int(request["media"]["byte_size"]):
        raise InvalidTaskError("transcription media size changed after task creation")
    if sha256_file(candidate) != request["media"]["sha256"]:
        raise InvalidTaskError("transcription media changed after task creation")
    return candidate


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_rows(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise InvalidTaskError("transcription worker artifacts must be an array")
    rows: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, Mapping):
            raise InvalidTaskError("transcription worker artifact must be an object")
        required = {
            "artifact_type", "relative_path", "schema_version", "sha256", "byte_size"
        }
        if set(raw) != required:
            raise InvalidTaskError("transcription worker artifact fields are invalid")
        relative = PurePosixPath(str(raw["relative_path"]).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
            raise InvalidTaskError("transcription worker artifact path is invalid")
        name = relative.as_posix()
        if name in rows:
            raise InvalidTaskError("transcription worker artifact path is duplicated")
        rows[name] = dict(raw)
    return rows


def _validate_worker_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "artifacts", "summary"
    }:
        raise InvalidTaskError("transcription worker result fields are invalid")
    if value["schema_version"] != TRANSCRIPTION_RESULT_SCHEMA:
        raise InvalidTaskError("transcription worker result schema is unsupported")
    rows = _artifact_rows(value["artifacts"])
    if not set(_REQUIRED_ARTIFACTS).issubset(rows):
        raise InvalidTaskError("transcription worker result is missing required artifacts")
    if set(rows) - set(_REQUIRED_ARTIFACTS) - set(_OPTIONAL_ARTIFACTS):
        raise InvalidTaskError("transcription worker result contains unknown artifacts")
    for name, row in rows.items():
        expected = (_REQUIRED_ARTIFACTS | _OPTIONAL_ARTIFACTS)[name]
        if (row["artifact_type"], row["schema_version"]) != expected:
            raise InvalidTaskError(f"transcription worker artifact contract changed: {name}")
        digest = str(row["sha256"]).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise InvalidTaskError("transcription worker artifact digest is invalid")
        if isinstance(row["byte_size"], bool) or not isinstance(row["byte_size"], int) or row["byte_size"] < 1:
            raise InvalidTaskError("transcription worker artifact size is invalid")
    summary = value["summary"]
    if not isinstance(summary, Mapping) or set(summary) != {
        "profile_id",
        "language",
        "duration_seconds",
        "unit_count",
        "sentence_count",
        "media_sha256",
        "input_fingerprint",
    }:
        raise InvalidTaskError("transcription worker summary is invalid")
    for field in ("profile_id", "language"):
        if not isinstance(summary[field], str) or (
            field == "profile_id" and not summary[field].strip()
        ):
            raise InvalidTaskError(f"transcription worker summary {field} is invalid")
    duration = summary["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or float(duration) < 0
    ):
        raise InvalidTaskError("transcription worker summary duration is invalid")
    for field, minimum in (("unit_count", 1), ("sentence_count", 0)):
        count = summary[field]
        if isinstance(count, bool) or not isinstance(count, int) or count < minimum:
            raise InvalidTaskError(f"transcription worker summary {field} is invalid")
    for field in ("media_sha256", "input_fingerprint"):
        digest = str(summary[field]).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise InvalidTaskError(f"transcription worker summary {field} is invalid")
    return {"schema_version": TRANSCRIPTION_RESULT_SCHEMA, "artifacts": rows, "summary": dict(summary)}


def _safe_public_json(value: Any, field: str) -> None:
    secret_markers = ("api_key", "apikey", "authorization", "credential", "password", "secret", "token")
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered_key = str(key).casefold()
            if any(marker in rendered_key for marker in secret_markers):
                raise InvalidTaskError(f"{field} contains a secret field")
            _safe_public_json(child, f"{field}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _safe_public_json(child, f"{field}[{index}]")
        return
    if isinstance(value, str):
        if re.match(r"^(?:[A-Za-z]:[\\/]|/|\\\\)", value):
            raise InvalidTaskError(f"{field} contains an absolute path")
        if len(value) > 20000:
            raise InvalidTaskError(f"{field} contains oversized text")
        return
    if value is not None and not isinstance(value, (int, float, bool)):
        raise InvalidTaskError(f"{field} contains an unsupported value")


def _validate_provider_audit(value: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "provider",
        "model",
        "input_fingerprint",
        "resumed_remote_task",
        "submitted_body_sha256",
        "public_body",
        "compilation",
    }:
        raise InvalidTaskError("provider submission audit fields are invalid")
    if value["schema_version"] != "substar.provider-submission-audit.v1":
        raise InvalidTaskError("provider submission audit schema is unsupported")
    if not isinstance(value["provider"], str) or not value["provider"].strip():
        raise InvalidTaskError("provider submission audit provider is invalid")
    if not isinstance(value["model"], str) or not value["model"].strip():
        raise InvalidTaskError("provider submission audit model is invalid")
    if value["input_fingerprint"] != request["input_fingerprint"]:
        raise InvalidTaskError("provider submission audit belongs to different input")
    if not isinstance(value["resumed_remote_task"], bool):
        raise InvalidTaskError("provider submission resume audit is invalid")
    for digest_field in ("submitted_body_sha256",):
        digest = str(value[digest_field])
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise InvalidTaskError("provider submission audit digest is invalid")
    compilation = value["compilation"]
    if not isinstance(compilation, Mapping) or set(compilation) != {
        "requested_prompt_sha256",
        "submitted_context_sha256",
        "requested_prompt_characters",
        "hotwords_sha256",
        "submitted_vocabulary",
    }:
        raise InvalidTaskError("provider submission compilation audit is invalid")
    for field in (
        "requested_prompt_sha256",
        "submitted_context_sha256",
        "hotwords_sha256",
    ):
        digest = str(compilation[field])
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise InvalidTaskError("provider submission compilation digest is invalid")
    expected_prompt = hashlib.sha256(request["prompt"].encode("utf-8")).hexdigest()
    if compilation["requested_prompt_sha256"] != expected_prompt:
        raise InvalidTaskError("provider submission prompt audit is invalid")
    if compilation["requested_prompt_characters"] != len(request["prompt"]):
        raise InvalidTaskError("provider submission prompt length audit is invalid")
    vocabulary = compilation["submitted_vocabulary"]
    if not isinstance(vocabulary, Mapping):
        raise InvalidTaskError("provider submission vocabulary audit is invalid")
    expected_vocabulary = {item["text"]: item["weight"] for item in request["hotwords"]}
    if str(value["model"]).startswith("qwen-audio-") and dict(vocabulary) != expected_vocabulary:
        raise InvalidTaskError("provider submission vocabulary does not match task input")
    _safe_public_json(value["public_body"], "provider submission public body")
    return dict(value)


def _resumable_artifact_directory(context: TaskWorkContext, fingerprint: str) -> str | None:
    current_attempt = int(context.task["attempt"])
    for attempt in range(current_attempt - 1, 0, -1):
        candidate = context.attempt_directory.parent / str(attempt) / "artifacts"
        try:
            snapshot = json.loads(
                (candidate / "recognition_request.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if snapshot.get("input_fingerprint") != fingerprint:
            continue
        prepared = candidate / "audio_16k_mono.wav"
        checkpoint = candidate / "ingest_chunks"
        if (prepared.is_file() and prepared.stat().st_size > 44) or checkpoint.is_dir():
            return str(candidate.resolve())
    return None


def _completed_qwen_checkpoint(artifact_directory: str | None, fingerprint: str) -> bool:
    if not artifact_directory:
        return False
    root = Path(artifact_directory)
    try:
        state = json.loads(
            (root / "ingest_chunks" / "qwen_cloud_state.json").read_text(
                encoding="utf-8"
            )
        )
        result = json.loads(
            (root / "ingest_chunks" / "qwen_cloud_result.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(state, Mapping)
        and state.get("input_fingerprint") == fingerprint
        and isinstance(result, Mapping)
    )


def build_transcription_handler(
    projects_root: Path,
    application_root: Path,
) -> TaskHandler:
    projects_root = projects_root.resolve()
    application_root = application_root.resolve()

    def validate_input(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = validate_transcription_request(payload)
        model = str(request["options"].get("qwen_cloud_model", ""))
        if model.startswith("qwen3-asr-flash-filetrans") and request["hotwords"]:
            raise InvalidTaskError(
                "the selected Qwen3 file transcription model does not support weighted hotwords"
            )
        return request

    def prepare(context: TaskWorkContext) -> WorkerLaunch:
        request = validate_transcription_request(context.input_payload)
        project_id = str(context.task.get("project_id") or "")
        project = _contained_project(projects_root, project_id)
        media = _resolved_media(project, request)
        previous_artifact_directory = _resumable_artifact_directory(
            context, request["input_fingerprint"]
        )
        profile_id = request["profile_id"]
        credential_refs = (
            (() if _completed_qwen_checkpoint(
                previous_artifact_directory, request["input_fingerprint"]
            ) else (ASR_QWEN,))
            if profile_id == "qwen_cloud"
            else (() if profile_id != "api" else (ASR_GENERIC,))
        )
        timeout = max(
            600.0,
            float(request["options"].get("stage_timeout_seconds", 3600)),
            float(request["options"].get("qwen_cloud_task_timeout_seconds", 7200)) + 600.0,
        )
        return WorkerLaunch(
            argv=tuple(python_script_command("scripts/run_transcription_worker.py")),
            cwd=application_root,
            project_root=project,
            worker_input={
                "request": request,
                "resolved_media_path": str(media),
                "resume_artifact_directory": previous_artifact_directory,
            },
            credential_refs=credential_refs,
            timeout_seconds=min(timeout, 24 * 3600.0),
        )

    def handle_worker_event(
        _context: TaskWorkContext, message: WorkerMessage
    ) -> Mapping[str, Any]:
        step = str(message.step or "")
        if step not in _PROGRESS_MESSAGES:
            raise InvalidTaskError("transcription worker progress step is unsupported")
        return {
            "progress": float(message.progress or 0.0),
            "message": _PROGRESS_MESSAGES[step],
            "step": step,
            "wait_reason": None,
        }

    def finalize(
        context: TaskWorkContext, completion: WorkerCompletion
    ) -> Mapping[str, Any]:
        result = _validate_worker_result(completion.result)
        request = validate_transcription_request(context.input_payload)
        project = _contained_project(projects_root, str(context.task.get("project_id") or ""))
        rows = result["artifacts"]
        for name, row in rows.items():
            source = (context.artifact_directory / name).resolve()
            if context.artifact_directory.resolve() not in source.parents or not source.is_file():
                raise InvalidTaskError(f"transcription artifact is missing: {name}")
            if source.stat().st_size != row["byte_size"] or sha256_file(source) != row["sha256"]:
                raise InvalidTaskError(f"transcription artifact changed before finalization: {name}")

        try:
            request_artifact = json.loads(
                (context.artifact_directory / "recognition_request.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidTaskError("recognition request artifact is invalid") from exc
        if request_artifact != request:
            raise InvalidTaskError(
                "recognition request artifact does not equal canonical task input"
            )

        try:
            manifest = json.loads(
                (context.artifact_directory / "run_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidTaskError("recognition manifest is invalid") from exc
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != "substar.run.v1":
            raise InvalidTaskError("recognition manifest schema is invalid")
        if manifest.get("source_path") != request["media"]["relative_path"]:
            raise InvalidTaskError("recognition manifest media binding is invalid")
        evidence_link = manifest.get("recognition_evidence")
        if not isinstance(evidence_link, Mapping) or evidence_link != {
            "schema_version": RECOGNITION_EVIDENCE_SCHEMA,
            "relative_path": "recognition_evidence.json",
            "input_fingerprint": request["input_fingerprint"],
        }:
            raise InvalidTaskError("recognition manifest evidence binding is invalid")
        _safe_public_json(manifest, "recognition manifest")

        try:
            provider_audit = _validate_provider_audit(
                json.loads(
                    (
                        context.artifact_directory
                        / "provider_submission_audit.json"
                    ).read_text(encoding="utf-8")
                ),
                request,
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidTaskError("provider submission audit is invalid") from exc

        evidence_path = context.artifact_directory / "recognition_evidence.json"
        evidence = validate_recognition_evidence(
            json.loads(evidence_path.read_text(encoding="utf-8"))
        )
        summary = result["summary"]
        if evidence["media"]["sha256"] != request["media"]["sha256"]:
            raise InvalidTaskError("recognition evidence belongs to different media")
        if evidence["request"]["input_fingerprint"] != request["input_fingerprint"]:
            raise InvalidTaskError("recognition evidence belongs to different task input")
        expected_request_audit = {
            "input_fingerprint": request["input_fingerprint"],
            "profile_id": request["profile_id"],
            "requested_language": request["language"],
            "prompt_sha256": hashlib.sha256(
                request["prompt"].encode("utf-8")
            ).hexdigest(),
            "hotwords_sha256": hashlib.sha256(
                json.dumps(
                    {"items": request["hotwords"]},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "hotword_count": len(request["hotwords"]),
            "provider_submission": {
                "relative_path": "provider_submission_audit.json",
                "sha256": rows["provider_submission_audit.json"]["sha256"],
            },
            "provider_response": (
                {
                    "relative_path": "provider_response.json",
                    "sha256": rows["provider_response.json"]["sha256"],
                }
                if "provider_response.json" in rows
                else None
            ),
        }
        if evidence["request"] != expected_request_audit:
            raise InvalidTaskError(
                "recognition evidence request audit does not match canonical input"
            )
        evidence_model = str(
            evidence["engines"].get("transcript")
            or evidence["engines"].get("profile_id")
        )
        if str(provider_audit["model"]) not in evidence_model:
            raise InvalidTaskError("provider submission model does not match evidence")
        if summary["profile_id"] != request["profile_id"]:
            raise InvalidTaskError("recognition evidence profile summary is invalid")
        if summary["media_sha256"] != request["media"]["sha256"]:
            raise InvalidTaskError("recognition evidence media summary is invalid")
        if summary["input_fingerprint"] != request["input_fingerprint"]:
            raise InvalidTaskError("recognition evidence input summary is invalid")
        if int(summary["unit_count"]) != len(evidence["units"]):
            raise InvalidTaskError("recognition evidence summary unit count is invalid")
        if int(summary["sentence_count"]) != len(evidence["chunks"]):
            raise InvalidTaskError("recognition evidence summary sentence count is invalid")
        if summary["language"] != evidence["language"]:
            raise InvalidTaskError("recognition evidence language summary is invalid")

        # Only this scheduler finalizer publishes into the project directory.
        # The worker owns attempt files and can never overwrite project state.
        for name in rows:
            if name in _PRIVATE_ARTIFACTS:
                continue
            _copy_atomic(context.artifact_directory / name, project / name)
        prompt_source = application_root / "prompts"
        if prompt_source.is_dir():
            shutil.copytree(prompt_source, project / "prompts", dirs_exist_ok=True)

        return {
            "schema_version": TRANSCRIPTION_RESULT_SCHEMA,
            "recognition_evidence": {
                "artifact_type": "recognition_evidence",
                "schema_version": RECOGNITION_EVIDENCE_SCHEMA,
                "sha256": rows["recognition_evidence.json"]["sha256"],
                "byte_size": rows["recognition_evidence.json"]["byte_size"],
            },
            "summary": summary,
            "outputs": {
                "evidence": "recognition_evidence.json",
                "transcript": "master_transcript.txt",
                "alignment_table": "alignment.tsv",
                "segmentation_material": "segmentation_material.json",
            },
        }

    return TaskHandler(
        task_type="transcription",
        validate_input=validate_input,
        handle_worker_event=handle_worker_event,
        prepare=prepare,
        finalize=finalize,
        # The slim production route is Qwen cloud ASR. It prepares media and
        # performs provider I/O but must not consume the local-GPU semaphore.
        resources=("worker", "media_cpu", "provider_io"),
    )
