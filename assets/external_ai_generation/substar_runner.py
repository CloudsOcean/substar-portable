#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from execution_planner import execution_block_plan
from semantic_execution import validate_presentation_plan


ROOT = Path(__file__).resolve().parent
RESULTS = {
    "split": ROOT / "01_split_result.json",
    "calibration": ROOT / "02_corrected_result.json",
    "translation": ROOT / "03_translated_result.json",
}
SRT_RESULTS = {
    "split": ROOT / "01_split_result.srt",
    "calibration": ROOT / "02_corrected_result.srt",
    "translation": ROOT / "03_translated_result.srt",
}
CHECKPOINT_SCHEMA = "substar.external-ai-generation-checkpoint.v1"


class TaskError(ValueError):
    pass


def read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskError(f"无法读取有效 JSON：{path.relative_to(ROOT)}：{exc}") from exc
    if not isinstance(value, dict):
        raise TaskError(f"JSON 顶层必须是对象：{path.relative_to(ROOT)}")
    return value


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def srt_time(value: float) -> str:
    milliseconds = max(0, round(float(value) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def write_stage_srt(stage: str, cues: list[dict[str, Any]]) -> None:
    rows: list[str] = []
    for number, cue in enumerate(cues, start=1):
        source = str(
            cue.get("calibrated_text")
            if stage != "split" and cue.get("calibrated_text") is not None
            else cue.get("source_text", "")
        ).strip()
        if stage == "translation":
            target = str(cue.get("target_text") or "").strip()
            text = f"{source}\n{target}" if source and target else target or source
        else:
            text = source
        rows.append(
            f"{number}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{text}\n"
        )
    path = SRT_RESULTS[stage]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(rows), encoding="utf-8-sig")
    temporary.replace(path)


def inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    task = read(ROOT / "SUBSTAR_TASK.json")
    material = read(ROOT / "source_material.json")
    plan = read(ROOT / "work_plan.json")
    if task.get("schema_version") != "substar.external-ai-command-task.v1":
        raise TaskError("任务协议版本不受支持")
    if material.get("task_id") != task.get("task_id") or plan.get("task_id") != task.get("task_id"):
        raise TaskError("任务、材料与工作计划绑定不一致")
    if digest(material.get("tokens")) != task.get("source_fingerprint"):
        raise TaskError("源词元指纹不匹配")
    if digest(plan) != task.get("work_plan_sha256"):
        raise TaskError("工作计划指纹不匹配")
    return task, material, plan


def active_plan(task: dict[str, Any], initial: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / "downstream_work_plan.json"
    if not path.exists():
        return initial
    value = read(path)
    if value.get("task_id") != task.get("task_id"):
        raise TaskError("下游工作计划与任务绑定不一致")
    return value


def split_checkpoint(task: dict[str, Any]) -> dict[str, Any]:
    value = read(RESULTS["split"])
    validate_checkpoint(value, task, "split")
    return value


def visible_count(text: str, count_spaces: bool) -> int:
    return len(text) if count_spaces else sum(not char.isspace() for char in text)


def checkpoint(task: dict[str, Any], stage: str, completed: list[str], parent: str | None,
               cues: list[dict[str, Any]], semantic_groups: list[dict[str, Any]],
               translation_plan: dict[str, Any] | None = None,
               work_plan_sha256: str | None = None) -> dict[str, Any]:
    value = {
        "schema_version": CHECKPOINT_SCHEMA,
        "task_id": task["task_id"],
        "project_id": task["project_id"],
        "source_revision_id": task["source_revision_id"],
        "source_document_hash": task["source_document_hash"],
        "source_fingerprint": task["source_fingerprint"],
        "checkpoint": stage,
        "completed_stages": completed,
        "parent_checkpoint_sha256": parent,
        "languages": task["languages"],
        "hard_limits": task["hard_limits"],
        "work_plan_sha256": work_plan_sha256 or task["work_plan_sha256"],
        "cues": cues,
        "semantic_groups": semantic_groups,
    }
    if translation_plan is not None:
        value["translation_plan"] = translation_plan
    value["checkpoint_sha256"] = digest(value)
    return value


def validate_checkpoint(value: dict[str, Any], task: dict[str, Any], stage: str) -> None:
    supplied = value.get("checkpoint_sha256")
    unhashed = dict(value)
    unhashed.pop("checkpoint_sha256", None)
    if value.get("schema_version") != CHECKPOINT_SCHEMA or value.get("checkpoint") != stage:
        raise TaskError(f"{stage} checkpoint 协议无效")
    if value.get("task_id") != task.get("task_id") or supplied != digest(unhashed):
        raise TaskError(f"{stage} checkpoint 绑定或哈希无效")


def decisions(stage: str, plan: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    values = []
    for block in plan["blocks"]:
        path = ROOT / "decisions" / stage / f"{block['block_id']}.json"
        decision = read(path)
        if decision.get("task_id") != plan["task_id"] or decision.get("block_id") != block["block_id"]:
            raise TaskError(f"{path.relative_to(ROOT)} 的任务或工作块绑定不一致")
        values.append((block, decision))
    return values


def finalize_split() -> Path:
    task, material, plan = inputs()
    tokens = material["tokens"]
    by_index = {int(row["index"]): row for row in tokens}
    boundaries: list[str] = []
    group_ranges: list[tuple[str, int, int]] = []
    for block, decision in decisions("split", plan):
        semantic = decision.get("semantic_result")
        groups = semantic.get("meaning_groups") if isinstance(semantic, dict) else None
        if not isinstance(groups, list) or not groups:
            raise TaskError(f"{block['block_id']} 缺少模型 meaning_groups")
        cursor = int(block["alignment_start"])
        for local_index, group in enumerate(groups, start=1):
            start = int(group.get("alignment_start", -1))
            end = int(group.get("alignment_end", -1))
            line_breaks = group.get("line_breaks_after")
            if (
                start != cursor or end < start or not isinstance(line_breaks, list)
                or [int(value) for value in line_breaks] != sorted(set(int(value) for value in line_breaks))
                or int(line_breaks[-1]) != end
            ):
                raise TaskError(f"{block['block_id']} 的语义组覆盖或行边界无效")
            if any(index not in by_index for index in range(start, end + 1)):
                raise TaskError(f"{block['block_id']} 的语义组包含未知词元")
            group_ranges.append((f"meaning_group_{len(group_ranges) + 1:04d}", start, end))
            boundaries.extend(by_index[int(index)]["token_id"] for index in line_breaks)
            cursor = end + 1
        if cursor != int(block["alignment_end"]) + 1:
            raise TaskError(f"{block['block_id']} 的语义组未完整覆盖工作块")
    if boundaries and boundaries[-1] == tokens[-1]["token_id"]:
        boundaries.pop()
    positions = {row["token_id"]: index for index, row in enumerate(tokens)}
    if len(boundaries) != len(set(boundaries)) or any(value not in positions for value in boundaries):
        raise TaskError("切分结果包含重复或未知边界")
    boundaries.sort(key=positions.__getitem__)
    stops = [positions[value] + 1 for value in boundaries] + [len(tokens)]
    starts = [0] + [positions[value] + 1 for value in boundaries]
    cues = []
    for number, (start, stop) in enumerate(zip(starts, stops), start=1):
        rows = tokens[start:stop]
        text = " ".join(str(row["text"]) for row in rows)
        count = visible_count(text, bool(task["counting"]["source_count_spaces"]))
        if count > int(task["hard_limits"]["source"]):
            raise TaskError(f"Cue {number} 源文超过限制：{count}/{task['hard_limits']['source']}")
        cues.append({
            "cue_id": f"gen_cue_{number:04d}", "index": number - 1,
            "start": rows[0]["start"], "end": rows[-1]["end"],
            "speaker": rows[0].get("speaker"),
            "token_ids": [row["token_id"] for row in rows],
            "source_text": text, "calibrated_text": None, "target_text": None,
            "source_character_count": count,
        })
    cue_by_token = {token_id: cue["cue_id"] for cue in cues for token_id in cue["token_ids"]}
    semantic_groups = []
    for group_id, start, end in group_ranges:
        cue_ids = list(dict.fromkeys(cue_by_token[by_index[index]["token_id"]] for index in range(start, end + 1)))
        semantic_groups.append({"group_id": group_id, "cue_ids": cue_ids})
    allowed_after = {end for _group_id, _start, end in group_ranges[:-1]}
    replanned = execution_block_plan(
        tokens,
        target_seconds=float(plan.get("target_block_seconds", 90)),
        minimum_seconds=float(plan.get("minimum_block_seconds", 75)),
        maximum_seconds=float(plan.get("maximum_block_seconds", 100)),
        allowed_after=allowed_after or None,
        basis="accepted_ai_semantic_groups",
    )
    downstream_blocks = []
    for block in replanned["blocks"]:
        owned = [
            by_index[index]["token_id"]
            for index in range(int(block["alignment_start"]), int(block["alignment_end"]) + 1)
        ]
        downstream_blocks.append({**block, "owned_token_ids": owned})
    downstream_plan = {
        "schema_version": "substar.external-ai-work-plan.v1",
        "task_id": task["task_id"],
        "basis": replanned["basis"],
        "target_block_seconds": replanned["target_seconds"],
        "minimum_block_seconds": replanned["minimum_seconds"],
        "maximum_block_seconds": replanned["maximum_seconds"],
        "duration_seconds": max(0.0, float(tokens[-1]["end"]) - float(tokens[0]["start"])),
        "block_count": len(downstream_blocks),
        "blocks": downstream_blocks,
        "exceptions": replanned["exceptions"],
    }
    write(ROOT / "downstream_work_plan.json", downstream_plan)
    value = checkpoint(
        task, "split", ["split"], None, cues, semantic_groups,
        work_plan_sha256=digest(downstream_plan),
    )
    write(RESULTS["split"], value)
    write_stage_srt("split", cues)
    return RESULTS["split"]


def owner_block(cue: dict[str, Any], plan: dict[str, Any]) -> str:
    first = cue["token_ids"][0]
    for block in plan["blocks"]:
        if first in block["owned_token_ids"]:
            return str(block["block_id"])
    raise TaskError(f"Cue 无法映射工作块：{cue['cue_id']}")


def prepare(stage: str) -> None:
    task, material, initial_plan = inputs()
    plan = active_plan(task, initial_plan)
    source = split_checkpoint(task) if stage == "calibration" else read(RESULTS["calibration"])
    if stage != "calibration":
        validate_checkpoint(source, task, "corrected")
    token_by_id = {row["token_id"]: row for row in material["tokens"]}
    cue_by_id = {cue["cue_id"]: cue for cue in source["cues"]}
    for block in plan["blocks"]:
        block_id = block["block_id"]
        owned = [cue for cue in source["cues"] if owner_block(cue, plan) == block_id]
        value: dict[str, Any] = {
            "task_id": task["task_id"], "stage": stage, "block_id": block_id,
            "owned_cue_ids": [cue["cue_id"] for cue in owned],
        }
        if stage == "calibration":
            value["cues_with_context"] = [{
                **cue,
                "tokens": [{"token_id": token_id, "text": token_by_id[token_id]["text"]} for token_id in cue["token_ids"]],
            } for cue in owned]
            template = {"task_id": task["task_id"], "block_id": block_id, "actions": []}
        elif stage == "translation":
            owned_ids = {cue["cue_id"] for cue in owned}
            value["groups"] = [{
                "group_id": group["group_id"],
                "semantic_group_ids": [group["group_id"]],
                "cues": [{
                    "cue_id": cue_id, "source_text": cue_by_id[cue_id]["calibrated_text"],
                    "start": cue_by_id[cue_id]["start"], "end": cue_by_id[cue_id]["end"],
                    "hard_limit": int(task["hard_limits"]["target"]),
                    "count_rule": "characters_including_spaces" if task["counting"]["target_count_spaces"] else "characters_excluding_spaces",
                } for cue_id in group["cue_ids"]],
            } for group in source["semantic_groups"] if set(group["cue_ids"]) <= owned_ids]
            template = {"task_id": task["task_id"], "block_id": block_id, "group_results": []}
        else:
            raise TaskError("prepare 只支持 calibration 或 translation")
        write(ROOT / "stage_material" / stage / f"{block_id}.json", value)
        decision_path = ROOT / "decisions" / stage / f"{block_id}.json"
        if not decision_path.exists():
            write(decision_path, template)


def apply_actions(cue: dict[str, Any], tokens: dict[str, dict[str, Any]],
                  actions: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    ids = cue["token_ids"]
    positions = {token_id: index for index, token_id in enumerate(ids)}
    original = [str(tokens[token_id]["text"]) for token_id in ids]
    result, changed, accepted = list(original), set(), []
    for action in actions:
        action_ids = action.get("token_ids")
        if not isinstance(action_ids, list) or not action_ids or any(value not in positions for value in action_ids):
            raise TaskError(f"{cue['cue_id']} 包含无效校正词元")
        indexes = [positions[value] for value in action_ids]
        before = " ".join(original[index] for index in indexes)
        after = str(action.get("after_text", "")).strip()
        fragments = [part for part in after.split(" ") if part]
        if action.get("before_text") != before or len(fragments) != len(action_ids):
            raise TaskError(f"{action.get('action_id')} 不能安全回放")
        if action.get("disposition") == "apply":
            if changed.intersection(action_ids):
                raise TaskError("增量校正操作重叠")
            changed.update(action_ids)
            for index, fragment in zip(indexes, fragments):
                result[index] = fragment
        elif action.get("disposition") != "review":
            raise TaskError("增量校正 disposition 无效")
        accepted.append(dict(action))
    return " ".join(result), accepted


def finalize_calibration() -> Path:
    task, material, initial_plan = inputs()
    plan = active_plan(task, initial_plan)
    split = split_checkpoint(task)
    action_rows = [row for block, decision in decisions("calibration", plan) for row in decision.get("actions", [])]
    token_owner = {token_id: cue["cue_id"] for cue in split["cues"] for token_id in cue["token_ids"]}
    actions_by_cue = {cue["cue_id"]: [] for cue in split["cues"]}
    cue_by_id = {cue["cue_id"]: cue for cue in split["cues"]}
    token_map = {row["token_id"]: row for row in material["tokens"]}
    rejected = []
    for action in action_rows:
        owners = {token_owner.get(token_id) for token_id in action.get("token_ids", [])}
        if len(owners) != 1 or None in owners:
            rejected.append({"action": action, "reason": "action 必须完全属于一个 Cue"})
            continue
        cue_id = next(iter(owners))
        candidate = [*actions_by_cue[cue_id], action]
        try:
            candidate_text, _accepted = apply_actions(cue_by_id[cue_id], token_map, candidate)
            if visible_count(candidate_text, bool(task["counting"]["source_count_spaces"])) > int(task["hard_limits"]["source"]):
                raise TaskError("action 会使 Cue 超过源文硬上限")
        except TaskError as exc:
            rejected.append({"action": action, "reason": str(exc)})
            continue
        actions_by_cue[cue_id] = candidate
    cues = []
    for original in split["cues"]:
        cue = dict(original)
        text, operations = apply_actions(cue, token_map, actions_by_cue[cue["cue_id"]])
        count = visible_count(text, bool(task["counting"]["source_count_spaces"]))
        if count > int(task["hard_limits"]["source"]):
            raise TaskError(f"{cue['cue_id']} 校正后超过源文限制")
        cue.update(calibrated_text=text, calibrated_character_count=count, calibration_operations=operations)
        cues.append(cue)
    value = checkpoint(
        task, "corrected", ["split", "calibration"], split["checkpoint_sha256"],
        cues, split["semantic_groups"], work_plan_sha256=digest(plan),
    )
    write(RESULTS["calibration"], value)
    write_stage_srt("calibration", cues)
    write(ROOT / "calibration_rejections.json", {
        "schema_version": "substar.external-ai-calibration-rejections.v1",
        "rejected": rejected,
    })
    return RESULTS["calibration"]


def finalize_translation() -> Path:
    task, _material, initial_plan = inputs()
    plan = active_plan(task, initial_plan)
    corrected = read(RESULTS["calibration"])
    validate_checkpoint(corrected, task, "corrected")
    cue_by_id = {cue["cue_id"]: cue for cue in corrected["cues"]}
    groups = {group["group_id"]: group for group in corrected["semantic_groups"]}
    rows = [row for _block, decision in decisions("translation", plan) for row in decision.get("group_results", [])]
    row_by_id = {str(row.get("group_id", "")): row for row in rows if isinstance(row, dict)}
    plans = []
    target_by_cue: dict[str, str] = {}
    for group_id, group in groups.items():
        contract_group = {
            "group_id": group_id,
            "cues": [{
                "cue_id": cue_id, "start": cue_by_id[cue_id]["start"], "end": cue_by_id[cue_id]["end"],
                "hard_limit": int(task["hard_limits"]["target"]),
                "count_rule": "characters_including_spaces" if task["counting"]["target_count_spaces"] else "characters_excluding_spaces",
            } for cue_id in group["cue_ids"]],
        }
        presentation = validate_presentation_plan(contract_group, row_by_id.get(group_id))
        if presentation is None:
            raise TaskError(f"{group_id} 的多对多翻译计划无效")
        plans.append(presentation)
        units = {unit["meaning_unit_id"]: unit["target_text"] for unit in presentation["meaning_units"]}
        for assignment in presentation["cue_assignments"]:
            target_by_cue[assignment["cue_id"]] = units[assignment["meaning_unit_id"]]
    if set(target_by_cue) != set(cue_by_id):
        raise TaskError("翻译计划没有完整覆盖全部 Cue")
    cues = []
    for original in corrected["cues"]:
        cue = dict(original)
        text = target_by_cue[cue["cue_id"]]
        cue["target_text"] = text
        cue["target_character_count"] = visible_count(text, bool(task["counting"]["target_count_spaces"]))
        cues.append(cue)
    value = checkpoint(
        task, "translated", ["split", "calibration", "translation"],
        corrected["checkpoint_sha256"], cues, corrected["semantic_groups"],
        {"group_results": plans}, work_plan_sha256=digest(plan),
    )
    write(RESULTS["translation"], value)
    write_stage_srt("translation", cues)
    return RESULTS["translation"]


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("stage", choices=["calibration", "translation"])
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("stage", choices=["split", "calibration", "translation"])
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.stage)
    elif args.stage == "split":
        print(finalize_split().name)
    elif args.stage == "calibration":
        print(finalize_calibration().name)
    else:
        print(finalize_translation().name)


if __name__ == "__main__":
    main()
