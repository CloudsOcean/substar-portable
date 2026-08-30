from __future__ import annotations

import json
from unittest.mock import Mock, patch

from substar_core.ai_progress import ai_progress
from substar_core.editor import http_api
from substar_core.editor.translation import contextual
from substar_core.editor.translation.service import _progress
from substar_core.stage2 import Stage2Error
from substar_core.model_routing import resolve_stage_request
from scripts.segmentation_support import call_model


def test_common_progress_contract_is_monotonic_and_counted() -> None:
    values = [
        ai_progress(kind="calibration", phase="executing", unit_label="块", planned=4, completed=0),
        ai_progress(kind="calibration", phase="executing", unit_label="块", planned=4, completed=4),
        ai_progress(
            kind="calibration", phase="repairing", unit_label="块",
            planned=4, completed=4, accepted=3, failed=1,
            repair_planned=1, repair_completed=0,
        ),
        ai_progress(
            kind="calibration", phase="repairing", unit_label="块",
            planned=4, completed=4, accepted=3, failed=1,
            repair_planned=1, repair_completed=1, repair_accepted=1,
        ),
        ai_progress(kind="calibration", phase="validating", unit_label="块", planned=4, completed=4),
        ai_progress(kind="calibration", phase="materializing", unit_label="块", planned=4, completed=4),
        ai_progress(kind="calibration", phase="publishing", unit_label="块", planned=4, completed=4),
        ai_progress(kind="calibration", phase="completed", unit_label="块", planned=4, completed=4),
    ]
    assert [row["progress"] for row in values] == sorted(
        row["progress"] for row in values
    )
    assert values[0]["message"] == "模型处理 0/4 块"
    assert values[2]["message"] == "修复 0/1 块 · 首轮通过 3/4"
    assert values[-1]["progress"] == 1.0


def test_all_model_stages_resolve_one_active_provider_and_capability_policy() -> None:
    settings = {
        "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "translation_api_key": "only-active-provider-key",
        "translation_api_auth_mode": "bearer",
        "translation_api_model": "glm-5.3-flash",
    }
    for stage in (
        "segmentation", "segmentation_repair", "translation",
        "translation_repair", "calibration", "audit_repair",
    ):
        settings[f"stage_{stage}_model"] = "glm-5.3-flash"
        settings[f"stage_{stage}_thinking_mode"] = "disabled"
        settings[f"stage_{stage}_reasoning_effort"] = "low"

    routes = [resolve_stage_request(settings, stage) for stage in (
        "segmentation", "segmentation_repair", "translation",
        "translation_repair", "calibration", "audit_repair",
    )]

    assert {row["base_url"] for row in routes} == {
        "https://open.bigmodel.cn/api/paas/v4"
    }
    assert {row["api_key"] for row in routes} == {"only-active-provider-key"}
    assert {row["model"] for row in routes} == {"glm-5.3-flash"}
    assert {row["thinking_mode"] for row in routes} == {"enabled"}
    assert {row["reasoning_effort"] for row in routes} == {"low"}


def test_calibration_runs_primary_then_real_fallback_and_reports_both_counts() -> None:
    calls: list[dict[str, object]] = []
    phases: list[tuple[str, int, int, int]] = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return {"actions": []}, {
            "model": kwargs["model"],
            "effective_thinking_mode": kwargs["thinking_mode"],
            "effective_reasoning_effort": kwargs["reasoning_effort"],
        }

    def fail_one_primary(stage: str, block_id: str, _attempt: int) -> None:
        if stage == "calibration" and block_id == "b2":
            raise Stage2Error("injected invalid primary block")

    settings = {
        "translation_api_key": "test-key",
        "translation_api_base_url": "https://open.bigmodel.cn/api/paas/v4",
        "translation_api_auth_mode": "bearer",
        "translation_api_model": "glm-5.3-flash",
        "stage_calibration_model": "glm-5.3-flash",
        "stage_calibration_thinking_mode": "enabled",
        "stage_calibration_reasoning_effort": "low",
        "stage_audit_repair_model": "glm-5.3-flash",
        "stage_audit_repair_thinking_mode": "enabled",
        "stage_audit_repair_reasoning_effort": "low",
        "translation_workers": 2,
        "http_retry_attempts": 1,
    }
    with patch.object(http_api, "call_translation_model", side_effect=fake_call):
        results = http_api._run_editor_ai_blocks(
            settings=settings,
            system_prompt="test",
            blocks={"b1": [], "b2": []},
            failure_key="actions",
            stage_name="calibration",
            retry_stage="audit_repair",
            response_validator=lambda _block_id, value: isinstance(value.get("actions"), list),
            phase_callback=lambda phase, done, total, accepted: phases.append(
                (phase, done, total, accepted)
            ),
            failure_injector=fail_one_primary,
        )

    assert len(results) == 2
    assert all(not metadata.get("error") for _block, _value, metadata in results)
    assert [row["stage"] for _block, _value, row in results].count("audit_repair") == 1
    assert phases[0] == ("repairing", 0, 1, 0)
    assert phases[-1] == ("repairing", 1, 1, 1)
    assert len(calls) == 2  # b1 primary + b2 fallback; injected b2 primary made no request
    assert all(call["api_key"] == "test-key" for call in calls)
    assert all(call["model"] == "glm-5.3-flash" for call in calls)
    assert all(call["thinking_mode"] == "enabled" for call in calls)
    assert all(call["reasoning_effort"] == "low" for call in calls)
    assert "rejected_output" not in calls[0]["groups"][0]
    repair_request = next(
        call["groups"][0] for call in calls
        if "rejected_output" in call["groups"][0]
    )
    assert repair_request["program_validation_error"] == "injected invalid primary block"
    assert repair_request["repair_attempt"] == 1


def test_translation_repairs_only_invalid_groups_and_reports_repair_denominator() -> None:
    groups = [{"group_id": "g1"}, {"group_id": "g2"}]
    progress: list[tuple[int, int, int]] = []

    def fake_plan(group, row):
        return {"group_id": group["group_id"]} if row else None

    def fake_repair(**kwargs):
        group = kwargs["group"]
        return {"group_id": group["group_id"]}, [{"valid": True}]

    with (
        patch.object(contextual, "_presentation_plan", side_effect=fake_plan),
        patch.object(contextual, "_repair_group", side_effect=fake_repair),
    ):
        plans, report = contextual.complete_results(
            settings={"translation_repair_attempts": 1, "translation_workers": 2},
            repair_prompt="repair",
            groups=groups,
            response={"group_results": [{"group_id": "g1"}]},
            progress_callback=lambda done, total, accepted: progress.append(
                (done, total, accepted)
            ),
        )

    assert {row["group_id"] for row in plans} == {"g1", "g2"}
    assert report["invalid_group_ids"] == []
    assert progress[0] == (0, 1, 0)
    assert progress[-1] == (1, 1, 1)


def test_translation_service_reads_unified_counted_progress(tmp_path) -> None:
    path = tmp_path / "progress.json"
    value = ai_progress(
        kind="translation", phase="executing", unit_label="个意义组",
        planned=7, completed=3,
    )
    path.write_text(json.dumps({"ai_progress": value}), encoding="utf-8")
    progress, message = _progress(path)
    assert progress == value["progress"]
    assert message == "模型处理 3/7 个意义组"


@patch("scripts.segmentation_support.requests.post")
def test_segmentation_shared_caller_enforces_glm_thinking_low(post: Mock) -> None:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{
            "message": {"content": "{}"},
            "finish_reason": "stop",
        }],
        "usage": {},
    }
    post.return_value = response

    _result, telemetry = call_model(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="glm-key",
        model="glm-5.3-flash",
        system_prompt="test",
        user_payload="{}",
        timeout=30,
        max_tokens=128,
        json_mode=False,
        thinking_mode="disabled",
        reasoning_effort="low",
        request_attempts=1,
    )

    payload = post.call_args.kwargs["json"]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "low"
    assert "temperature" not in payload
    assert telemetry["requested_thinking_mode"] == "disabled"
    assert telemetry["effective_thinking_mode"] == "enabled"
