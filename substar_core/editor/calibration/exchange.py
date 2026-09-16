"""External execution adapter for the production calibration protocol."""
import json
from dataclasses import replace
from substar_core.cue_script import render_cue_request, finalize_calibration_candidate
from substar_core.domain import ChangeKind, ChangeProvenance
from .service import (_editor_ai_cues, _calibration_model_blocks, calibration_prompts,
                      _validated_calibration_contract_actions, _apply_ai_calibration_operations,
                      BatchReplacement)

SCHEMA = "substar.external-calibration.v1"

def material(revision):
    tokens, cues = _editor_ai_cues(revision)
    if not tokens or not cues or any(not c["tokens"] for c in cues):
        raise ValueError("内置词元校准需要词级字幕；当前字幕工程没有可校准词元")
    rows = _calibration_model_blocks({"external": cues})["external"]
    request, ledger = render_cue_request(rows, task="CALIBRATE",
        instructions="Return every OWN Cue exactly once; use its local alias unchanged.")
    return tokens, cues, request, ledger

def export_calibration(project_id, revision, settings, glossary, instruction=""):
    _, cues, request, _ = material(revision)
    source_glossary = [{"source": r.get("source"), "standard_source": r.get("standard_source"),
                       "aliases": r.get("aliases", [])} for r in glossary]
    prompt, _ = calibration_prompts(settings, cues, source_glossary, instruction)
    envelope = {"schema_version": SCHEMA, "project_id": project_id,
                "revision_id": revision.revision_id, "output": "1|完整校准后原文\n2|完整校准后原文"}
    return "\n\n".join(["# 外部校准生成", prompt, request,
        "# 交付文件封装\n执行以上内置校准协议。将全部编号行作为 output 字符串，保留换行，"
        "封装为以下 JSON 文件。项目和版本标识原样保留。必须交付可下载的 UTF-8 .json 文件，文件名为 calibration_result.json。不能仅回复完成说明或把 JSON 放在 Markdown 代码块里。若环境不能创建附件，则只输出完整有效 JSON，供用户保存为 .json 文件；不附解释或 Markdown。",
        json.dumps(envelope, ensure_ascii=False, indent=2)])

def inspect_calibration(project_id, revision, payload):
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        raise ValueError("外部校准文件格式不正确")
    if payload.get("project_id") != project_id or payload.get("revision_id") != revision.revision_id:
        raise ValueError("项目或版本与导出时不同，请重新导出校准材料")
    if not isinstance(payload.get("output"), str):
        raise ValueError("缺少校准结果 output 文本")
    tokens, cues, _, ledger = material(revision)
    value = finalize_calibration_candidate(payload["output"], ledger)
    mapping = {t["token_id"]: c["cue_id"] for c in cues for t in c["tokens"]}
    actions, rejected = _validated_calibration_contract_actions(value, list(mapping), tokens, mapping)
    issues = [*value.get("_cue_script_issues", []), *rejected]
    if issues:
        raise ValueError("校准结果未通过内置校验：" + json.dumps(issues[:8], ensure_ascii=False))
    desired, merges, review = {}, [], []
    for action in actions:
        if action["disposition"] == "review":
            review.append(action)
        elif action["kind"] == "merge_span":
            merges.append({**action, "cue_id": mapping[action["token_ids"][0]]})
        else:
            parts = action["after_text"].split() if action["kind"] == "replace_span" else [action["after_text"]]
            for token_id, text in zip(action["token_ids"], parts, strict=True):
                desired[token_id] = text
    replacements = [BatchReplacement(token_id=k, text=v, expected_text=tokens[k].text)
                    for k, v in desired.items() if v != tokens[k].text]
    stale = list(dict.fromkeys(mapping[t] for a in actions if a["disposition"] == "apply"
                              and a["affects_translation"] for t in a["token_ids"]))
    metadata = {"external": True, "based_on_revision_id": revision.revision_id,
                "translation_stale_cue_ids": stale, "review_actions": review,
                "calibration_problem_cue_ids": list(dict.fromkeys(mapping[t] for a in review for t in a["token_ids"]))}
    document = _apply_ai_calibration_operations(revision.document, replacements=replacements,
        merge_actions=merges, calibration_metadata=metadata, operation_id="external_" + revision.revision_id)
    provenance = ChangeProvenance(kind=ChangeKind.AI, operation="ai_calibration_apply",
                                  actor="external-ai-calibration", metadata=metadata)
    document = replace(document, changes=(*document.changes, provenance))
    return document, provenance, {"replacement_count":len(replacements), "merge_count":len(merges),
        "review_count":len(review), "actions":actions, "review_actions":review}
