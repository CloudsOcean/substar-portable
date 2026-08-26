from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from substar_core.recognition.registry import get_recognition_profile
from substar_core.runtime.model import InvalidTaskError


TRANSCRIPTION_INPUT_SCHEMA = "substar.transcription-request.v1"
RECOGNITION_EVIDENCE_SCHEMA = "substar.recognition-evidence.v1"
TRANSCRIPTION_RESULT_SCHEMA = "substar.transcription-result.v1"
RECOGNITION_SOURCE_SCHEMA = "substar.recognition-source.v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)

# These are the non-secret recognition settings whose exact values affect a
# transcription.  Everything else belongs to another stage and must not be
# smuggled into the worker through an untyped settings dictionary.
TRANSCRIPTION_OPTION_KEYS = frozenset(
    {
        "alignment_language",
        "alignment_source",
        "api_auth_mode",
        "api_base_url",
        "api_model",
        "api_provider",
        "api_timeout_seconds",
        "audio_denoise_mode",
        "hf_endpoint",
        "http_retry_attempts",
        "mimo_base64_limit_mb",
        "mimo_boundary_search_seconds",
        "mimo_mp3_bitrate_kbps",
        "mimo_overlap_seconds",
        "mimo_request_target_seconds",
        "mimo_seam_context_seconds",
        "parakeet_device",
        "parakeet_dtype",
        "parakeet_model",
        "qwen_aligner_model",
        "qwen_asr_model",
        "qwen_cloud_base_url",
        "qwen_cloud_model",
        "qwen_cloud_poll_interval_seconds",
        "qwen_cloud_region",
        "qwen_cloud_request_timeout_seconds",
        "qwen_cloud_task_timeout_seconds",
        "qwen_cloud_temporary_upload",
        "stage_timeout_seconds",
        "transcript_source",
        "whisper_beam_size",
        "whisper_compute_type",
        "whisper_device",
        "whisper_model",
        "whisper_vad_filter",
        "whisperx_alignment_model",
        "whisperx_batch_size",
    }
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidTaskError(f"{field} must be a JSON object")
    return {str(key): child for key, child in value.items()}


def _exact_keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extras = sorted(set(value) - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extras:
            detail.append("unsupported " + ", ".join(extras))
        raise InvalidTaskError(f"{field} fields are invalid: {'; '.join(detail)}")


def _text(value: Any, field: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InvalidTaskError(f"{field} must be text")
    rendered = value.strip()
    if not rendered and not allow_empty:
        raise InvalidTaskError(f"{field} must not be empty")
    if len(rendered) > maximum:
        raise InvalidTaskError(f"{field} is too long")
    return rendered


def _safe_relative_path(value: Any, field: str) -> str:
    rendered = _text(value, field, maximum=500).replace("\\", "/")
    path = PurePosixPath(rendered)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise InvalidTaskError(f"{field} must be a contained relative path")
    return path.as_posix()


def _json_value(value: Any, field: str) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > 4000:
            raise InvalidTaskError(f"{field} is too long")
        return value
    raise InvalidTaskError(f"{field} must be a JSON scalar")


def validate_transcription_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = _object(payload, "transcription input")
    _exact_keys(
        value,
        {
            "schema_version",
            "media",
            "profile_id",
            "language",
            "prompt",
            "hotwords",
            "options",
            "input_fingerprint",
        },
        "transcription input",
    )
    if value["schema_version"] != TRANSCRIPTION_INPUT_SCHEMA:
        raise InvalidTaskError("unsupported transcription input schema_version")

    media = _object(value["media"], "media")
    _exact_keys(media, {"relative_path", "original_name", "sha256", "byte_size"}, "media")
    relative_path = _safe_relative_path(media["relative_path"], "media.relative_path")
    original_name = _text(media["original_name"], "media.original_name", maximum=255)
    if PurePosixPath(original_name.replace("\\", "/")).name != original_name:
        raise InvalidTaskError("media.original_name must be a file name")
    digest = _text(media["sha256"], "media.sha256", maximum=64).lower()
    if not _SHA256.fullmatch(digest):
        raise InvalidTaskError("media.sha256 must be a lowercase SHA-256 digest")
    byte_size = media["byte_size"]
    if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 1:
        raise InvalidTaskError("media.byte_size must be a positive integer")

    profile_id = _text(value["profile_id"], "profile_id", maximum=100)
    try:
        get_recognition_profile(profile_id)
    except ValueError as exc:
        raise InvalidTaskError(str(exc)) from exc
    language = _text(value["language"], "language", maximum=40)
    prompt = _text(value["prompt"], "prompt", maximum=20000, allow_empty=True)

    raw_hotwords = value["hotwords"]
    if not isinstance(raw_hotwords, list) or len(raw_hotwords) > 2000:
        raise InvalidTaskError("hotwords must be an array with at most 2000 items")
    hotwords: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_hotwords):
        item = _object(raw, f"hotwords[{index}]")
        _exact_keys(item, {"text", "weight"}, f"hotwords[{index}]")
        text = _text(item["text"], f"hotwords[{index}].text", maximum=300)
        key = text.casefold()
        if key in seen:
            raise InvalidTaskError("hotwords must not contain duplicates")
        seen.add(key)
        weight = item["weight"]
        if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 5:
            raise InvalidTaskError(f"hotwords[{index}].weight must be between 1 and 5")
        hotwords.append({"text": text, "weight": weight})

    raw_options = _object(value["options"], "options")
    unknown = set(raw_options) - TRANSCRIPTION_OPTION_KEYS
    if unknown:
        raise InvalidTaskError(
            "unsupported transcription options: " + ", ".join(sorted(unknown))
        )
    for key in raw_options:
        if any(marker in key.casefold() for marker in _SECRET_MARKERS):
            raise InvalidTaskError("transcription options must not contain credentials")
    options = {
        key: _json_value(raw_options[key], f"options.{key}")
        for key in sorted(raw_options)
    }

    normalized_without_fingerprint = {
        "schema_version": TRANSCRIPTION_INPUT_SCHEMA,
        "media": {
            "relative_path": relative_path,
            "original_name": original_name,
            "sha256": digest,
            "byte_size": byte_size,
        },
        "profile_id": profile_id,
        "language": language,
        "prompt": prompt,
        "hotwords": hotwords,
        "options": options,
    }
    expected_fingerprint = _canonical_hash(normalized_without_fingerprint)
    supplied_fingerprint = _text(
        value["input_fingerprint"], "input_fingerprint", maximum=64
    ).lower()
    if supplied_fingerprint != expected_fingerprint:
        raise InvalidTaskError("transcription input_fingerprint does not match its input")
    return {**normalized_without_fingerprint, "input_fingerprint": expected_fingerprint}


def build_transcription_request(
    *,
    media_path: Path,
    project_directory: Path,
    profile_id: str,
    language: str,
    prompt: str,
    hotwords: Mapping[str, int],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    project_root = project_directory.resolve()
    source = media_path.resolve()
    if project_root not in source.parents or not source.is_file():
        raise InvalidTaskError("media file must exist inside its project directory")
    relative_path = source.relative_to(project_root).as_posix()
    options = {
        key: settings[key]
        for key in sorted(TRANSCRIPTION_OPTION_KEYS)
        if key in settings and settings[key] is not None
    }
    body: dict[str, Any] = {
        "schema_version": TRANSCRIPTION_INPUT_SCHEMA,
        "media": {
            "relative_path": relative_path,
            "original_name": source.name,
            "sha256": sha256_file(source),
            "byte_size": source.stat().st_size,
        },
        "profile_id": str(profile_id),
        "language": str(language or "Auto"),
        "prompt": str(prompt or ""),
        "hotwords": [
            {"text": str(text), "weight": int(weight)}
            for text, weight in hotwords.items()
        ],
        "options": options,
    }
    body["input_fingerprint"] = _canonical_hash(body)
    return validate_transcription_request(body)


def recognition_evidence_from_alignment(
    alignment: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    provider_submission: Mapping[str, Any],
    provider_response: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = copy.deepcopy(_object(alignment, "alignment"))
    if source.get("schema_version") != RECOGNITION_SOURCE_SCHEMA:
        raise InvalidTaskError("worker alignment schema is unsupported")
    source["schema_version"] = RECOGNITION_EVIDENCE_SCHEMA
    source["source_schema_version"] = RECOGNITION_SOURCE_SCHEMA
    source["request"] = {
        "input_fingerprint": request["input_fingerprint"],
        "profile_id": request["profile_id"],
        "requested_language": request["language"],
        "prompt_sha256": hashlib.sha256(request["prompt"].encode("utf-8")).hexdigest(),
        "hotwords_sha256": _canonical_hash({"items": request["hotwords"]}),
        "hotword_count": len(request["hotwords"]),
        "provider_submission": dict(provider_submission),
        "provider_response": (
            dict(provider_response) if provider_response is not None else None
        ),
    }
    return validate_recognition_evidence(source)


def recognition_source_from_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(validate_recognition_evidence(evidence))
    value["schema_version"] = RECOGNITION_SOURCE_SCHEMA
    value.pop("source_schema_version", None)
    value.pop("request", None)
    return value


def validate_recognition_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(_object(payload, "recognition evidence"))
    required = {
        "schema_version",
        "source_schema_version",
        "created_at",
        "media",
        "engines",
        "language",
        "master_text",
        "chunks",
        "units",
        "request",
    }
    _exact_keys(value, required, "recognition evidence")
    if value["schema_version"] != RECOGNITION_EVIDENCE_SCHEMA:
        raise InvalidTaskError("unsupported recognition evidence schema_version")
    if value["source_schema_version"] != RECOGNITION_SOURCE_SCHEMA:
        raise InvalidTaskError("unsupported recognition evidence source schema")
    _text(value["created_at"], "recognition evidence.created_at", maximum=100)
    media = _object(value["media"], "recognition evidence.media")
    digest = str(media.get("sha256", "")).lower()
    if not _SHA256.fullmatch(digest):
        raise InvalidTaskError("recognition evidence media.sha256 is invalid")
    duration = media.get("duration_seconds", 0)
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or float(duration) < 0
    ):
        raise InvalidTaskError("recognition evidence media duration is invalid")
    engines = _object(value["engines"], "recognition evidence.engines")
    if not engines:
        raise InvalidTaskError("recognition evidence engines must not be empty")
    for key, engine_value in engines.items():
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key) is None:
            raise InvalidTaskError("recognition evidence engine name is invalid")
        if engine_value is not None and not isinstance(
            engine_value, (str, int, float, bool)
        ):
            raise InvalidTaskError("recognition evidence engine value is invalid")
        if isinstance(engine_value, str) and len(engine_value) > 500:
            raise InvalidTaskError("recognition evidence engine value is too long")
    _text(value["language"], "recognition evidence.language", maximum=200, allow_empty=True)
    _text(value["master_text"], "recognition evidence.master_text", maximum=50_000_000)
    if not isinstance(value["chunks"], list):
        raise InvalidTaskError("recognition evidence chunks must be an array")
    for index, raw in enumerate(value["chunks"]):
        chunk = _object(raw, f"recognition evidence.chunks[{index}]")
        if chunk.get("index") != index:
            raise InvalidTaskError("recognition evidence chunk indices must be contiguous")
        try:
            start = float(chunk["start"])
            end = float(chunk["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidTaskError("recognition evidence chunk timing is invalid") from exc
        if start < 0 or end < start:
            raise InvalidTaskError("recognition evidence chunk timing is invalid")
        _text(
            chunk.get("text"),
            f"recognition evidence.chunks[{index}].text",
            maximum=1_000_000,
        )
    if not isinstance(value["units"], list) or not value["units"]:
        raise InvalidTaskError("recognition evidence units must be a non-empty array")
    previous_start = -1.0
    for index, raw in enumerate(value["units"]):
        unit = _object(raw, f"recognition evidence.units[{index}]")
        if unit.get("index") != index:
            raise InvalidTaskError("recognition evidence unit indices must be contiguous")
        try:
            start = float(unit["start"])
            end = float(unit["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidTaskError("recognition evidence unit timing is invalid") from exc
        if start < 0 or end < start or start + 0.001 < previous_start:
            raise InvalidTaskError("recognition evidence unit timing is not monotonic")
        _text(unit.get("text"), f"recognition evidence.units[{index}].text", maximum=10000)
        previous_start = start
    request = _object(value["request"], "recognition evidence.request")
    _exact_keys(
        request,
        {
            "input_fingerprint",
            "profile_id",
            "requested_language",
            "prompt_sha256",
            "hotwords_sha256",
            "hotword_count",
            "provider_submission",
            "provider_response",
        },
        "recognition evidence.request",
    )
    for key in ("input_fingerprint", "prompt_sha256", "hotwords_sha256"):
        if not _SHA256.fullmatch(str(request.get(key, ""))):
            raise InvalidTaskError(f"recognition evidence request {key} is invalid")
    if isinstance(request.get("hotword_count"), bool) or not isinstance(
        request.get("hotword_count"), int
    ) or request["hotword_count"] < 0:
        raise InvalidTaskError("recognition evidence hotword_count is invalid")
    _text(request.get("profile_id"), "recognition evidence request profile_id", maximum=100)
    _text(
        request.get("requested_language"),
        "recognition evidence request requested_language",
        maximum=40,
    )

    def validate_artifact_reference(raw: Any, field: str) -> None:
        reference = _object(raw, field)
        _exact_keys(reference, {"relative_path", "sha256"}, field)
        path = _safe_relative_path(reference["relative_path"], f"{field}.relative_path")
        if len(PurePosixPath(path).parts) != 1:
            raise InvalidTaskError(f"{field}.relative_path must be an artifact file")
        if not _SHA256.fullmatch(str(reference.get("sha256", ""))):
            raise InvalidTaskError(f"{field}.sha256 is invalid")

    validate_artifact_reference(
        request.get("provider_submission"),
        "recognition evidence request provider_submission",
    )
    if request.get("provider_response") is not None:
        validate_artifact_reference(
            request["provider_response"],
            "recognition evidence request provider_response",
        )
    if engines.get("profile_id") != request["profile_id"]:
        raise InvalidTaskError("recognition evidence engine profile does not match request")
    return value
