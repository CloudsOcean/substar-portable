from __future__ import annotations

from substar_core.config import DEFAULTS
import substar_core.editor.http_api as editor_http_api
from substar_core.segmentation.contracts import build_segmentation_request
from substar_core.model_gateway import ModelGatewayError


def test_segmentation_repair_reads_the_canonical_non_thinking_policy() -> None:
    settings = {
        **DEFAULTS,
        "translation_api_model": "global-model",
        "stage_segmentation_model": "grouping-model",
        "stage_segmentation_repair_model": "repair-model",
        "stage_segmentation_repair_thinking_mode": "disabled",
        "stage_segmentation_repair_reasoning_effort": "high",
        "stage_segmentation_repair_max_tokens": 4321,
        "stage_segmentation_repair_temperature": 0.1,
    }
    request = build_segmentation_request(
        transcription_task_id="tsk_" + "a" * 32,
        transcription_input_fingerprint="1" * 64,
        media_sha256="2" * 64,
        source_asset_id="asset-1",
        language="en",
        segmentation_enabled=True,
        reference_document=None,
        prompt_snapshot={
            "relative_path": "task_inputs/segmentation_prompts",
            "sha256": "3" * 64,
            "file_count": 3,
        },
        glossary_snapshot=[],
        settings=settings,
    )
    assert request["provider"]["grouping"]["model"] == "grouping-model"
    assert request["provider"]["repair"] == {
        "model": "repair-model",
        "thinking_mode": "disabled",
        "reasoning_effort": "high",
        "max_tokens": 4321,
        "temperature": 0.1,
    }


def test_every_exposed_model_stage_has_a_complete_default_policy() -> None:
    stages = (
        "segmentation",
        "segmentation_repair",
        "translation",
        "translation_repair",
        "calibration",
        "audit_repair",
    )
    for stage in stages:
        for suffix in (
            "model",
            "thinking_mode",
            "reasoning_effort",
            "max_tokens",
            "temperature",
        ):
            assert f"stage_{stage}_{suffix}" in DEFAULTS
    assert DEFAULTS["stage_translation_repair_thinking_mode"] == "disabled"
    assert DEFAULTS["stage_segmentation_repair_thinking_mode"] == "disabled"
    assert DEFAULTS["stage_audit_repair_thinking_mode"] == "disabled"
