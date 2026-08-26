from __future__ import annotations

from substar_core.config import DEFAULTS
import substar_core.editor.http_api as editor_http_api
from substar_core.segmentation.contracts import build_segmentation_request
from substar_core.stage2 import Stage2Error


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
        "review",
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


def test_editor_audit_fallback_uses_configured_non_thinking_policy(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_call_translation_model(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise Stage2Error("main stage failed")
        return {"issues": []}, {"provider": "fake"}

    monkeypatch.setattr(editor_http_api, "call_translation_model", fake_call_translation_model)
    settings = {
        "translation_api_key": "secret",
        "translation_api_base_url": "https://example.invalid",
        "translation_api_model": "global-model",
        "stage_review_model": "review-model",
        "stage_review_thinking_mode": "enabled",
        "stage_review_reasoning_effort": "max",
        "stage_review_max_tokens": 10000,
        "stage_review_temperature": 0.0,
        "stage_audit_repair_model": "fallback-model",
        "stage_audit_repair_thinking_mode": "disabled",
        "stage_audit_repair_reasoning_effort": "high",
        "stage_audit_repair_max_tokens": 5000,
        "stage_audit_repair_temperature": 0.0,
    }
    results = editor_http_api._run_editor_ai_blocks(
        settings=settings,
        system_prompt="p",
        blocks={"block-1": []},
        failure_key="issues",
        stage_name="review",
        response_validator=lambda value: isinstance(value.get("issues"), list),
    )
    assert not results[0][2].get("error")
    assert calls[0]["model"] == "review-model"
    assert calls[1]["model"] == "fallback-model"
    assert calls[1]["thinking_mode"] == "disabled"
