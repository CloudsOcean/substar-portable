from __future__ import annotations

from unittest.mock import patch

from substar_core.ai_progress import ai_progress, progress_from_mapping
from substar_core.editor import http_api
from substar_core.cue_script import finalize_calibration_candidate, render_cue_request
from substar_core.model_gateway import ModelGatewayError, ModelGatewayRequestError


def test_translation_progress_is_counted_monotonic_and_uses_short_repair_label(
    tmp_path,
) -> None:
    values = [
        ai_progress(
            kind="translation", phase="executing", unit_label="块",
            planned=4, completed=0,
        ),
        ai_progress(
            kind="translation", phase="executing", unit_label="块",
            planned=4, completed=4,
        ),
        ai_progress(
            kind="translation", phase="repair", unit_label="块",
            planned=4, completed=4, accepted=3, failed=1,
            repair_planned=1, repair_completed=1, repair_accepted=1,
        ),
        ai_progress(
            kind="translation", phase="completed", unit_label="块",
            planned=4, completed=4,
        ),
    ]
    assert [row["progress"] for row in values] == sorted(
        row["progress"] for row in values
    )
    assert values[2]["message"] == "修复 1/1 块 · 首轮通过 3/4"
    assert [row["label"] for row in values[2]["steps"]] == [
        "模型处理", "修复", "结果验收", "生成可编辑结果", "交付产物", "已交付",
    ]

    projected = progress_from_mapping({"ai_progress": values[2]})
    assert projected is not None
    assert projected["progress"] == values[2]["progress"]
    assert projected["message"] == values[2]["message"]


def test_v1_progress_is_not_normalized_or_displayed() -> None:
    assert progress_from_mapping({
        "ai_progress": {
            "schema_version": "substar.ai-stage-progress.v1",
            "phase": "repairing",
        }
    }) is None


def test_calibration_reports_primary_and_repair_block_counts() -> None:
    calls: list[dict[str, object]] = []
    primary: list[tuple[int, int]] = []
    repairs: list[tuple[str, int, int, int]] = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return {"actions": []}, {"model": kwargs["model"]}

    def fail_one_primary(stage: str, block_id: str, _attempt: int) -> None:
        if stage == "calibration" and block_id == "b2":
            raise ModelGatewayError("injected invalid primary block")

    settings = {
        "translation_api_key": "test-key",
        "translation_api_base_url": "https://example.invalid",
        "translation_api_model": "model",
        "stage_calibration_model": "model",
        "stage_audit_repair_model": "repair-model",
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
            response_validator=lambda _block_id, value: isinstance(
                value.get("actions"), list
            ),
            progress_callback=lambda done, total: primary.append((done, total)),
            phase_callback=lambda phase, done, total, accepted: repairs.append(
                (phase, done, total, accepted)
            ),
            failure_injector=fail_one_primary,
        )

    assert all(not metadata.get("error") for _block, _value, metadata in results)
    assert primary[-1] == (2, 2)
    assert repairs[0] == ("repair", 0, 1, 0)
    assert repairs[-1] == ("repair", 1, 1, 1)
    assert len(calls) == 2
    repair_payload = next(
        call["groups"][0] for call in calls
        if "rejected_output" in call["groups"][0]
    )
    assert repair_payload["program_validation_error"] == (
        "injected invalid primary block"
    )
    assert "program_validation_errors" in repair_payload
    assert "frozen_accepted_output" in repair_payload


def test_calibration_repairs_all_rejected_items_and_freezes_accepted_output(
    tmp_path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"actions": [
                {"action_id": "accepted"},
                {"action_id": "rejected-a"},
                {"action_id": "rejected-b"},
            ]}, {}
        return {"actions": [
            {"action_id": "repaired-a"},
            {"action_id": "repaired-b"},
        ]}, {}

    def validate(_block_id, value):
        ids = [row.get("action_id") for row in value.get("actions", [])]
        if ids == ["accepted", "repaired-a", "repaired-b"]:
            return {
                "valid": True,
                "issues": [],
                "accepted_output": {"actions": list(value["actions"])},
            }
        return {
            "valid": False,
            "issues": [
                {"code": "invalid_action", "action_id": "rejected-a"},
                {"code": "invalid_action", "action_id": "rejected-b"},
            ],
            "accepted_output": {"actions": [{"action_id": "accepted"}]},
        }

    settings = {
        "translation_api_key": "test-key",
        "translation_api_base_url": "https://example.invalid",
        "translation_api_model": "model",
        "stage_calibration_model": "model",
        "stage_audit_repair_model": "repair-model",
        "translation_workers": 1,
    }
    with patch.object(http_api, "call_translation_model", side_effect=fake_call):
        result = http_api._run_editor_ai_blocks(
            settings=settings,
            system_prompt="primary prompt",
            repair_system_prompt="repair prompt",
            blocks={"b1": []},
            failure_key="actions",
            stage_name="calibration",
            retry_stage="audit_repair",
            response_validator=validate,
            cache_directory=tmp_path,
            cache_scope="contract-v3-test",
        )

    assert len(calls) == 2
    assert calls[0]["system_prompt"] == "primary prompt"
    assert calls[1]["system_prompt"] == "repair prompt"
    repair_payload = calls[1]["groups"][0]
    assert repair_payload["frozen_accepted_output"] == {
        "actions": [{"action_id": "accepted"}]
    }
    assert [row["action_id"] for row in result[0][1]["actions"]] == [
        "accepted", "repaired-a", "repaired-b",
    ]
    assert result[0][2]["primary_error"] == (
        "calibration returned an invalid response contract"
    )
    assert result[0][2]["primary_validation_issues"] == [
        {"code": "invalid_action", "action_id": "rejected-a"},
        {"code": "invalid_action", "action_id": "rejected-b"},
    ]
    # The rejected primary response is never cached; only the valid repair is.
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_calibration_raw_wire_cache_rebinds_current_token_ids(tmp_path) -> None:
    calls = 0

    def model(**_kwargs):
        nonlocal calls
        calls += 1
        return "CUE\tC001\tHello.", {}

    settings = {
        "translation_api_key": "test-key",
        "translation_api_base_url": "https://example.invalid",
        "translation_api_model": "model",
        "stage_calibration_model": "model",
        "translation_workers": 1,
    }
    renderer = lambda cues, feedback=None: render_cue_request(
        cues, task="CALIBRATE", instructions="complete rows",
        repair_feedback=feedback,
    )
    first_block = [{
        "cue_id": "old-cue", "editable": True,
        "tokens": [{"token_id": "old-token", "text": "hello"}],
    }]
    second_block = [{
        "cue_id": "new-cue", "editable": True,
        "tokens": [{"token_id": "new-token", "text": "hello"}],
    }]
    with patch.object(http_api, "call_text_model", side_effect=model):
        first = http_api._run_editor_ai_blocks(
            settings=settings, system_prompt="prompt", blocks={"old": first_block},
            failure_key="actions", stage_name="calibration", retry_stage=None,
            response_validator=lambda _block, value: not value.get("_cue_script_issues"),
            cache_directory=tmp_path, cache_scope="raw-cache-rebind-test",
            request_renderer=renderer,
            response_finalizer=finalize_calibration_candidate,
        )
        second = http_api._run_editor_ai_blocks(
            settings=settings, system_prompt="prompt", blocks={"new": second_block},
            failure_key="actions", stage_name="calibration", retry_stage=None,
            response_validator=lambda _block, value: not value.get("_cue_script_issues"),
            cache_directory=tmp_path, cache_scope="raw-cache-rebind-test",
            request_renderer=renderer,
            response_finalizer=finalize_calibration_candidate,
        )

    assert calls == 1
    assert first[0][2]["cache_hit"] is False
    assert second[0][2]["cache_hit"] is True
    assert first[0][1]["actions"][0]["token_ids"] == ["old-token"]
    assert second[0][1]["actions"][0]["token_ids"] == ["new-token"]


def test_calibration_does_not_repair_authentication_failures() -> None:
    calls = 0

    def auth_failure(**_kwargs):
        nonlocal calls
        calls += 1
        raise ModelGatewayRequestError("HTTP 401", status=401)

    settings = {
        "translation_api_key": "bad-key",
        "translation_api_base_url": "https://example.invalid",
        "translation_api_model": "model",
        "stage_calibration_model": "model",
        "stage_audit_repair_model": "repair-model",
        "translation_workers": 1,
        "http_retry_attempts": 3,
    }
    with patch.object(http_api, "call_translation_model", side_effect=auth_failure):
        try:
            http_api._run_editor_ai_blocks(
                settings=settings,
                system_prompt="test",
                blocks={"b1": []},
                failure_key="actions",
                stage_name="calibration",
                retry_stage="audit_repair",
            )
        except ModelGatewayError as exc:
            assert "401" in str(exc)
        else:
            raise AssertionError("authentication failure must fail the task")

    assert calls == 1
