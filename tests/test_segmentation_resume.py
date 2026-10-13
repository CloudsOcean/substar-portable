import json
from argparse import Namespace
from unittest.mock import patch

import pytest

from scripts.run_semantic_segmentation import (
    cached_semantic_grouping_responses, request_semantic_grouping_block,
    semantic_grouping_binding, SegmentationRequestError,
)
from substar_core.segmentation.material import AlignmentUnit
from substar_core.segmentation.worker import collect_resume_responses


def fixture(tmp_path):
    units = [AlignmentUnit(index=i, start=i, end=i + .8, text="word") for i in range(52)]
    args = Namespace(
        checkpoint_dir=tmp_path / "checkpoints", checkpoint_scope="same-input",
        checkpoint_attempt=1, source_language="en", hard_limit=55, repair_attempts=1,
        output_dir=tmp_path, sentence_boundary_policy="unpunctuated",
        grouping_model="model-a", base_url="https://example.test", api_key="test",
        auth_mode="bearer", timeout=30, api_telemetry=[], grouping_thinking_mode="disabled",
        grouping_reasoning_effort="low", grouping_max_tokens=4096, grouping_temperature=0,
    )
    return units, args


def response(units, index):
    _, binding = semantic_grouping_binding(units, index, index, index + 1,
                                          sentence_boundary_policy="unpunctuated")
    return {"schema_version": "substar.semantic-grouping-result.v1", **binding,
            "meaning_groups": [{"alignment_start": index, "alignment_end": index,
                                "line_breaks_after": [index]}], "exceptions": []}


def call(units, args, index=0, **kwargs):
    return request_semantic_grouping_block(units, (index, index), index + 1,
                                          args, "prompt", [], **kwargs)


def test_fifty_of_fifty_two_blocks_survive_restart_and_model_change(tmp_path):
    units, args = fixture(tmp_path)
    with patch("scripts.run_semantic_segmentation.model_cue_script") as model:
        for index in range(50):
            model.return_value = response(units, index)
            call(units, args, index)
        assert model.call_count == 50
        model.side_effect = SegmentationRequestError("HTTP 429: rate limit", status=429)
        for index in (50, 51):
            with pytest.raises(SegmentationRequestError):
                call(units, args, index)
        assert len(list(args.checkpoint_dir.glob("*.json"))) == 50
    # A fresh args object simulates a subsequent process, including another model.
    _, restarted = fixture(tmp_path)
    restarted.grouping_model = "model-b"
    restarted.checkpoint_attempt = 3
    with patch("scripts.run_semantic_segmentation.model_cue_script") as model:
        for index in range(52):
            model.return_value = response(units, index)
            call(units, restarted, index)
        assert model.call_count == 2
    origins = [json.loads(p.read_text("utf-8"))["origin"]["model"]
               for p in args.checkpoint_dir.glob("*.json")]
    assert origins.count("model-a") == 50
    assert origins.count("model-b") == 2


def test_repaired_merged_result_is_durable(tmp_path):
    units, args = fixture(tmp_path)
    with patch("scripts.run_semantic_segmentation.model_cue_script",
               side_effect=[{"invalid": True}, response(units, 0)]) as model:
        assert not call(units, args)[5]
        assert model.call_count == 2
    with patch("scripts.run_semantic_segmentation.model_cue_script") as model:
        assert not call(units, args)[5]
        model.assert_not_called()


@pytest.mark.parametrize("damage", ["{", "[]", '{"accepted":true}'])
def test_corrupt_checkpoint_only_retries_affected_block(tmp_path, damage):
    units, args = fixture(tmp_path)
    with patch("scripts.run_semantic_segmentation.model_cue_script") as model:
        for index in range(2):
            model.return_value = response(units, index)
            call(units, args, index)
    path = next(args.checkpoint_dir.glob("*.json"))
    broken_index = int(json.loads(path.read_text("utf-8"))["block_id"][1:]) - 1
    path.write_text(damage, encoding="utf-8")
    with patch("scripts.run_semantic_segmentation.model_cue_script",
               return_value=response(units, broken_index)) as model:
        for index in range(2):
            call(units, args, index)
        assert model.call_count == 1


@pytest.mark.parametrize("changed", ["scope", "limit", "text", "time", "language"])
def test_changed_input_does_not_reuse_checkpoint(tmp_path, changed):
    units, args = fixture(tmp_path)
    with patch("scripts.run_semantic_segmentation.model_cue_script", return_value=response(units, 0)):
        call(units, args)
    if changed == "scope":
        args.checkpoint_scope = "other-input"
    elif changed == "limit":
        args.hard_limit = 30
    elif changed == "language":
        args.source_language = "zh"
    else:
        units[0] = AlignmentUnit(index=0, start=0, end=.7 if changed == "time" else .8,
                                 text="new" if changed == "text" else "word")
    with patch("scripts.run_semantic_segmentation.model_cue_script", return_value=response(units, 0)) as model:
        call(units, args)
        model.assert_called_once()


def event(value, repair=False, legacy=False):
    return "SUBSTAR_RUNTIME_EVENT\t" + json.dumps({
        "event": f"semantic_grouping{'_repair' if repair else ''} c0001 API response",
        "payload": {"output" if legacy else "finalizedOutput": value}})


def test_all_historical_logs_keep_valid_repair_despite_newer_invalid_response(tmp_path):
    units, args = fixture(tmp_path)
    good = response(units, 0)
    request, material = {"input": "same"}, {"text": "same"}
    for attempt, text in [(1, event({"invalid": 1}, legacy=True)),
                          (2, event(good, repair=True)), (10, event({"invalid": 2}))]:
        root = tmp_path / "attempts" / str(attempt)
        (root / "artifacts").mkdir(parents=True)
        (root / "work").mkdir()
        (root / "artifacts/segmentation_request.json").write_text(json.dumps(request))
        (root / "work/segmentation_material.json").write_text(json.dumps(material))
        (root / "work/algorithm.stdout.log").write_text(text)
    work = tmp_path / "attempts/11/work"
    work.mkdir(parents=True)
    log = collect_resume_responses(work, 11, request, material)
    cache = cached_semantic_grouping_responses(log, all_candidates=True)
    assert cache["c0001"][0] == {"invalid": 2}
    with patch("scripts.run_semantic_segmentation.model_cue_script") as model:
        call(units, args, cached_value=cache["c0001"])
        model.assert_not_called()
    assert len(list(args.checkpoint_dir.glob("*.json"))) == 1
    assert collect_resume_responses(work, 11, {"input": "different"}, material) is None


def test_parser_tolerates_malformed_records_and_old_field(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_text("\n".join(["SUBSTAR_RUNTIME_EVENT\t[]", "SUBSTAR_RUNTIME_EVENT\t{",
        'SUBSTAR_RUNTIME_EVENT\t{"payload": [1]}', event({"old": True}, legacy=True),
        event({"repaired": True}, repair=True)]))
    assert cached_semantic_grouping_responses(path) == {"c0001": {"repaired": True}}
