from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Mapping

from substar_core.artifacts import atomic_write_json
from substar_core.domain import EditorDocument
from substar_core.manuscript_matching import (
    extract_reference_text,
    materialize_reference_alignment,
    materialize_reference_script,
)
from substar_core.segmentation.input_contract import (
    build_segmentation_material,
    build_segmentation_material_with_display_projection,
)
from substar_core.segmentation.contracts import segmentation_credential_ref
from substar_core.transcription.contracts import (
    recognition_source_from_evidence,
    validate_recognition_evidence,
)
from substar_core.runtime.worker_protocol import (
    WorkerCommand,
    WorkerControl,
    WorkerControlType,
    WorkerMessage,
    WorkerMessageType,
    WorkerTaskType,
    credential_environment_key,
    parse_command_line,
    utc_now,
)
from .contracts import (
    SEGMENTATION_CANDIDATE_SCHEMA,
    SEGMENTATION_INPUT_SCHEMA,
    SEGMENTATION_MANIFEST_SCHEMA,
    SEGMENTATION_RESULT_SCHEMA,
    SEGMENTATION_VALIDATION_SCHEMA,
    canonical_sha256,
    sha256_file,
    sha256_tree,
    validate_segmentation_candidate,
    validate_segmentation_request,
)
from .document_builder import (
    apply_semantic_display_projection,
    attach_semantic_reference_audit,
    build_reference_script_document,
    build_sentence_boundary_document,
    validate_editor_document,
)


class SegmentationAlgorithmError(RuntimeError):
    """A bounded, redacted algorithm failure that is safe for task UI output."""


_ABSOLUTE_PATH_TOKEN = re.compile(r"(?i)(?:[a-z]:)?[^\s\"']*[\\/][^\s\"']+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|authorization|password|secret|token)\s*[:=]\s*\S+"
)


def _public_algorithm_error(stderr_path: Path, return_code: int) -> str:
    try:
        raw = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raw = ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()][-6:]
    rendered = "\n".join(lines)
    rendered = _SECRET_ASSIGNMENT.sub(r"\1=[redacted]", rendered)

    def basename(match: re.Match[str]) -> str:
        token = match.group(0)
        trailing = ""
        while token and token[-1] in ",;:)]}":
            trailing = token[-1] + trailing
            token = token[:-1]
        name = token.replace("\\", "/").rsplit("/", 1)[-1]
        return f"{name}{trailing}"

    rendered = _ABSOLUTE_PATH_TOKEN.sub(basename, rendered).strip()
    if not rendered:
        rendered = f"字幕切分算法退出，返回码 {return_code}"
    return rendered[-1600:]


def semantic_segmentation_arguments(
    *,
    material_path: Path,
    scratch: Path,
    progress_path: Path,
    glossary_path: Path,
    request: dict[str, Any],
) -> list[str]:
    """Return arguments for the in-process semantic algorithm entrypoint.

    This is deliberately not a process command. Passing a process command to
    ``run_semantic_segmentation.main`` would make its parser interpret the
    Python executable or script path as the positional material file.
    """

    constraints = request["constraints"]
    provider = request["provider"]
    grouping = provider["grouping"]
    repair = provider["repair"]
    return [
        str(material_path),
        "--output-dir", str(scratch),
        "--route", "semantic",
        "--base-url", provider["base_url"],
        "--auth-mode", provider.get("auth_mode", "bearer"),
        "--grouping-model", grouping["model"],
        "--grouping-thinking-mode", grouping["thinking_mode"],
        "--grouping-reasoning-effort", grouping["reasoning_effort"],
        "--grouping-max-tokens", str(grouping["max_tokens"]),
        "--grouping-temperature", str(grouping["temperature"]),
        "--repair-attempts", str(constraints["repair_attempts"]),
        "--repair-model", repair["model"],
        "--repair-thinking-mode", repair["thinking_mode"],
        "--repair-reasoning-effort", repair["reasoning_effort"],
        "--repair-max-tokens", str(repair["max_tokens"]),
        "--repair-temperature", str(repair["temperature"]),
        "--target-seconds", str(constraints["target_seconds"]),
        "--english-hard-limit", str(constraints["english_hard_limit"]),
        "--chinese-hard-limit", str(constraints["chinese_hard_limit"]),
        "--mixed-hard-limit", str(constraints["mixed_hard_limit"]),
        "--japanese-hard-limit", str(constraints["japanese_hard_limit"]),
        "--korean-hard-limit", str(constraints["korean_hard_limit"]),
        "--target-hard-limit", str(constraints["chinese_hard_limit"]),
        "--source-language", request["language"],
        "--timeout", str(constraints["request_timeout_seconds"]),
        "--progress-file", str(progress_path),
        "--source-kind", "asr",
        "--source-asset-id", request["source_asset_id"],
        "--sentence-boundary-policy", constraints["sentence_boundary_policy"],
        "--glossary-snapshot", str(glossary_path),
        "--candidate-only",
    ]


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
        "substar.editor-document.v1",
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


class SegmentationCancelled(InterruptedError):
    pass


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _effective_material(
    evidence: Mapping[str, Any], reference_path: Path | None, request: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    alignment = recognition_source_from_evidence(evidence)
    master = str(evidence["master_text"]).strip()
    audit: dict[str, Any] = {
        "schema_version": "substar.reference-match.v1",
        "applied": False,
        "reference_sha256": None,
        "report": {},
    }
    if reference_path is not None:
        reference_text = extract_reference_text(
            reference_path.read_bytes(), reference_path.name
        )
        if request["mode"] == "reference_script":
            material, breaks, raw_audit = materialize_reference_script(
                reference_text,
                alignment.get("units", []),
                str(request["constraints"]["reference_break_symbols"]),
                str(request.get("language") or "Auto"),
            )
            alignment = {
                **alignment,
                "units": list(material["units"]),
                "reference_script_breaks": breaks,
            }
        else:
            master, alignment, raw_audit = materialize_reference_alignment(
                reference_text,
                alignment,
                str(request.get("language") or "Auto"),
            )
        audit = {
            "schema_version": "substar.reference-match.v1",
            "applied": True,
            "reference_sha256": sha256_file(reference_path),
            "report": raw_audit,
        }
        if request["mode"] == "reference_script":
            return material, alignment, audit, []
        material, display_projection = (
            build_segmentation_material_with_display_projection(master, alignment)
        )
        return material, alignment, audit, display_projection
    return build_segmentation_material(master, alignment), alignment, audit, []


def _semantic_candidate(
    request: Mapping[str, Any],
    scratch: Path,
    reference_audit: Mapping[str, Any],
    display_projection: list[Mapping[str, Any]],
) -> tuple[dict[str, Any], EditorDocument, dict[str, Any]]:
    raw_result = _json_object(
        scratch / "segmentation_algorithm_result.json", "algorithm result"
    )
    planning = _json_object(
        scratch / "execution_block_plan.json", "algorithm planning"
    )
    raw_document = _json_object(
        scratch / "editor_document_candidate.json", "algorithm editor candidate"
    )
    document = validate_editor_document(EditorDocument.from_dict(raw_document))
    if bool(reference_audit.get("applied")):
        document = apply_semantic_display_projection(document, display_projection)
        report = reference_audit.get("report")
        if isinstance(report, Mapping):
            document = attach_semantic_reference_audit(document, report)
    notices = []
    for row in raw_result.get("exceptions", []):
        if not isinstance(row, Mapping):
            continue
        notices.append(
            {
                "code": str(row.get("code") or "segmentation_review_required"),
                "block_id": str(row.get("block_id") or row.get("chunk") or ""),
                "alignment_start": row.get("alignment_start"),
                "alignment_end": row.get("alignment_end"),
                "detail": str(row.get("detail") or "该字幕范围需要人工审阅。"),
            }
        )
    validation = {
        "schema_version": SEGMENTATION_VALIDATION_SCHEMA,
        "status": "accepted_with_review" if notices else "accepted",
        "cue_count": len(document.cues),
        "source_token_count": len(document.source_tokens),
        "review_required_count": len(notices),
        "document_sha256": document.content_hash(),
    }
    provenance = raw_result.get("provenance", {})
    provenance = provenance if isinstance(provenance, Mapping) else {}
    models = provenance.get("models", {})
    models = models if isinstance(models, Mapping) else {}
    def prompt_audit(value: Any, role: str) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        return {
            "role": role,
            "version": str(value.get("version") or "1"),
            "sha256": str(value.get("sha256") or ""),
        }

    candidate = {
        "schema_version": SEGMENTATION_CANDIDATE_SCHEMA,
        "input_fingerprint": request["input_fingerprint"],
        "mode": request["mode"],
        "source": {
            "transcription_task_id": request["transcription"]["task_id"],
            "transcription_input_fingerprint": request["transcription"]["input_fingerprint"],
            "media_sha256": request["transcription"]["media_sha256"],
        },
        "execution_plan": {
            "target_seconds": int(planning.get("target_seconds", 0)),
            "boundaries_after": list(planning.get("boundaries_after", [])),
            "blocks": list(planning.get("blocks", [])),
            "skipped_reason": planning.get("skipped_reason"),
        },
        "semantic_groups": list(raw_result.get("meaning_groups", [])),
        "display_breaks": list(raw_result.get("display_breaks", [])),
        "cues": list(raw_result.get("cues", [])),
        "notices": notices,
        "validation": validation,
        "provenance": {
            "source_language": provenance.get("source_language"),
            "sentence_boundary_policy": provenance.get("sentence_boundary_policy"),
            "prompt": prompt_audit(
                provenance.get("semantic_grouping_prompt"), "semantic_grouping"
            ),
            "repair_prompt": prompt_audit(
                provenance.get("semantic_grouping_repair_prompt"),
                "semantic_grouping_repair"
            ),
            "models": {
                "execution_planning": models.get("execution_planning"),
                "semantic_grouping": models.get("semantic_grouping"),
                "semantic_grouping_repair": models.get("semantic_grouping_repair"),
            },
            "api_call_count": len(provenance.get("api_calls", []))
            if isinstance(provenance.get("api_calls"), list)
            else 0,
        },
    }
    return candidate, document, validation


def _boundary_candidate(
    request: Mapping[str, Any], evidence: Mapping[str, Any]
) -> tuple[dict[str, Any], EditorDocument, dict[str, Any]]:
    document = build_sentence_boundary_document(
        evidence, source_asset_id=str(request["source_asset_id"])
    )
    lineage = next(
        change
        for change in document.changes
        if change.operation == "build_from_segmentation"
    )
    layout = dict(lineage.metadata.get("layout", {}))
    cue_lineage = dict(lineage.metadata.get("cue_lineage", {}))
    validation = {
        "schema_version": SEGMENTATION_VALIDATION_SCHEMA,
        "status": "accepted",
        "cue_count": len(document.cues),
        "source_token_count": len(document.source_tokens),
        "review_required_count": 0,
        "document_sha256": document.content_hash(),
    }
    candidate = {
        "schema_version": SEGMENTATION_CANDIDATE_SCHEMA,
        "input_fingerprint": request["input_fingerprint"],
        "mode": request["mode"],
        "source": {
            "transcription_task_id": request["transcription"]["task_id"],
            "transcription_input_fingerprint": request["transcription"]["input_fingerprint"],
            "media_sha256": request["transcription"]["media_sha256"],
        },
        "execution_plan": {
            "target_seconds": request["constraints"]["target_seconds"],
            "boundaries_after": [],
            "blocks": [],
            "skipped_reason": "semantic_segmentation_disabled",
        },
        "semantic_groups": [],
        "display_breaks": list(layout.get("display_breaks", [])),
        "cues": [
            {
                "cue_id": cue.cue_id,
                "alignment_start": int(cue_lineage[cue.cue_id]["source_indexes"][0]),
                "alignment_end": int(cue_lineage[cue.cue_id]["source_indexes"][1]),
            }
            for cue in document.cues
        ],
        "notices": [],
        "validation": validation,
        "provenance": {
            "source_language": evidence.get("language"),
            "sentence_boundary_policy": "recognizer_boundaries",
            "prompt": None,
            "repair_prompt": None,
            "models": {
                "execution_planning": "recognizer_boundaries",
                "semantic_grouping": None,
                "semantic_grouping_repair": None,
            },
            "api_call_count": 0,
        },
    }
    return candidate, document, validation


def _reference_script_candidate(
    request: Mapping[str, Any],
    material: Mapping[str, Any],
    alignment: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> tuple[dict[str, Any], EditorDocument, dict[str, Any]]:
    display_breaks = [int(value) for value in alignment.get("reference_script_breaks", [])]
    document = build_reference_script_document(
        material,
        source_asset_id=str(request["source_asset_id"]),
        display_breaks=display_breaks,
        reference_report=dict(audit.get("report", {})),
    )
    lineage = next(
        change
        for change in document.changes
        if change.operation == "build_from_segmentation"
    )
    del lineage
    source_index_by_id = {
        token.token_id: token.index for token in document.source_tokens
    }
    display_by_id = {token.token_id: token for token in document.display_tokens}

    def cue_source_range(cue: Any) -> tuple[int, int]:
        indexes = sorted(
            {
                source_index_by_id[source_id]
                for display_id in cue.display_token_ids
                for source_id in display_by_id[display_id].source_token_ids
                if source_id in source_index_by_id
            }
        )
        if not indexes:
            raise ValueError(f"reference cue has no ASR timing lineage: {cue.cue_id}")
        return indexes[0], indexes[-1]

    cue_ranges = {
        cue.cue_id: cue_source_range(cue) for cue in document.cues
    }
    report = audit.get("report", {})
    quality = str(report.get("quality") or "failed")
    notices = []
    if quality != "good":
        notices.append(
            {
                "code": f"reference_alignment_{quality}",
                "block_id": "reference-script",
                "alignment_start": 0,
                "alignment_end": len(material["units"]) - 1,
                "detail": (
                    "参考稿与听写的对齐置信度较低，时间边界需要在编辑器中检查。"
                ),
            }
        )
    validation = {
        "schema_version": SEGMENTATION_VALIDATION_SCHEMA,
        "status": "accepted" if quality == "good" else "accepted_with_review",
        "cue_count": len(document.cues),
        "source_token_count": len(document.source_tokens),
        "review_required_count": len(notices),
        "document_sha256": document.content_hash(),
    }
    candidate = {
        "schema_version": SEGMENTATION_CANDIDATE_SCHEMA,
        "input_fingerprint": request["input_fingerprint"],
        "mode": request["mode"],
        "source": {
            "transcription_task_id": request["transcription"]["task_id"],
            "transcription_input_fingerprint": request["transcription"]["input_fingerprint"],
            "media_sha256": request["transcription"]["media_sha256"],
        },
        "execution_plan": {
            "target_seconds": request["constraints"]["target_seconds"],
            "boundaries_after": [],
            "blocks": [],
            "skipped_reason": "reference_script_deterministic",
        },
        "semantic_groups": [],
        "display_breaks": [
            cue_ranges[cue.cue_id][1] for cue in document.cues[:-1]
        ],
        "cues": [
            {
                "cue_id": cue.cue_id,
                "alignment_start": cue_ranges[cue.cue_id][0],
                "alignment_end": cue_ranges[cue.cue_id][1],
            }
            for cue in document.cues
        ],
        "notices": notices,
        "validation": validation,
        "provenance": {
            "source_language": request.get("language"),
            "sentence_boundary_policy": "reference_primary_asr_timing",
            "break_symbols": request["constraints"]["reference_break_symbols"],
            "alignment_quality": quality,
            "prompt": None,
            "repair_prompt": None,
            "models": {
                "execution_planning": "reference_script_deterministic",
                "semantic_grouping": None,
                "semantic_grouping_repair": None,
            },
            "api_call_count": 0,
        },
    }
    return candidate, document, validation


def _recover_accepted_result(
    source_directory: Path,
    artifact_directory: Path,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    source_directory = source_directory.resolve()
    if not source_directory.is_dir():
        raise ValueError("segmentation recovery artifacts are unavailable")
    prior_request = _json_object(
        source_directory / "segmentation_request.json", "recovery request"
    )
    if prior_request != request:
        raise ValueError("segmentation recovery artifacts belong to another input")
    validation = _json_object(
        source_directory / "segmentation_validation.json", "recovery validation"
    )
    if validation.get("schema_version") != SEGMENTATION_VALIDATION_SCHEMA or validation.get(
        "status"
    ) not in {"accepted", "accepted_with_review"}:
        raise ValueError("segmentation recovery artifacts were not accepted")
    document = validate_editor_document(
        EditorDocument.from_dict(
            _json_object(
                source_directory / "editor_document_candidate.json",
                "recovery editor candidate",
            )
        )
    )
    candidate = validate_segmentation_candidate(
        _json_object(
            source_directory / "segmentation_candidate.json",
            "recovery segmentation candidate",
        ),
        request,
    )
    digest = document.content_hash()
    if (
        validation.get("document_sha256") != digest
        or candidate["validation"].get("document_sha256") != digest
    ):
        raise ValueError("segmentation recovery document changed after validation")
    for name in _ARTIFACT_CONTRACTS:
        source = (source_directory / name).resolve()
        if source_directory not in source.parents or not source.is_file():
            raise ValueError(f"segmentation recovery artifact is missing: {name}")
        temporary = artifact_directory / f".{name}.recovering"
        shutil.copyfile(source, temporary)
        os.replace(temporary, artifact_directory / name)
    rows = []
    for name, (artifact_type, schema_version) in _ARTIFACT_CONTRACTS.items():
        path = artifact_directory / name
        rows.append(
            {
                "artifact_type": artifact_type,
                "relative_path": name,
                "schema_version": schema_version,
                "sha256": sha256_file(path),
                "byte_size": path.stat().st_size,
            }
        )
    return {
        "schema_version": SEGMENTATION_RESULT_SCHEMA,
        "artifacts": rows,
        "summary": {
            "mode": request["mode"],
            "cue_count": len(document.cues),
            "source_token_count": len(document.source_tokens),
            "review_required_count": int(validation["review_required_count"]),
            "input_fingerprint": request["input_fingerprint"],
            "document_sha256": digest,
        },
    }


def run(command: WorkerCommand) -> int:
    if command.task_type is not WorkerTaskType.SEGMENTATION:
        raise ValueError("worker command task_type must be segmentation")
    payload = command.input
    if not isinstance(payload, dict) or set(payload) != {
        "request",
        "resolved_evidence_path",
        "resolved_reference_path",
        "resolved_prompt_root",
        "application_root",
        "resolved_recovery_artifact_directory",
    }:
        raise ValueError("segmentation worker input is invalid")
    request = validate_segmentation_request(payload["request"])
    artifact_directory = Path(command.paths.artifact_directory).resolve()
    work_directory = Path(command.paths.work_directory).resolve()
    evidence_path = Path(str(payload["resolved_evidence_path"])).resolve()
    reference_path = (
        Path(str(payload["resolved_reference_path"])).resolve()
        if payload["resolved_reference_path"]
        else None
    )
    prompt_root = Path(str(payload["resolved_prompt_root"])).resolve()
    application_root = Path(str(payload["application_root"])).resolve()
    recovery_artifact_directory = (
        Path(str(payload["resolved_recovery_artifact_directory"])).resolve()
        if payload["resolved_recovery_artifact_directory"]
        else None
    )
    artifact_directory.mkdir(parents=True, exist_ok=True)
    work_directory.mkdir(parents=True, exist_ok=True)
    cancelled = threading.Event()
    protocol_stdout = sys.stdout
    emit_lock = threading.Lock()
    sequence = 0

    def emit(
        message_type: WorkerMessageType,
        data: dict[str, Any] | None = None,
        *,
        progress: float | None = None,
        step: str | None = None,
    ) -> None:
        nonlocal sequence
        with emit_lock:
            sequence += 1
            protocol_stdout.write(
                WorkerMessage(
                    task_id=command.task_id,
                    attempt=command.attempt,
                    sequence=sequence,
                    message_type=message_type,
                    occurred_at=utc_now(),
                    progress=progress,
                    step=step,
                    data=data or {},
                ).to_json_line()
            )
            protocol_stdout.flush()

    def read_controls() -> None:
        for line in sys.stdin:
            try:
                control = parse_command_line(line)
            except Exception:
                continue
            if (
                isinstance(control, WorkerControl)
                and control.task_id == command.task_id
                and control.attempt == command.attempt
                and control.control_type
                in {WorkerControlType.CANCEL, WorkerControlType.SHUTDOWN}
            ):
                cancelled.set()
                return

    threading.Thread(target=read_controls, daemon=True).start()
    emit(WorkerMessageType.READY, {"worker": "segmentation"})
    try:
        if recovery_artifact_directory is not None:
            emit(
                WorkerMessageType.PROGRESS,
                {"message": "Reusing the accepted segmentation result"},
                progress=0.96,
                step="segmentation.validation",
            )
            recovered = _recover_accepted_result(
                recovery_artifact_directory, artifact_directory, request
            )
            for row in recovered["artifacts"]:
                emit(WorkerMessageType.ARTIFACT, dict(row))
            emit(
                WorkerMessageType.PROGRESS,
                {"message": "Building the initial editor document"},
                progress=0.98,
                step="segmentation.document_build",
            )
            emit(WorkerMessageType.RESULT, recovered)
            return 0
        atomic_write_json(artifact_directory / "segmentation_request.json", request)
        evidence = validate_recognition_evidence(_json_object(evidence_path, "recognition evidence"))
        if evidence["request"]["input_fingerprint"] != request["transcription"]["input_fingerprint"]:
            raise ValueError("recognition evidence belongs to a different transcription input")
        if evidence["media"]["sha256"] != request["transcription"]["media_sha256"]:
            raise ValueError("recognition evidence belongs to different media")
        prompt_sha256, prompt_count = sha256_tree(prompt_root)
        if (
            prompt_sha256 != request["prompt_snapshot"]["sha256"]
            or prompt_count != request["prompt_snapshot"]["file_count"]
        ):
            raise ValueError("segmentation prompt snapshot changed")
        if reference_path is not None:
            reference = request["reference_document"]
            if reference is None:
                raise ValueError("unexpected reference document")
            if (
                not reference_path.is_file()
                or reference_path.stat().st_size != reference["byte_size"]
                or sha256_file(reference_path) != reference["sha256"]
            ):
                raise ValueError("reference document changed")

        emit(
            WorkerMessageType.PROGRESS,
            {"message": "Preparing immutable recognition input"},
            progress=0.08,
            step="segmentation.input_prepare",
        )
        material, effective_alignment, reference_audit, display_projection = _effective_material(
            evidence, reference_path, request
        )
        material_path = work_directory / "segmentation_material.json"
        atomic_write_json(material_path, material)
        atomic_write_json(artifact_directory / "reference_match.json", reference_audit)
        if cancelled.is_set():
            raise SegmentationCancelled()

        mock_semantic = os.environ.get("SUBSTAR_MOCK_SEGMENTATION", "") == "1"
        if request["mode"] == "semantic" and not mock_semantic:
            secret = os.environ.pop(
                credential_environment_key(segmentation_credential_ref(request["provider"])), ""
            ).strip()
            if not secret:
                raise ValueError("segmentation provider credential is unavailable")
            scratch = work_directory / "algorithm"
            scratch.mkdir(parents=True, exist_ok=True)
            glossary_path = work_directory / "glossary_snapshot.json"
            progress_path = work_directory / "algorithm_progress.json"
            atomic_write_json(glossary_path, request["glossary_snapshot"])
            argv = semantic_segmentation_arguments(
                material_path=material_path,
                scratch=scratch,
                progress_path=progress_path,
                glossary_path=glossary_path,
                request=request,
            )
            stdout_path = work_directory / "algorithm.stdout.log"
            stderr_path = work_directory / "algorithm.stderr.log"
            # The canonical supervisor already owns this worker process and
            # its timeout/cancellation boundary. Starting another Python
            # interpreter here created an unnecessary nested-process startup
            # failure on Windows. Run the pure algorithm entrypoint in-process
            # and keep its legacy console output isolated from JSONL stdout.
            from scripts.run_semantic_segmentation import main as run_segmentation

            previous_key = os.environ.get("SUBSTAR_MODEL_API_KEY")
            previous_prompt_root = os.environ.get("SUBSTAR_PROMPT_ROOT")
            last_semantic_report: tuple[int, int, int, int, int] | None = None

            def report_semantic_progress(snapshot: dict[str, Any]) -> None:
                nonlocal last_semantic_report
                stages = snapshot.get("stages")
                if not isinstance(stages, Mapping):
                    return
                row = stages.get("semantic_grouping")
                if not isinstance(row, Mapping):
                    return
                planned = max(0, int(row.get("planned", 0)))
                if planned <= 0:
                    return
                responses = min(planned, max(0, int(row.get("responses", 0))))
                accepted = max(0, int(row.get("accepted", 0)))
                failed = max(0, int(row.get("failed", 0)))
                completed = min(planned, accepted + failed)
                blocks = row.get("blocks")
                repairing = sum(
                    1
                    for block in (blocks.values() if isinstance(blocks, Mapping) else [])
                    if isinstance(block, Mapping) and block.get("status") == "repairing"
                )
                signature = (planned, responses, completed, repairing, failed)
                if signature == last_semantic_report:
                    return
                last_semantic_report = signature
                # In the project projection 0.230769 maps to 50%, while
                # 0.923077 maps to 95%. Reserve the remainder for validation,
                # document construction and atomic publication.
                value = 0.230769 + 0.692308 * (completed / planned)
                emit(
                    WorkerMessageType.PROGRESS,
                    {
                        "message": "语义切分分块处理中",
                        "planned": planned,
                        "responses": responses,
                        "completed": completed,
                        "repairing": repairing,
                        "failed": failed,
                    },
                    progress=value,
                    step="segmentation.semantic_grouping",
                )

            os.environ["SUBSTAR_MODEL_API_KEY"] = secret
            os.environ["SUBSTAR_PROMPT_ROOT"] = str(prompt_root)
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr:
                emit(
                    WorkerMessageType.PROGRESS,
                    {"message": "正在初始化语义切分"},
                    progress=0.230769,
                    step="segmentation.semantic_grouping",
                )
                try:
                    try:
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            return_code = run_segmentation(
                                argv, progress_callback=report_semantic_progress
                            )
                    except SystemExit as exc:
                        return_code = exc.code if isinstance(exc.code, int) else 1
                    except Exception as exc:
                        print(f"{type(exc).__name__}: {exc}", file=stderr, flush=True)
                        raise SegmentationAlgorithmError(
                            _public_algorithm_error(stderr_path, 1)
                        ) from exc
                finally:
                    if previous_key is None:
                        os.environ.pop("SUBSTAR_MODEL_API_KEY", None)
                    else:
                        os.environ["SUBSTAR_MODEL_API_KEY"] = previous_key
                    if previous_prompt_root is None:
                        os.environ.pop("SUBSTAR_PROMPT_ROOT", None)
                    else:
                        os.environ["SUBSTAR_PROMPT_ROOT"] = previous_prompt_root
            if return_code:
                raise SegmentationAlgorithmError(
                    _public_algorithm_error(stderr_path, int(return_code))
                )
            candidate, document, validation = _semantic_candidate(
                request, scratch, reference_audit, display_projection
            )
        elif request["mode"] == "reference_script":
            emit(
                WorkerMessageType.PROGRESS,
                {"message": "Aligning the reference script and applying symbol boundaries"},
                progress=0.55,
                step="segmentation.cue_layout",
            )
            candidate, document, validation = _reference_script_candidate(
                request, material, effective_alignment, reference_audit
            )
        else:
            emit(
                WorkerMessageType.PROGRESS,
                {"message": "Applying recognition sentence boundaries"},
                progress=0.55,
                step="segmentation.cue_layout",
            )
            candidate, document, validation = _boundary_candidate(
                request,
                {**evidence, "units": list(material["units"])},
            )

        if cancelled.is_set():
            raise SegmentationCancelled()
        emit(
            WorkerMessageType.PROGRESS,
            {"message": "Validating editor document candidate"},
            progress=0.95,
            step="segmentation.validation",
        )
        document.validate()
        validate_segmentation_candidate(candidate, request)
        atomic_write_json(artifact_directory / "segmentation_candidate.json", candidate)
        atomic_write_json(
            artifact_directory / "editor_document_candidate.json", document.to_dict()
        )
        atomic_write_json(
            artifact_directory / "segmentation_validation.json", validation
        )
        manifest = {
            "schema_version": SEGMENTATION_MANIFEST_SCHEMA,
            "input_fingerprint": request["input_fingerprint"],
            "source": candidate["source"],
            "prompt_snapshot_sha256": request["prompt_snapshot"]["sha256"],
            "glossary_snapshot_sha256": canonical_sha256(
                request["glossary_snapshot"]
            ),
            "candidate": "segmentation_candidate.json",
            "editor_document_candidate": "editor_document_candidate.json",
            "validation": "segmentation_validation.json",
        }
        atomic_write_json(artifact_directory / "segmentation_manifest.json", manifest)

        artifact_rows: list[dict[str, Any]] = []
        for name, (artifact_type, schema_version) in _ARTIFACT_CONTRACTS.items():
            path = artifact_directory / name
            row = {
                "artifact_type": artifact_type,
                "relative_path": name,
                "schema_version": schema_version,
                "sha256": sha256_file(path),
                "byte_size": path.stat().st_size,
            }
            artifact_rows.append(row)
            emit(WorkerMessageType.ARTIFACT, row)
        emit(
            WorkerMessageType.PROGRESS,
            {"message": "Publishing validated segmentation artifacts"},
            progress=0.98,
            step="segmentation.document_build",
        )
        emit(
            WorkerMessageType.RESULT,
            {
                "schema_version": SEGMENTATION_RESULT_SCHEMA,
                "artifacts": artifact_rows,
                "summary": {
                    "mode": request["mode"],
                    "cue_count": len(document.cues),
                    "source_token_count": len(document.source_tokens),
                    "review_required_count": validation["review_required_count"],
                    "input_fingerprint": request["input_fingerprint"],
                    "document_sha256": document.content_hash(),
                },
            },
        )
        return 0
    except SegmentationCancelled:
        emit(WorkerMessageType.CANCELLED, {"reason": "requested"})
        return 0
    except BaseException as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        error = {"code": "segmentation_worker_failed"}
        if isinstance(exc, SegmentationAlgorithmError):
            error["public_message"] = str(exc)
        emit(WorkerMessageType.ERROR, error)
        return 1


def main() -> int:
    _configure_stdio()
    command = parse_command_line(sys.stdin.readline())
    if not isinstance(command, WorkerCommand):
        return 90
    return run(command)
