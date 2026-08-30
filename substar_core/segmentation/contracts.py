from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from substar_core.credential_store import model_provider_credential_ref
from substar_core.model_providers import canonical_provider_id, infer_model_provider
from substar_core.model_routing import resolve_stage_request

from substar_core.glossary import normalize_entry
from substar_core.manuscript_matching import reference_break_symbols_for_language
from substar_core.runtime.model import InvalidTaskError


SEGMENTATION_INPUT_SCHEMA = "substar.segmentation-request.v1"
SEGMENTATION_CANDIDATE_SCHEMA = "substar.segmentation-candidate.v1"
SEGMENTATION_VALIDATION_SCHEMA = "substar.segmentation-validation.v1"
SEGMENTATION_MANIFEST_SCHEMA = "substar.segmentation-manifest.v1"
SEGMENTATION_RESULT_SCHEMA = "substar.segmentation-task-result.v1"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^tsk_[0-9a-f]{32}$")
_THINKING = {"enabled", "disabled"}
_EFFORT = {"low", "medium", "high", "max", "xhigh"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> tuple[str, int]:
    """Hash relative names and bytes so a prompt snapshot is portable."""

    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest(), len(files)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _text(value: Any, field: str, *, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidTaskError(f"{field} must be non-empty text")
    return value.strip()


def _digest(value: Any, field: str) -> str:
    rendered = str(value).lower()
    if _DIGEST.fullmatch(rendered) is None:
        raise InvalidTaskError(f"{field} must be a lowercase SHA-256 digest")
    return rendered


def _relative_path(value: Any, field: str, *, depth: int | None = None) -> str:
    raw = _text(value, field, maximum=500).replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or re.match(r"^[A-Za-z]:/", raw) is not None
        or raw.startswith("//")
        or ".." in path.parts
    ):
        raise InvalidTaskError(f"{field} must be a contained relative path")
    if depth is not None and len(path.parts) != depth:
        raise InvalidTaskError(f"{field} has an invalid path depth")
    return path.as_posix()


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTaskError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise InvalidTaskError(f"{field} is outside its supported range")
    return value


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidTaskError(f"{field} must be numeric")
    rendered = float(value)
    if not minimum <= rendered <= maximum:
        raise InvalidTaskError(f"{field} is outside its supported range")
    return rendered


def _model_policy(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "model", "thinking_mode", "reasoning_effort", "max_tokens", "temperature"
    }:
        raise InvalidTaskError(f"{field} fields are invalid")
    thinking = str(value["thinking_mode"])
    effort = str(value["reasoning_effort"])
    if thinking not in _THINKING or effort not in _EFFORT:
        raise InvalidTaskError(f"{field} reasoning policy is invalid")
    return {
        "model": _text(value["model"], f"{field}.model", maximum=300),
        "thinking_mode": thinking,
        "reasoning_effort": effort,
        "max_tokens": _integer(value["max_tokens"], f"{field}.max_tokens", 256, 500000),
        "temperature": _number(value["temperature"], f"{field}.temperature", 0.0, 2.0),
    }


def _normalize_glossary(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 5000:
        raise InvalidTaskError("glossary_snapshot must be an array")
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise InvalidTaskError("glossary_snapshot entries must be objects")
        try:
            entry = normalize_entry(item)
        except ValueError as exc:
            raise InvalidTaskError("glossary_snapshot entry is invalid") from exc
        if not entry["enabled"]:
            raise InvalidTaskError("glossary_snapshot may contain only active entries")
        normalized.append(entry)
    return normalized


def _request_without_fingerprint(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "transcription",
        "source_kind",
        "source_asset_id",
        "mode",
        "language",
        "reference_document",
        "prompt_snapshot",
        "glossary_snapshot",
        "constraints",
        "provider",
        "input_fingerprint",
    }
    if set(value) != required:
        raise InvalidTaskError("segmentation request fields are invalid")
    if value["schema_version"] != SEGMENTATION_INPUT_SCHEMA:
        raise InvalidTaskError("segmentation request schema is unsupported")

    transcription = value["transcription"]
    if not isinstance(transcription, Mapping) or set(transcription) != {
        "task_id", "input_fingerprint", "media_sha256", "evidence_relative_path"
    }:
        raise InvalidTaskError("segmentation transcription binding is invalid")
    task_id = str(transcription["task_id"])
    if _TASK_ID.fullmatch(task_id) is None:
        raise InvalidTaskError("segmentation transcription task_id is invalid")
    normalized_transcription = {
        "task_id": task_id,
        "input_fingerprint": _digest(
            transcription["input_fingerprint"], "transcription.input_fingerprint"
        ),
        "media_sha256": _digest(
            transcription["media_sha256"], "transcription.media_sha256"
        ),
        "evidence_relative_path": _relative_path(
            transcription["evidence_relative_path"],
            "transcription.evidence_relative_path",
            depth=1,
        ),
    }

    source_kind = str(value["source_kind"])
    if source_kind != "asr":
        raise InvalidTaskError("canonical segmentation currently requires ASR evidence")
    mode = str(value["mode"])
    if mode not in {"semantic", "sentence_boundaries", "reference_script"}:
        raise InvalidTaskError("segmentation mode is invalid")

    reference = value["reference_document"]
    normalized_reference: dict[str, Any] | None = None
    if reference is not None:
        if not isinstance(reference, Mapping) or set(reference) != {
            "relative_path", "sha256", "byte_size"
        }:
            raise InvalidTaskError("reference_document fields are invalid")
        normalized_reference = {
            "relative_path": _relative_path(
                reference["relative_path"], "reference_document.relative_path"
            ),
            "sha256": _digest(reference["sha256"], "reference_document.sha256"),
            "byte_size": _integer(
                reference["byte_size"], "reference_document.byte_size", 1, 50 * 1024 * 1024
            ),
        }
    if mode == "reference_script" and normalized_reference is None:
        raise InvalidTaskError("reference_script mode requires a reference document")

    prompt = value["prompt_snapshot"]
    if not isinstance(prompt, Mapping) or set(prompt) != {
        "relative_path", "sha256", "file_count"
    }:
        raise InvalidTaskError("prompt_snapshot fields are invalid")
    normalized_prompt = {
        "relative_path": _relative_path(prompt["relative_path"], "prompt_snapshot.relative_path"),
        "sha256": _digest(prompt["sha256"], "prompt_snapshot.sha256"),
        "file_count": _integer(prompt["file_count"], "prompt_snapshot.file_count", 1, 10000),
    }

    glossary = _normalize_glossary(value["glossary_snapshot"])
    constraints = value["constraints"]
    constraint_fields = {
        "target_seconds",
        "english_hard_limit",
        "chinese_hard_limit",
        "mixed_hard_limit",
        "japanese_hard_limit",
        "korean_hard_limit",
        "sentence_boundary_policy",
        "repair_attempts",
        "request_timeout_seconds",
        "task_timeout_seconds",
    }
    actual_constraint_fields = (
        frozenset(constraints) if isinstance(constraints, Mapping) else frozenset()
    )
    if actual_constraint_fields not in {
        frozenset(constraint_fields),
        frozenset({*constraint_fields, "reference_break_symbols"}),
    }:
        raise InvalidTaskError("segmentation constraints fields are invalid")
    if mode == "reference_script" and "reference_break_symbols" not in actual_constraint_fields:
        raise InvalidTaskError("reference_script mode requires break symbols")
    boundary_policy = str(constraints["sentence_boundary_policy"])
    if boundary_policy not in {"reference", "reconstruct", "unpunctuated"}:
        raise InvalidTaskError("sentence_boundary_policy is invalid")
    normalized_constraints = {
        "target_seconds": _integer(constraints["target_seconds"], "constraints.target_seconds", 30, 600),
        "english_hard_limit": _integer(constraints["english_hard_limit"], "constraints.english_hard_limit", 1, 200),
        "chinese_hard_limit": _integer(constraints["chinese_hard_limit"], "constraints.chinese_hard_limit", 1, 200),
        "mixed_hard_limit": _integer(constraints["mixed_hard_limit"], "constraints.mixed_hard_limit", 1, 200),
        "japanese_hard_limit": _integer(constraints["japanese_hard_limit"], "constraints.japanese_hard_limit", 1, 200),
        "korean_hard_limit": _integer(constraints["korean_hard_limit"], "constraints.korean_hard_limit", 1, 200),
        "sentence_boundary_policy": boundary_policy,
        "repair_attempts": _integer(constraints["repair_attempts"], "constraints.repair_attempts", 0, 4),
        "request_timeout_seconds": _integer(constraints["request_timeout_seconds"], "constraints.request_timeout_seconds", 10, 3600),
        "task_timeout_seconds": _integer(constraints["task_timeout_seconds"], "constraints.task_timeout_seconds", 60, 86400),
    }
    if "reference_break_symbols" in actual_constraint_fields:
        raw_symbols = str(constraints["reference_break_symbols"])
        symbols = "".join(dict.fromkeys(raw_symbols))
        symbols = "".join(symbol for symbol in symbols if not symbol.isspace())
        if not symbols or len(symbols) > 32:
            raise InvalidTaskError("constraints.reference_break_symbols is invalid")
        normalized_constraints["reference_break_symbols"] = symbols

    provider = value["provider"]
    if not isinstance(provider, Mapping) or not set(provider).issubset({
        "id", "base_url", "auth_mode", "grouping", "repair"
    }) or not {"base_url", "grouping", "repair"}.issubset(provider):
        raise InvalidTaskError("segmentation provider fields are invalid")
    auth_mode = str(provider.get("auth_mode", "bearer")).strip().lower()
    if auth_mode not in {"bearer", "api-key"}:
        raise InvalidTaskError("provider.auth_mode is invalid")
    normalized_provider = {
        "base_url": _text(provider["base_url"], "provider.base_url", maximum=500).rstrip("/"),
        "auth_mode": auth_mode,
        "grouping": _model_policy(provider["grouping"], "provider.grouping"),
        "repair": _model_policy(provider["repair"], "provider.repair"),
    }
    if "id" in provider:
        provider_id = canonical_provider_id(provider["id"])
        endpoint_provider = infer_model_provider(normalized_provider["base_url"])
        if endpoint_provider != "custom" and provider_id != endpoint_provider:
            raise InvalidTaskError(
                "provider.id does not own the configured provider.base_url"
            )
        normalized_provider["id"] = provider_id

    return {
        "schema_version": SEGMENTATION_INPUT_SCHEMA,
        "transcription": normalized_transcription,
        "source_kind": source_kind,
        "source_asset_id": _text(value["source_asset_id"], "source_asset_id", maximum=200),
        "mode": mode,
        "language": str(value["language"]).strip()[:40],
        "reference_document": normalized_reference,
        "prompt_snapshot": normalized_prompt,
        "glossary_snapshot": glossary,
        "constraints": normalized_constraints,
        "provider": normalized_provider,
    }


def validate_segmentation_request(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidTaskError("segmentation request must be an object")
    normalized = _request_without_fingerprint(value)
    expected = canonical_sha256(normalized)
    actual = _digest(value.get("input_fingerprint"), "input_fingerprint")
    if actual != expected:
        raise InvalidTaskError("segmentation request fingerprint is invalid")
    return {**normalized, "input_fingerprint": expected}


def build_segmentation_request(
    *,
    transcription_task_id: str,
    transcription_input_fingerprint: str,
    media_sha256: str,
    source_asset_id: str,
    language: str,
    segmentation_enabled: bool,
    reference_document: Mapping[str, Any] | None,
    prompt_snapshot: Mapping[str, Any],
    glossary_snapshot: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    default_model = str(settings.get("translation_api_model") or "deepseek-v4-flash")
    provider_base_url = str(
        settings.get("translation_api_base_url") or "https://api.deepseek.com"
    )
    provider_id = canonical_provider_id(
        settings.get("active_model_provider")
        or infer_model_provider(provider_base_url)
    )

    def policy(prefix: str, *, fallback: str = default_model) -> dict[str, Any]:
        routed = resolve_stage_request(settings, prefix.removeprefix("stage_"))
        return {
            "model": str(routed["model"] or fallback),
            "thinking_mode": str(routed["thinking_mode"]),
            "reasoning_effort": str(routed["reasoning_effort"]),
            "max_tokens": int(routed["max_tokens"]),
            "temperature": float(routed["temperature"]),
        }

    raw = {
        "schema_version": SEGMENTATION_INPUT_SCHEMA,
        "transcription": {
            "task_id": transcription_task_id,
            "input_fingerprint": transcription_input_fingerprint,
            "media_sha256": media_sha256,
            "evidence_relative_path": "recognition_evidence.json",
        },
        "source_kind": "asr",
        "source_asset_id": source_asset_id,
        "mode": (
            "reference_script"
            if bool(settings.get("reference_script_mode", False))
            else "semantic" if segmentation_enabled else "sentence_boundaries"
        ),
        "language": str(language or "Auto"),
        "reference_document": dict(reference_document) if reference_document else None,
        "prompt_snapshot": dict(prompt_snapshot),
        "glossary_snapshot": [dict(item) for item in glossary_snapshot],
        "constraints": {
            "target_seconds": int(settings.get("segmentation_chunk_seconds", 90)),
            "english_hard_limit": int(settings.get("english_hard_limit", 55)),
            "chinese_hard_limit": int(settings.get("chinese_hard_limit", 24)),
            "mixed_hard_limit": int(settings.get("mixed_hard_limit", 25)),
            "japanese_hard_limit": int(settings.get("japanese_hard_limit", 25)),
            "korean_hard_limit": int(settings.get("korean_hard_limit", 32)),
            "sentence_boundary_policy": str(settings.get("sentence_boundary_policy", "unpunctuated")),
            "repair_attempts": 1,
            "request_timeout_seconds": min(int(settings.get("translation_api_timeout_seconds", 300)), 3600),
            "task_timeout_seconds": int(settings.get("stage_timeout_seconds", 3600)),
            **(
                {
                    "reference_break_symbols": str(
                        settings.get("reference_break_symbols")
                        or reference_break_symbols_for_language(language)
                    )
                }
                if bool(settings.get("reference_script_mode", False))
                else {}
            ),
        },
        "provider": {
            "id": provider_id,
            "base_url": provider_base_url,
            "auth_mode": str(settings.get("translation_api_auth_mode") or "bearer"),
            "grouping": policy("stage_segmentation", fallback=default_model),
            "repair": policy("stage_segmentation_repair", fallback=default_model),
        },
        "input_fingerprint": "0" * 64,
    }
    normalized = _request_without_fingerprint(raw)
    return validate_segmentation_request(
        {**normalized, "input_fingerprint": canonical_sha256(normalized)}
    )


def segmentation_credential_ref(provider: Mapping[str, Any]) -> str:
    """Resolve the credential authority frozen into a segmentation request."""

    provider_id = canonical_provider_id(
        provider.get("id") or infer_model_provider(provider.get("base_url"))
    )
    return model_provider_credential_ref(provider_id)


def validate_segmentation_candidate(value: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "input_fingerprint",
        "mode",
        "source",
        "execution_plan",
        "semantic_groups",
        "display_breaks",
        "cues",
        "notices",
        "validation",
        "provenance",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise InvalidTaskError("segmentation candidate fields are invalid")
    if value["schema_version"] != SEGMENTATION_CANDIDATE_SCHEMA:
        raise InvalidTaskError("segmentation candidate schema is unsupported")
    if value["input_fingerprint"] != request["input_fingerprint"]:
        raise InvalidTaskError("segmentation candidate belongs to different input")
    if value["mode"] != request["mode"]:
        raise InvalidTaskError("segmentation candidate mode changed")
    source = value["source"]
    if not isinstance(source, Mapping) or set(source) != {
        "transcription_task_id", "transcription_input_fingerprint", "media_sha256"
    }:
        raise InvalidTaskError("segmentation candidate source binding is invalid")
    expected_source = {
        "transcription_task_id": request["transcription"]["task_id"],
        "transcription_input_fingerprint": request["transcription"]["input_fingerprint"],
        "media_sha256": request["transcription"]["media_sha256"],
    }
    if dict(source) != expected_source:
        raise InvalidTaskError("segmentation candidate source binding changed")
    for field in (
        "semantic_groups", "display_breaks", "cues", "notices",
    ):
        if not isinstance(value[field], list):
            raise InvalidTaskError(f"segmentation candidate {field} must be an array")
    if not isinstance(value["execution_plan"], Mapping):
        raise InvalidTaskError("segmentation candidate execution_plan must be an object")
    if not isinstance(value["validation"], Mapping):
        raise InvalidTaskError("segmentation candidate validation must be an object")
    if not isinstance(value["provenance"], Mapping):
        raise InvalidTaskError("segmentation candidate provenance must be an object")
    return {key: value[key] for key in required}
