from __future__ import annotations

import concurrent.futures
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping
import time
from pydantic import BaseModel, Field
from substar_core.artifacts import atomic_write_json
from substar_core.ai_progress import ai_progress
from substar_core.ai_block_cache import fingerprint, load_ai_block_cache, save_ai_block_cache
from substar_core.model_routing import resolve_stage_request
from substar_core.config import load_settings
from substar_core.glossary import active_glossary, glossary_prompt
from substar_core.prompt_registry import calibration_variant, normalize_source_language, render_prompt, source_language_for_text
from substar_core.model_gateway import ModelGatewayError, ModelGatewayRequestError, call_text_model, call_translation_model
from substar_core.cue_script import finalize_calibration_candidate, output_contract, render_cue_request
from substar_core.domain import ChangeKind, ChangeProvenance, EditorDocument
from substar_core.document_operations import DocumentOperationError, apply_document_operation
from substar_core.storage import ProjectStore
from substar_core.task_info import load_task_info
from substar_core.prompt_registry import frozen_prompt
from substar_core.editor.application.publication import write_candidate

_CALIBRATION_PUNCTUATION = ".,?!;:，。？！；：、…-—–"

_CALIBRATION_ACTION_FIELDS = {
    "action_id", "kind", "token_ids", "before_text", "after_text",
    "confidence", "evidence", "disposition", "affects_translation",
}

_CALIBRATION_EVIDENCE_KINDS = {
    "glossary", "reference_document", "document_consistency", "context",
    "user_instruction",
}

class BatchReplacement(BaseModel):
    token_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    expected_text: str | None = None

class AiCalibrationRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1)
    instruction: str = Field(default="", max_length=4000)

def _revision_id(revision: Any) -> str:
    """Read the id from either a domain revision or an API payload.

    Internal save endpoints intentionally return the serialized payload, while
    storage returns a ``DocumentRevision``.  Keeping this boundary explicit
    prevents post-save audit code from mixing the two representations.
    """
    value = revision.get("revision_id") if isinstance(revision, Mapping) else getattr(revision, "revision_id", "")
    value = str(value or "").strip()
    if not value:
        raise ValueError("revision payload is missing revision_id")
    return value

def _editor_ai_cues(revision: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    token_map = {
        token.token_id: token
        for token in revision.document.display_tokens
        if token.state.value == "active"
    }
    group_map = {group.group_id: group for group in revision.document.groups}
    cues: list[dict[str, Any]] = []
    for cue in revision.document.cues:
        if cue.state.value != "active":
            continue
        token_ids = [token_id for token_id in cue.display_token_ids if token_id in token_map]
        group = group_map.get(cue.group_id or "")
        mapping = cue.mapping if isinstance(cue.mapping, Mapping) else {}
        target_metadata = (
            cue.target.provenance.metadata
            if cue.target is not None and isinstance(cue.target.provenance.metadata, Mapping)
            else {}
        )
        cues.append({
            "cue_id": cue.cue_id,
            "start": cue.start,
            "end": cue.end,
            "group_id": cue.group_id,
            "group_origin": group.origin if group else None,
            "execution_block_ids": list(group.execution_block_ids) if group else [],
            "group_dirty_flags": list(group.dirty_flags) if group else [],
            "group_migration_confidence": group.migration_confidence if group else None,
            "tokens": [
                {"token_id": token_id, "text": token_map[token_id].text}
                for token_id in token_ids
            ],
            "target_text": cue.target.target_text if cue.target else "",
            "translation_mapping": {
                "mapping_type": mapping.get("mapping_type"),
                "group_mapping_type": mapping.get("group_mapping_type"),
                "source_cue_ids": list(mapping.get("source_cue_ids", [])),
                "source_evidence_cue_ids": list(
                    mapping.get(
                        "source_evidence_cue_ids",
                        target_metadata.get("source_evidence_cue_ids", []),
                    )
                ),
                "meaning_unit_id": mapping.get(
                    "meaning_unit_id", target_metadata.get("meaning_unit_id")
                ),
            } if cue.target else None,
        })
    return token_map, cues

def _editor_ai_blocks(cues: list[dict[str, Any]], *, halo: int = 3) -> dict[str, list[dict[str, Any]]]:
    owners: dict[str, list[int]] = {}
    for index, cue in enumerate(cues):
        inherited = [
            str(value)
            for value in cue.get("execution_block_ids", [])
            if str(value)
        ]
        owners.setdefault(inherited[0] if inherited else "manual", []).append(index)
    blocks: dict[str, list[dict[str, Any]]] = {}
    for owner, indexes in owners.items():
        first = max(0, indexes[0] - halo)
        last = min(len(cues), indexes[-1] + halo + 1)
        owned = set(indexes)
        blocks[owner] = [
            {**cues[index], "editable": index in owned}
            for index in range(first, last)
        ]
    return blocks

def _run_editor_ai_blocks(
    *,
    settings: Mapping[str, Any],
    system_prompt: str,
    repair_system_prompt: str | None = None,
    blocks: Mapping[str, list[dict[str, Any]]],
    failure_key: str,
    stage_name: str,
    retry_stage: str | None = "audit_repair",
    response_validator: Any | None = None,
    progress_callback: Any | None = None,
    phase_callback: Any | None = None,
    failure_injector: Any | None = None,
    cache_directory: Path | None = None,
    cache_scope: str = "",
    request_renderer: Any | None = None,
    response_finalizer: Any | None = None,
    repair_scope_builder: Any | None = None,
    wire_protocol: str = "substar-cue-script.v2",
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    api_key = str(settings.get("translation_api_key", "")).strip()
    if not api_key:
        raise HTTPException(status_code=400, detail={"code": "api_key_missing", "message": "尚未配置翻译 API Key"})

    def run(
        item: tuple[str, list[dict[str, Any]]], active_stage: str, attempt: int,
        rejected: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        return run_owned(item, active_stage, attempt, rejected)

    def run_owned(
        item: tuple[str, list[dict[str, Any]]], active_stage: str, attempt: int,
        rejected: tuple[dict[str, Any], dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        block_id, block_cues = item
        active_block_cues = (
            repair_scope_builder(block_cues, rejected)
            if rejected is not None and repair_scope_builder is not None
            else block_cues
        )
        route = resolve_stage_request(settings, active_stage)
        active_system_prompt = (
            str(repair_system_prompt)
            if rejected is not None and repair_system_prompt
            else system_prompt
        )
        value: dict[str, Any] = {failure_key: []}
        validation_report: dict[str, Any] = {}
        request_metadata: dict[str, Any] = {}
        raw_response_for_cache: str | None = None
        primary_failure_metadata: dict[str, Any] = {}
        if rejected is not None:
            rejected_metadata = rejected[1]
            primary_failure_metadata = {
                "primary_error": str(rejected_metadata.get("error") or ""),
                "primary_validation_issues": [
                    dict(row) for row in rejected_metadata
                    .get("validation_report", {}).get("issues", [])
                    if isinstance(row, Mapping)
                ],
            }
        try:
            if failure_injector is not None:
                failure_injector(active_stage, block_id, attempt)
            request_group: dict[str, Any] = {
                "block_id": block_id, "cues": active_block_cues,
            }
            if rejected is not None:
                rejected_value, rejected_metadata = rejected
                request_group.update(
                    rejected_output=rejected_value,
                    program_validation_error=str(
                        rejected_metadata.get("error") or "invalid response contract"
                    ),
                    program_validation_errors=list(
                        rejected_metadata.get("validation_report", {}).get("issues", [])
                    ),
                    frozen_accepted_output=dict(
                        rejected_metadata.get("validation_report", {}).get("accepted_output", {})
                    ),
                    repair_attempt=attempt - 1,
                )
            wire_text = ""
            wire_ledger: Any | None = None
            if request_renderer is not None:
                wire_text, wire_ledger = request_renderer(
                    active_block_cues,
                    {
                        **request_group,
                        "raw_model_response": (
                            str(rejected[1].get("raw_model_response") or "")
                            if rejected is not None else ""
                        ),
                    } if rejected is not None else None,
                )
            cache_key = fingerprint({
                "scope": cache_scope,
                "stage": active_stage,
                "model": str(route["model"]),
                "system_prompt": active_system_prompt,
                "wire_text": wire_text,
                "thinking_mode": str(route["thinking_mode"]),
                "reasoning_effort": str(route["reasoning_effort"]),
                "max_tokens": int(route["max_tokens"]),
                "temperature": float(route["temperature"]),
                "schema": (
                    f"{wire_protocol}-raw-cache"
                    if request_renderer is not None else "calibration-actions.v3"
                ),
            })
            cached = (
                load_ai_block_cache(cache_directory, cache_key)
                if cache_directory is not None else None
            )
            if cached is not None and request_renderer is not None and response_finalizer is not None and isinstance(cached.get("_raw_model_response"), str):
                raw_response_for_cache = str(cached["_raw_model_response"])
                value = response_finalizer(raw_response_for_cache, wire_ledger)
                request_metadata = {
                    "cache_hit": True, "cache_key": cache_key,
                    "wire_protocol": wire_protocol,
                    "raw_model_response": raw_response_for_cache,
                    "finalized_response": value,
                }
            elif cached is not None:
                value = cached
                request_metadata = {"cache_hit": True, "cache_key": cache_key}
            elif request_renderer is not None and response_finalizer is not None:
                raw_response, request_metadata = call_text_model(
                    base_url=str(route["base_url"]),
                    api_key=api_key,
                    auth_mode=str(route["auth_mode"]),
                    model=str(route["model"]),
                    system_prompt=active_system_prompt,
                    user_text=wire_text,
                    timeout=min(600, int(settings.get("translation_api_timeout_seconds", 300))),
                    thinking_mode=str(route["thinking_mode"]),
                    reasoning_effort=str(route["reasoning_effort"]),
                    request_attempts=(
                        max(1, int(settings.get("http_retry_attempts", 2)) + 1)
                        if retry_stage else 1
                    ),
                    max_tokens=int(route["max_tokens"]),
                    temperature=float(route["temperature"]),
                )
                raw_response_for_cache = str(raw_response)
                exchange_path = (
                    cache_directory / "exchanges" / f"{time.time_ns()}_{cache_key}.json"
                    if cache_directory is not None else None
                )
                if exchange_path is not None:
                    atomic_write_json(exchange_path, {
                        "schema_version": "substar.model-exchange.v1",
                        "stage": active_stage,
                        "block_id": block_id,
                        "wire_protocol": wire_protocol,
                        "system_prompt": active_system_prompt,
                        "system_prompt_sha256": fingerprint({"text": active_system_prompt}),
                        "request_text": wire_text,
                        "raw_model_response": raw_response,
                        "transport_telemetry": request_metadata,
                    })
                try:
                    value = response_finalizer(raw_response, wire_ledger)
                except (TypeError, ValueError, KeyError) as exc:
                    if exchange_path is not None:
                        atomic_write_json(exchange_path, {
                            "schema_version": "substar.model-exchange.v1",
                            "stage": active_stage,
                            "block_id": block_id,
                            "wire_protocol": wire_protocol,
                            "system_prompt": active_system_prompt,
                            "system_prompt_sha256": fingerprint({"text": active_system_prompt}),
                            "request_text": wire_text,
                            "raw_model_response": raw_response,
                            "transport_telemetry": request_metadata,
                            "finalizer_error": str(exc),
                        })
                    raise ModelGatewayError(
                        f"{active_stage} Cue Script finalizer rejected output: {exc}"
                    ) from exc
                request_metadata = {
                    **request_metadata,
                    "wire_protocol": wire_protocol,
                    "wire_input_characters": len(wire_text),
                    "wire_output_characters": len(raw_response),
                    "raw_model_response": raw_response,
                    "finalized_response": value,
                    "cache_hit": False,
                    "cache_key": cache_key,
                }
                if exchange_path is not None:
                    atomic_write_json(exchange_path, {
                        "schema_version": "substar.model-exchange.v1",
                        "stage": active_stage,
                        "block_id": block_id,
                        "wire_protocol": wire_protocol,
                        "system_prompt": active_system_prompt,
                        "system_prompt_sha256": fingerprint({"text": active_system_prompt}),
                        "request_text": wire_text,
                        "raw_model_response": raw_response,
                        "finalized_response": value,
                        "transport_telemetry": {
                            key: item for key, item in request_metadata.items()
                            if key not in {"raw_model_response", "finalized_response"}
                        },
                    })
            else:
                value, request_metadata = call_translation_model(
                    base_url=str(route["base_url"]),
                    api_key=api_key,
                    auth_mode=str(route["auth_mode"]),
                    model=str(route["model"]),
                    system_prompt=active_system_prompt,
                    groups=[request_group],
                    timeout=min(600, int(settings.get("translation_api_timeout_seconds", 300))),
                    thinking_mode=str(route["thinking_mode"]),
                    reasoning_effort=str(route["reasoning_effort"]),
                    request_attempts=(
                        max(1, int(settings.get("http_retry_attempts", 2)) + 1)
                        if retry_stage else 1
                    ),
                    max_tokens=int(route["max_tokens"]),
                    temperature=float(route["temperature"]),
                )
                request_metadata = {
                    **request_metadata, "cache_hit": False, "cache_key": cache_key,
                }
            cache_value = (
                {"_raw_model_response": raw_response_for_cache}
                if raw_response_for_cache is not None and request_renderer is not None
                else dict(value)
            )
            if (
                rejected is not None and failure_key == "actions"
            ):
                frozen = list(
                    rejected[1].get("validation_report", {})
                    .get("accepted_output", {}).get("actions", [])
                )
                repaired = list(value.get("actions", []))
                value = {**value, "actions": [*frozen, *repaired]}
            if response_validator is not None:
                validation = response_validator(block_id, value)
                if isinstance(validation, Mapping):
                    validation_report = dict(validation)
                    valid = bool(validation_report.get("valid"))
                else:
                    valid = bool(validation)
                if not valid:
                    raise ModelGatewayError(
                        f"{active_stage} returned an invalid response contract"
                    )
            elif request_renderer is not None and value.get("_cue_script_issues"):
                raise ModelGatewayError(
                    f"{active_stage} Cue Script still has unresolved aliases"
                )
            if cached is None and cache_directory is not None:
                save_ai_block_cache(cache_directory, cache_key, cache_value)
            return block_id, value, {
                "attempt": attempt,
                "stage": active_stage,
                "primary_request_count": 1,
                "repair_attempted": attempt > 1,
                "repair_request_count": 1 if attempt > 1 else 0,
                "validation_report": validation_report,
                **primary_failure_metadata,
                **request_metadata,
            }
        except ModelGatewayError as exc:
            return block_id, value, {
                "attempt": attempt,
                "stage": active_stage,
                "error": str(exc),
                "terminal_error": (
                    isinstance(exc, ModelGatewayRequestError)
                ),
                "primary_request_count": 1,
                "repair_attempted": attempt > 1,
                "repair_request_count": 1 if attempt > 1 else 0,
                "validation_report": validation_report,
                **primary_failure_metadata,
                **request_metadata,
            }

    if not blocks:
        return []
    primary_results: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    workers = min(len(blocks), max(1, int(settings.get("translation_workers", 8))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, item, stage_name, 1) for item in blocks.items()]
        for future in concurrent.futures.as_completed(futures):
            primary_results.append(future.result())
            if progress_callback is not None:
                progress_callback(len(primary_results), len(blocks))

    failed = [row for row in primary_results if row[2].get("error")]
    terminal = [row for row in failed if row[2].get("terminal_error")]
    if terminal:
        raise ModelGatewayError(str(terminal[0][2]["error"]))
    if not retry_stage or not failed:
        if phase_callback is not None:
            phase_callback("repair", 0, 0, 0)
        return primary_results

    if phase_callback is not None:
        phase_callback("repair", 0, len(failed), 0)
    repaired: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    retry_items = [
        ((block_id, blocks[block_id]), (value, metadata))
        for block_id, value, metadata in failed
    ]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(workers, len(retry_items))
    ) as pool:
        futures = [
            pool.submit(run, item, str(retry_stage), 2, rejected)
            for item, rejected in retry_items
        ]
        for future in concurrent.futures.as_completed(futures):
            row = future.result()
            repaired[row[0]] = row
            if phase_callback is not None:
                accepted = sum(not item[2].get("error") for item in repaired.values())
                phase_callback("repair", len(repaired), len(failed), accepted)
    return [repaired.get(row[0], row) for row in primary_results]

def _calibration_signature(text: str) -> str:
    return "".join(
        char.casefold() for char in text
        if char not in _CALIBRATION_PUNCTUATION
    )

def _calibration_punctuation_signature(text: str) -> str:
    """Remove punctuation while preserving case for punctuation-only edits."""
    return "".join(
        char for char in text
        if char not in _CALIBRATION_PUNCTUATION
    )

def _calibration_alnum_signature(text: str) -> str:
    """Compare token content while ignoring case, separators, and punctuation."""
    return "".join(char.casefold() for char in str(text) if char.isalnum())

def _calibration_core(text: str) -> str:
    return str(text).rstrip(_CALIBRATION_PUNCTUATION)

def _calibration_suffix(text: str) -> str:
    value = str(text)
    return value[len(_calibration_core(value)):]

def _calibration_model_blocks(
    blocks: Mapping[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Send source-only context while preserving exact action preconditions."""
    return {
        block_id: [
            {
                **{
                    key: value
                    for key, value in cue.items()
                    if key not in {"target_text", "translation_mapping"}
                },
                "tokens": [dict(token) for token in cue["tokens"]],
            }
            for cue in block_cues
        ]
        for block_id, block_cues in blocks.items()
    }

def _validated_calibration_contract_actions(
    value: Any,
    owned_token_ids: list[str],
    token_map: Mapping[str, Any],
    token_to_cue_id: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    allowed_response_fields = {
        "actions", "_cue_script_issues", "_covered_cue_ids",
    }
    if (
        not isinstance(value, Mapping)
        or "actions" not in value
        or not set(value) <= allowed_response_fields
    ):
        return accepted, [{"code": "invalid_response", "fatal": True}]
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, list):
        return accepted, [{"code": "invalid_actions", "fatal": True}]
    positions = {token_id: index for index, token_id in enumerate(owned_token_ids)}
    shadow_text = {
        token_id: str(token_map[token_id].text)
        for token_id in owned_token_ids if token_id in token_map
    }
    merged_token_ids: set[str] = set()
    seen_action_ids: set[str] = set()
    for item_index, raw in enumerate(raw_actions):
        reason = ""
        if not isinstance(raw, Mapping) or set(raw) != _CALIBRATION_ACTION_FIELDS:
            reason = "action fields do not match the frozen contract"
        action = dict(raw) if isinstance(raw, Mapping) else {}
        action_id = str(action.get("action_id", "")).strip()
        kind = str(action.get("kind", ""))
        token_ids = action.get("token_ids")
        before_text = str(action.get("before_text", ""))
        after_text = str(action.get("after_text", ""))
        confidence = str(action.get("confidence", ""))
        disposition = str(action.get("disposition", ""))
        evidence = action.get("evidence")
        if not reason and (
            not action_id or action_id in seen_action_ids
            or kind not in {
                "set_case", "set_punctuation", "replace_token", "replace_span",
                "merge_span",
            }
            or confidence not in {"high", "medium", "low"}
            or disposition not in {"apply", "review"}
            or not isinstance(action.get("affects_translation"), bool)
        ):
            reason = "action identity, kind, confidence, or disposition is invalid"
        if not reason and before_text and after_text == before_text:
            # A no-op cannot mutate project state. Ignore it before binding so
            # a misplaced unchanged suggestion cannot fail the whole block.
            continue
        if not reason and (
            not isinstance(token_ids, list) or not token_ids
            or len(set(str(item) for item in token_ids)) != len(token_ids)
            or any(str(item) not in positions for item in token_ids)
        ):
            reason = "token_ids are not wholly owned by this block"
        normalized_ids = [str(item) for item in token_ids] if isinstance(token_ids, list) else []
        if not reason and any(token_id in merged_token_ids for token_id in normalized_ids):
            reason = "action targets a token consumed by an earlier merge_span"
        if not reason:
            indexes = [positions[token_id] for token_id in normalized_ids]
            if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
                reason = "token_ids must be contiguous and ordered"
        expected_before = ""
        if not reason:
            expected_before = " ".join(
                shadow_text[token_id] for token_id in normalized_ids
            )
        if not reason and (not before_text or before_text != expected_before or not after_text):
            reason = "before_text does not reproduce the bound source tokens"
        if not reason and kind in {"set_case", "set_punctuation", "replace_token"} and len(normalized_ids) != 1:
            reason = f"{kind} must target one token"
        if (
            not reason
            and kind == "replace_token"
            and not any(char.isspace() for char in after_text)
        ):
            if (
                _calibration_signature(after_text) == _calibration_signature(before_text)
                and _calibration_suffix(after_text) == _calibration_suffix(before_text)
            ):
                action["kind"] = "set_case"
                action["affects_translation"] = False
                kind = "set_case"
            elif (
                _calibration_punctuation_signature(after_text)
                == _calibration_punctuation_signature(before_text)
            ):
                action["kind"] = "set_punctuation"
                action["affects_translation"] = False
                kind = "set_punctuation"
        if (
            not reason
            and kind == "replace_token"
            and any(char.isspace() for char in after_text)
        ):
            # A display token must remain one written unit.  Preserve a useful
            # model suggestion for review, but never pretend that it can be
            # materialized by the current token-preserving operation contract.
            action["disposition"] = "review"
            disposition = "review"
        if (
            not reason
            and kind == "replace_span"
            and len(normalized_ids) >= 2
            and len(after_text.split()) == 1
            and len({token_to_cue_id.get(token_id) for token_id in normalized_ids}) == 1
            and token_to_cue_id.get(normalized_ids[0]) is not None
        ):
            # A same-Cue many-to-one replacement has an unambiguous topology:
            # it is a merge, even if the model selected the adjacent label.
            action["kind"] = "merge_span"
            action["affects_translation"] = True
            kind = "merge_span"
        if not reason and kind == "set_case" and (
            any(char.isspace() for char in after_text)
            or _calibration_signature(after_text) != _calibration_signature(before_text)
            or _calibration_suffix(after_text) != _calibration_suffix(before_text)
        ):
            reason = "set_case may change only case"
        if not reason and kind == "set_punctuation" and (
            any(char.isspace() for char in after_text)
            or _calibration_punctuation_signature(after_text)
            != _calibration_punctuation_signature(before_text)
        ):
            reason = "set_punctuation may change only light punctuation"
        if (
            not reason
            and kind == "replace_span"
            and disposition == "apply"
            and len(after_text.split()) != len(normalized_ids)
        ):
            reason = "replace_span must preserve token count"
        if not reason and kind == "merge_span":
            # A topology change necessarily invalidates the Cue translation.
            # This is derived from the action kind, so normalize a mistaken
            # model flag instead of rejecting an otherwise valid merge.
            action["affects_translation"] = True
            if (
                disposition == "apply"
                and _calibration_alnum_signature(after_text)
                != _calibration_alnum_signature(before_text)
            ):
                # A merge may change casing, punctuation and token boundaries,
                # but must not silently delete or invent lexical content.
                action["disposition"] = "review"
                disposition = "review"
        if not reason and kind == "merge_span" and (
            len(normalized_ids) < 2
            or len({token_to_cue_id.get(token_id) for token_id in normalized_ids}) != 1
            or None in {token_to_cue_id.get(token_id) for token_id in normalized_ids}
            or after_text.strip() != after_text
            or not after_text
            or any(char.isspace() for char in after_text)
        ):
            reason = "merge_span must merge contiguous tokens in one Cue into one token"
        if not reason and after_text == before_text:
            reason = "action must change the bound text"
        if not reason and (
            not isinstance(evidence, list) or not evidence
            or any(
                not isinstance(row, Mapping)
                or set(row) != {"kind", "reference"}
                or str(row.get("kind")) not in _CALIBRATION_EVIDENCE_KINDS
                or not str(row.get("reference", "")).strip()
                for row in evidence
            )
        ):
            reason = "evidence is missing or invalid"
        if reason:
            rejected.append({
                "code": "invalid_action", "fatal": False,
                "item_index": item_index, "action_id": action_id, "detail": reason,
            })
            continue
        seen_action_ids.add(action_id)
        if disposition == "apply":
            if kind == "replace_span":
                for token_id, text in zip(
                    normalized_ids, after_text.split(), strict=True
                ):
                    shadow_text[token_id] = text
            elif kind == "merge_span":
                shadow_text[normalized_ids[0]] = after_text
                merged_token_ids.update(normalized_ids)
            else:
                shadow_text[normalized_ids[0]] = after_text
        accepted.append({**action, "token_ids": normalized_ids})
    return accepted, rejected

def _apply_ai_calibration_operations(
    document: EditorDocument,
    *,
    replacements: list[BatchReplacement],
    merge_actions: list[Mapping[str, Any]],
    calibration_metadata: Mapping[str, Any],
    operation_id: str,
) -> EditorDocument:
    """Apply text edits and same-Cue topology merges as one in-memory change."""
    result = document
    if replacements:
        replacement_provenance = ChangeProvenance(
            kind=ChangeKind.AI,
            operation="ai_calibration_apply",
            actor="ai-calibration",
            metadata={
                **dict(calibration_metadata),
                "replacement_count": len(replacements),
                "merge_count": len(merge_actions),
            },
        )
        result = apply_document_operation(result, {
            "operation_id": f"{operation_id}_replace",
            "type": "batch_replace",
            "payload": {
                "replacements": [item.model_dump() for item in replacements],
                "provenance": replacement_provenance.to_dict(),
            },
        })

    for index, action in enumerate(merge_actions, start=1):
        token_ids = [str(token_id) for token_id in action["token_ids"]]
        current_tokens = {token.token_id: token for token in result.display_tokens}
        before_tokens = [current_tokens[token_id].to_dict() for token_id in token_ids]
        calibration_record = {
            "topology": "merge_span",
            "action_id": str(action["action_id"]),
            "applied": True,
            "cue_id": str(action["cue_id"]),
            "before_text": str(action["before_text"]),
            "after_text": str(action["after_text"]),
            "before_tokens": before_tokens,
            "affects_translation": True,
            "evidence": list(action.get("evidence", [])),
            "confidence": str(action.get("confidence", "")),
        }
        merge_provenance = ChangeProvenance(
            kind=ChangeKind.AI,
            operation="ai_calibration_merge",
            actor="ai-calibration",
            metadata={
                **dict(calibration_metadata),
                "ai_calibration": calibration_record,
                "affects_translation": True,
                "evidence": list(action.get("evidence", [])),
                "confidence": str(action.get("confidence", "")),
            },
        )
        result = apply_document_operation(result, {
            "operation_id": f"{operation_id}_merge_{index}",
            "type": "merge",
            "payload": {
                "cue_id": str(action["cue_id"]),
                "token_ids": token_ids,
                "text": str(action["after_text"]),
                "provenance": merge_provenance.to_dict(),
            },
        })
    return result

def compute_calibration(
    project_id: str,
    payload: AiCalibrationRequest,
    task_id: str,
    *,
    project_root: Path,
    artifact_directory: Path,
    settings_snapshot: Mapping[str, Any] | None = None,
    progress_sink: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    store = ProjectStore.open(project_root / "project")
    latest = store.load_latest()
    if latest is None:
        raise ValueError("项目还没有文稿版本")
    if payload.expected_revision_id != latest.revision_id:
        raise ValueError("AI 校准基于旧版本，请刷新后重试")
    token_map, cues = _editor_ai_cues(latest)
    blocks = _editor_ai_blocks(cues)
    request_blocks = _calibration_model_blocks(blocks)
    settings = dict(settings_snapshot) if settings_snapshot is not None else load_settings(include_secret=True)
    calibration_glossary = [
        {
            "source": row.get("source"),
            "standard_source": row.get("standard_source"),
            "aliases": row.get("aliases", []),
        }
        for row in (settings["glossary_snapshot"] if "glossary_snapshot" in settings else active_glossary(str(load_task_info(project_root, project_id).get("glossary_id") or "")))
    ]
    configured_source_language = str(settings.get("language") or "Auto")
    resolved_source_language = (
        source_language_for_text(
            " ".join(
                str(token["text"])
                for cue in cues
                for token in cue["tokens"]
            )
        )
        if configured_source_language.strip().lower() in {"", "auto", "automatic"}
        else normalize_source_language(configured_source_language)
    )
    calibration_prompt_variant = calibration_variant(resolved_source_language)
    calibration_prompt = frozen_prompt(settings,
        "calibration", variant=calibration_prompt_variant
    ).text
    calibration_repair_prompt = frozen_prompt(settings,
        "calibration_repair", variant=calibration_prompt_variant
    ).text
    if calibration_glossary:
        glossary_section = glossary_prompt(calibration_glossary, include_target=False)
        calibration_prompt += "\n\n" + glossary_section
        calibration_repair_prompt += "\n\n" + glossary_section
    if payload.instruction.strip():
        instruction_section = (
            "\n\n用户本次补充校准要求：\n"
            + payload.instruction.strip()
            + "\n补充要求只能在既有校准动作契约允许的范围内执行；不得改变 Cue 时间或结构。"
        )
        calibration_prompt += instruction_section
        calibration_repair_prompt += instruction_section
    # Keep the machine grammar last so neither glossary data nor a bounded
    # user instruction can accidentally shadow the output protocol.
    calibration_prompt += "\n\n" + output_contract("CALIBRATE")
    calibration_repair_prompt += "\n\n" + output_contract("CALIBRATE")
    tracker = {
        "planned": len(blocks), "completed": 0,
        "primary_accepted": 0, "repair_planned": 0,
        "repair_completed": 0, "repair_accepted": 0,
    }
    token_to_cue_id = {
        str(token["token_id"]): str(cue["cue_id"])
        for cue in cues
        for token in cue["tokens"]
    }

    def valid_calibration_block(block_id: str, value: Any) -> dict[str, Any]:
        owned_cues = [
            cue for cue in blocks.get(block_id, []) if cue["editable"]
        ]
        token_ids = [
            str(token["token_id"])
            for cue in owned_cues for token in cue["tokens"]
        ]
        actions, rejections = _validated_calibration_contract_actions(
            value, token_ids, token_map, token_to_cue_id
        )
        if isinstance(value, Mapping):
            rejections.extend(
                dict(row) for row in value.get("_cue_script_issues", [])
                if isinstance(row, Mapping)
            )
        return {
            "valid": not rejections,
            "issues": [dict(row) for row in rejections],
            "accepted_output": {"actions": [dict(row) for row in actions]},
        }

    def calibration_repair_scope(
        block_cues: list[dict[str, Any]],
        rejected: tuple[dict[str, Any], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        issues = rejected[1].get("validation_report", {}).get("issues", [])
        missing = {
            str(issue.get("cue_id") or "")
            for issue in issues if isinstance(issue, Mapping)
            and str(issue.get("cue_id") or "")
        }
        editable_indexes = [
            index for index, cue in enumerate(block_cues)
            if cue.get("editable") and (
                not missing or str(cue.get("cue_id")) in missing
            )
        ]
        if not editable_indexes:
            editable_indexes = [
                index for index, cue in enumerate(block_cues) if cue.get("editable")
            ]
        if not editable_indexes:
            return block_cues
        # Keep the complete original execution block as context. Only the
        # failed Cue aliases remain OWN; every accepted primary binding is
        # explicitly read-only and is merged back by the finalizer.
        return [
            {
                **cue,
                "editable": index in editable_indexes,
            }
            for index, cue in enumerate(block_cues)
        ]

    def write_calibration_progress(phase: str, *, detail: str = "") -> None:
        value = ai_progress(
            kind="calibration", phase=phase, unit_label="块",
            planned=tracker["planned"], completed=tracker["completed"],
            accepted=tracker["primary_accepted"],
            failed=max(0, tracker["completed"] - tracker["primary_accepted"]),
            repair_planned=tracker["repair_planned"],
            repair_completed=tracker["repair_completed"],
            repair_accepted=tracker["repair_accepted"],
            repair_failed=max(
                0, tracker["repair_completed"] - tracker["repair_accepted"]
            ),
            detail=detail,
        )
        if progress_sink is not None:
            progress_sink(value)

    def primary_progress(done: int, total: int) -> None:
        tracker["planned"] = total
        tracker["completed"] = done
        write_calibration_progress("executing")

    def phase_progress(_phase: str, done: int, total: int, accepted: int) -> None:
        tracker["repair_planned"] = total
        tracker["repair_completed"] = done
        tracker["repair_accepted"] = accepted
        tracker["primary_accepted"] = tracker["planned"] - total
        if total:
            write_calibration_progress("repair")

    write_calibration_progress("executing")
    results = _run_editor_ai_blocks(
        settings=settings,
        system_prompt=calibration_prompt,
        repair_system_prompt=calibration_repair_prompt,
        blocks=request_blocks,
        failure_key="actions",
        stage_name="calibration",
        retry_stage="audit_repair",
        response_validator=valid_calibration_block,
        progress_callback=primary_progress,
        phase_callback=phase_progress,
        cache_directory=project_root / "calibration" / "block_cache",
        cache_scope="calibration-lines-v2-numeric-pipe-block-patch",
        request_renderer=lambda block_cues, repair_feedback=None: render_cue_request(
            block_cues,
            task="CALIBRATE",
            instructions=(
                "Return every OWN Cue exactly once; use its local alias unchanged."
            ),
            repair_feedback=repair_feedback,
        ),
        response_finalizer=lambda raw, ledger: finalize_calibration_candidate(raw, ledger),
        repair_scope_builder=calibration_repair_scope,
        wire_protocol="substar-calibration-lines.v2-numeric-pipe",
    )

    write_calibration_progress("validating")

    desired_text: dict[str, str] = {}
    failed_blocks: list[str] = []
    problem_cue_ids: list[str] = []
    problem_block_ids: set[str] = set()
    request_metadata: list[dict[str, Any]] = []
    calibration_audit_blocks: list[dict[str, Any]] = []
    accepted_contract_actions: list[dict[str, Any]] = []
    review_actions: list[dict[str, Any]] = []
    merge_actions: list[dict[str, Any]] = []
    sentence_count = internal_count = case_suggested = lexical_count = filtered_count = 0
    for block_id, value, metadata in sorted(results):
        request_metadata.append({"block_id": block_id, "request_metadata": metadata})
        owned_cues = [cue for cue in blocks.get(block_id, []) if cue["editable"]]
        token_ids = [
            str(token["token_id"])
            for cue in owned_cues for token in cue["tokens"]
        ]
        if metadata.get("error"):
            validation_report = metadata.get("validation_report", {})
            actions = [
                dict(row) for row in validation_report
                .get("accepted_output", {}).get("actions", [])
                if isinstance(row, Mapping)
            ]
            validation_rejections = [
                dict(row) for row in validation_report.get("issues", [])
                if isinstance(row, Mapping)
            ]
            if not validation_rejections:
                validation_rejections = [{
                    "code": "model_request_failed",
                    "fatal": True,
                    "detail": str(metadata.get("error")),
                }]
            failed_blocks.append(block_id)
        else:
            actions, validation_rejections = _validated_calibration_contract_actions(
                value, token_ids, token_map, token_to_cue_id
            )
        accepted_contract_actions.extend(actions)
        filtered_count += len(validation_rejections)
        block_problem_cue_ids: set[str] = set()
        for rejection in validation_rejections:
            rejected_action_id = str(rejection.get("action_id", ""))
            rejected_action = next(
                (
                    row for row in value.get("actions", [])
                    if isinstance(row, Mapping)
                    and str(row.get("action_id", "")) == rejected_action_id
                ),
                None,
            ) if isinstance(value, Mapping) else None
            if isinstance(rejected_action, Mapping):
                block_problem_cue_ids.update(
                    token_to_cue_id.get(str(token_id), "")
                    for token_id in rejected_action.get("token_ids", [])
                    if token_to_cue_id.get(str(token_id))
                )
            if rejection.get("fatal"):
                block_problem_cue_ids.update(str(cue["cue_id"]) for cue in owned_cues)
        problem_cue_ids.extend(sorted(block_problem_cue_ids))
        if block_problem_cue_ids:
            problem_block_ids.add(block_id)
        calibration_audit_blocks.append({
            "block_id": block_id,
            "owned_cue_ids": [str(cue["cue_id"]) for cue in owned_cues],
            "request_metadata": metadata,
            "raw_response": value,
            "accepted_actions": actions,
            "filtered_actions": validation_rejections,
        })
        for action in actions:
            kind = str(action["kind"])
            if action["disposition"] == "review":
                review_actions.append(action)
                block_problem_cue_ids.update(
                    token_to_cue_id.get(token_id, "")
                    for token_id in action["token_ids"]
                    if token_to_cue_id.get(token_id)
                )
                continue
            if kind == "merge_span":
                merge_actions.append({
                    **action,
                    "cue_id": token_to_cue_id[action["token_ids"][0]],
                })
                lexical_count += 1
                continue
            replacement_parts = (
                str(action["after_text"]).split()
                if kind == "replace_span"
                else [str(action["after_text"])]
            )
            for token_id, replacement_text in zip(
                action["token_ids"], replacement_parts, strict=True
            ):
                desired_text[token_id] = replacement_text
            if kind == "set_case":
                case_suggested += 1
            elif kind == "set_punctuation":
                if _calibration_suffix(str(action["after_text"])) in {".", "?", "!"}:
                    sentence_count += 1
                else:
                    internal_count += 1
            else:
                lexical_count += 1
        problem_cue_ids.extend(sorted(block_problem_cue_ids))
        if block_problem_cue_ids:
            problem_block_ids.add(block_id)

    invalid_materialization = {
        token_id: text
        for token_id, text in desired_text.items()
        if not text or text.strip() != text or any(char.isspace() for char in text)
    }
    if invalid_materialization:
        raise RuntimeError(
            "AI calibration validation/materialization contract drift: "
            + ", ".join(sorted(invalid_materialization))
        )
    replacements = [
        BatchReplacement(
            token_id=token_id,
            text=text,
            expected_text=token_map[token_id].text,
        )
        for token_id, text in desired_text.items()
        if text != token_map[token_id].text
    ]
    applied_case_count = sum(
        _calibration_core(item.text)
        != _calibration_core(str(item.expected_text or ""))
        for item in replacements
    )
    applied_punctuation_count = sum(
        _calibration_suffix(item.text)
        != _calibration_suffix(str(item.expected_text or ""))
        for item in replacements
    )
    translation_stale_cue_ids = list(dict.fromkeys(
        token_to_cue_id[token_id]
        for action in accepted_contract_actions
        if action["disposition"] == "apply" and action["affects_translation"]
        for token_id in action["token_ids"]
        if token_id in token_to_cue_id
    ))
    calibration_metadata = {
        "execution_blocks": request_metadata,
        "failed_blocks": failed_blocks,
        "calibration_problem_cue_ids": list(dict.fromkeys(problem_cue_ids)),
        "calibration_problem_block_ids": sorted(problem_block_ids),
        "allowed_changes": "frozen_calibration_contract",
        "sentence_count": sentence_count,
        "filtered_count": filtered_count,
        "lexical_replacement_count": lexical_count,
        "merge_count": len(merge_actions),
        "translation_stale_cue_ids": translation_stale_cue_ids,
        "semantic_group_count": len({str(cue.get("group_id")) for cue in cues}),
        "single_attempt_delivery": True,
    }
    if blocks and len(failed_blocks) == len(blocks):
        # A provider-wide failure is not a completed calibration. In
        # particular, do not append an empty AI revision whose fatal block
        # markers make every owned cue appear in the problem-subtitle list.
        # Persist this attempt before raising so the next diagnosis never has
        # to rely on a stale successful/failed audit artifact.
        failure_errors = list(dict.fromkeys(
            str(item.get("request_metadata", {}).get("error", "")).strip()
            for item in request_metadata
            if str(item.get("request_metadata", {}).get("error", "")).strip()
        ))
        failure_audit_path = artifact_directory / "audit.json"
        atomic_write_json(
            failure_audit_path,
            {
                "schema_version": "substar.calibration-audit.v2",
                "project_id": project_id,
                "task_id": task_id,
                "based_on_revision_id": latest.revision_id,
                "result_revision_id": None,
                "blocks": calibration_audit_blocks,
                "summary": {
                    "checked_cues": len(cues),
                    "block_count": len(blocks),
                    "failed_blocks": failed_blocks,
                    "filtered_count": filtered_count,
                    "replacement_count": 0,
                    "merge_count": 0,
                    "sentence_count": sentence_count,
                    "case_applied_count": 0,
                    "punctuation_applied_count": 0,
                    "lexical_replacement_count": lexical_count,
                },
            },
        )
        detail = failure_errors[0] if failure_errors else "模型未返回错误详情"
        raise RuntimeError(
            f"AI 校准所有执行块均失败：{detail}"
        )
    if replacements or merge_actions:
        write_calibration_progress("materializing", detail="正在应用校准动作")
        calibration_operation_id = f"op_calibration_{latest.revision_id}"
        try:
            document = _apply_ai_calibration_operations(
                latest.document,
                replacements=replacements,
                merge_actions=merge_actions,
                calibration_metadata=calibration_metadata,
                operation_id=calibration_operation_id,
            )
        except (KeyError, TypeError, ValueError, DocumentOperationError) as exc:
            raise RuntimeError(f"AI 校准结果无法应用：{exc}") from exc
        provenance = ChangeProvenance(
            kind=ChangeKind.AI,
            operation="ai_calibration_apply",
            actor="ai-calibration",
            metadata={
                **calibration_metadata,
                "replacement_count": len(replacements),
                "merge_count": len(merge_actions),
            },
        )
        revision = write_candidate(artifact_directory, latest, document, provenance).to_dict()
    else:
        write_calibration_progress("materializing", detail="正在生成校准版本")
        provenance = ChangeProvenance(
            kind=ChangeKind.AI,
            operation="ai_calibration_apply",
            actor="ai-calibration",
            metadata={
                **calibration_metadata,
                "replacement_count": 0,
                "merge_count": 0,
            },
        )
        document = replace(latest.document, changes=(*latest.document.changes, provenance))
        revision = write_candidate(artifact_directory, latest, document, provenance).to_dict()
    calibration_directory = artifact_directory
    result_path = calibration_directory / "latest.json"
    audit_path = calibration_directory / "audit.json"
    audit_error = ""
    write_calibration_progress("publishing", detail="正在写入版本与审计产物")
    try:
        atomic_write_json(
            audit_path,
            {
                "schema_version": "substar.calibration-audit.v2",
                "project_id": project_id,
                "task_id": task_id,
                "based_on_revision_id": latest.revision_id,
                "result_revision_id": _revision_id(revision),
                "blocks": calibration_audit_blocks,
                "summary": {
                    "checked_cues": len(cues),
                    "block_count": len(blocks),
                    "failed_blocks": failed_blocks,
                    "filtered_count": filtered_count,
                    "replacement_count": len(replacements),
                    "merge_count": len(merge_actions),
                    "sentence_count": sentence_count,
                    "case_applied_count": applied_case_count,
                    "punctuation_applied_count": applied_punctuation_count,
                    "lexical_replacement_count": lexical_count,
                },
            },
        )
        atomic_write_json(
            result_path,
            {
                "schema_version": "substar.calibration-result.v2",
                "task_id": task_id,
                "project_id": project_id,
                "based_on_revision_id": latest.revision_id,
                "actions": accepted_contract_actions,
            },
        )
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"校准候选审计写入失败：{exc}") from exc
    result = {
        "revision": revision,
        "corrections": [item.model_dump() for item in replacements],
        "merges": [
            {
                "action_id": action["action_id"],
                "cue_id": action["cue_id"],
                "token_ids": action["token_ids"],
                "text": action["after_text"],
            }
            for action in merge_actions
        ],
        "failed_blocks": failed_blocks,
        "problem_cue_ids": list(dict.fromkeys(problem_cue_ids)),
        "problem_block_ids": sorted(problem_block_ids),
        "checked_cues": len(cues),
        "block_count": len(blocks),
        "semantic_group_count": calibration_metadata["semantic_group_count"],
        "suggested_count": sentence_count + internal_count + case_suggested + lexical_count + len(review_actions),
        "filtered_count": filtered_count,
        "sentence_count": sentence_count,
        "case_applied_count": applied_case_count,
        "punctuation_applied_count": applied_punctuation_count,
        "lexical_replacement_count": lexical_count,
        "merge_applied_count": len(merge_actions),
        "review_actions": review_actions,
        "translation_stale_cue_ids": translation_stale_cue_ids,
        "calibration_result_path": result_path.name,
        "calibration_audit_path": audit_path.name,
        "duration_seconds": round(sum(
            float(item.get("request_metadata", {}).get("duration_seconds", 0) or 0)
            for item in request_metadata
        ), 3),
    }
    if audit_error:
        result["calibration_audit_error"] = audit_error
    final_progress = ai_progress(
        kind="calibration", phase="completed", unit_label="块",
        planned=tracker["planned"], completed=tracker["planned"],
        accepted=max(0, tracker["planned"] - len(failed_blocks)),
        failed=len(failed_blocks), repair_planned=tracker["repair_planned"],
        repair_completed=tracker["repair_planned"],
        repair_accepted=tracker["repair_accepted"],
        repair_failed=len(failed_blocks),
        problem_count=len(problem_block_ids),
        detail=(
            f"替换 {len(replacements)} 项，合并 {len(merge_actions)} 项，"
            f"{len(problem_block_ids)} 块转人工"
        ),
    )
    if progress_sink is not None:
        progress_sink(final_progress)
    result["ai_progress"] = final_progress
    return result
