from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_flash_map_pro_editor import (  # noqa: E402
    apply_editor_transactions,
    build_plan_from_cuts,
    flatten_plan_cuts,
    normalize_p4,
    protection_for_editor,
    system_prompt,
)
from scripts.run_global_planner_ab import call_streaming_model  # noqa: E402
from scripts.run_stage1_pipeline import (  # noqa: E402
    Stage1PipelineError,
    _direct_report,
    read,
    resolve_api_key,
    source_punctuation_kwargs,
    write_json,
    write_two_level_artifacts,
)
from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402
from substar_core.stage1_direct import evaluate_direct_plan  # noqa: E402


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise Stage1PipelineError(f"JSON 顶层必须是对象：{path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只对已冻结的 Sol P1/P2/P3 方案执行一次有限 Pro P4"
    )
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--input-route", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--max-transactions", type=int, default=12)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-tokens", type=int, default=64000)
    args = parser.parse_args()

    material = read(args.material)
    master = extract_master(material)
    units = extract_alignment(material)
    route = args.input_route
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    p1 = load_json(route / "p1_protection.json")
    p2 = load_json(route / "p2_meaning_groups.json")
    plan = load_json(route / "stage1_direct_plan.json")
    meaning_groups = p2["groups"]
    initial_cuts = flatten_plan_cuts(plan, int(units[-1].index))
    initial_result = evaluate_direct_plan(
        master,
        units,
        plan,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )
    if not initial_result.valid:
        raise Stage1PipelineError(
            f"冻结方案本身不合法：{initial_result.issues[:10]}"
        )

    budget = max(
        1,
        min(args.max_transactions, math.ceil(len(meaning_groups) * 0.05)),
    )
    api_key, key_source = resolve_api_key("SUBSTAR_LLM_API_KEY")
    if not api_key:
        raise Stage1PipelineError("未配置 Stage1 LLM API key")
    raw, telemetry = call_streaming_model(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        system=system_prompt("p4", material),
        user="\n\n".join(
            [
                "# FULL_PROGRAM\n" + material,
                "# P1_PROTECTION\n"
                + json.dumps(protection_for_editor(p1), ensure_ascii=False),
                "# P2_MEANING_GROUPS\n"
                + json.dumps(p2, ensure_ascii=False),
                "# P3_CURRENT_CUTS\n"
                + json.dumps(sorted(initial_cuts), ensure_ascii=False),
                "# P3_SOURCE_DRAFT\n```text\n"
                + initial_result.draft
                + "\n```",
                "# PROGRAM_RISK_NOTICES\n"
                + json.dumps(
                    initial_result.review_notices, ensure_ascii=False
                ),
                (
                    "# EDIT_BUDGET\n"
                    f"最多 {budget} 条事务；每条事务最多修改 6 个边界。"
                    "只处理明确硬错误或严重结构风险。"
                ),
            ]
        ),
        timeout=args.timeout,
        max_tokens=min(args.max_tokens, 64000),
        reasoning_effort="max",
    )
    patch = normalize_p4(
        raw,
        max_transactions=budget,
        max_break_edits_per_transaction=6,
    )
    final_cuts, transactions = apply_editor_transactions(
        initial_cuts,
        patch,
        master=master,
        units=units,
        protection=p1,
        meaning_groups=meaning_groups,
    )
    final_plan = build_plan_from_cuts(
        final_cuts, units, p1, meaning_groups
    )
    final_result = evaluate_direct_plan(
        master,
        units,
        final_plan,
        review_confidence=0.72,
        **source_punctuation_kwargs(),
    )
    if not final_result.valid:
        raise Stage1PipelineError(
            f"P4 应用后硬校验失败：{final_result.issues[:10]}"
        )

    write_json(output / "p1_protection.json", p1)
    write_json(output / "p2_meaning_groups.json", p2)
    write_json(output / "p3_frozen_plan.json", plan)
    write_json(output / "p4_editor_patch.json", patch)
    write_json(output / "p4_api_call.json", telemetry)
    write_json(
        output / "p4_transaction_results.json",
        {"transactions": transactions},
    )
    write_json(output / "stage1_direct_plan.json", final_plan)
    write_json(output / "stage1_display_layout_plan.json", final_plan)
    write_json(output / "stage1_translation_group_plan.json", p2)
    write_two_level_artifacts(output, master, units, final_plan)
    (output / "stage03A_source_draft.txt").write_text(
        final_result.draft, encoding="utf-8"
    )
    write_json(
        output / "stage1_validation.json",
        _direct_report(final_result, repaired=False, attempts=0),
    )
    write_json(
        output / "p4_route_audit.json",
        {
            "schema_version": "substar.sol-p4-route-audit.v1",
            "model": args.model,
            "api_key_source": key_source,
            "budget": budget,
            "proposed_transactions": len(patch["transactions"]),
            "accepted_transactions": sum(
                item.get("accepted") is True for item in transactions
            ),
            "rejected_transactions": sum(
                item.get("accepted") is False for item in transactions
            ),
            "initial_cue_count": len(plan["groups"]),
            "final_cue_count": len(final_plan["groups"]),
            "valid": final_result.valid,
        },
    )
    print(
        f"complete accepted="
        f"{sum(item.get('accepted') is True for item in transactions)} "
        f"final_cues={len(final_plan['groups'])} output={output}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
