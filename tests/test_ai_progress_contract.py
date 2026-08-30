from __future__ import annotations

import json
from unittest.mock import patch

from substar_core.ai_progress import ai_progress
from substar_core.editor import http_api
from substar_core.editor.translation.service import _progress
from substar_core.stage2 import Stage2Error, Stage2RequestError


def test_translation_progress_is_counted_monotonic_and_uses_short_repair_label(
    tmp_path,
) -> None:
    values = [
        ai_progress(
            kind="translation", phase="executing", unit_label="个意义组",
            planned=4, completed=0,
        ),
        ai_progress(
            kind="translation", phase="executing", unit_label="个意义组",
            planned=4, completed=4,
        ),
        ai_progress(
            kind="translation", phase="repair", unit_label="个意义组",
            planned=4, completed=4, accepted=3, failed=1,
            repair_planned=1, repair_completed=1, repair_accepted=1,
        ),
        ai_progress(
            kind="translation", phase="completed", unit_label="个意义组",
            planned=4, completed=4,
        ),
    ]
    assert [row["progress"] for row in values] == sorted(
        row["progress"] for row in values
    )
    assert values[2]["message"] == "修复 1/1 个意义组 · 首轮通过 3/4"
    assert [row["label"] for row in values[2]["steps"]] == [
        "模型处理", "修复", "结果验收", "生成可编辑结果", "交付产物", "已交付",
    ]

    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"ai_progress": values[2]}), encoding="utf-8")
    progress, message = _progress(path)
    assert progress == values[2]["progress"]
    assert message == values[2]["message"]


def test_calibration_reports_primary_and_repair_block_counts() -> None:
    calls: list[dict[str, object]] = []
    primary: list[tuple[int, int]] = []
    repairs: list[tuple[str, int, int, int]] = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        return {"actions": []}, {"model": kwargs["model"]}

    def fail_one_primary(stage: str, block_id: str, _attempt: int) -> None:
        if stage == "calibration" and block_id == "b2":
            raise Stage2Error("injected invalid primary block")

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


def test_calibration_does_not_repair_authentication_failures() -> None:
    calls = 0

    def auth_failure(**_kwargs):
        nonlocal calls
        calls += 1
        raise Stage2RequestError("HTTP 401", status=401)

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
        except Stage2Error as exc:
            assert "401" in str(exc)
        else:
            raise AssertionError("authentication failure must fail the task")

    assert calls == 1
