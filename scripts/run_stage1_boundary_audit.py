from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_stage1_pipeline import (  # noqa: E402
    call_model,
    read,
    resolve_api_key,
    shared_context,
    write_json,
)


PROMPT = PROJECT_ROOT / "prompts" / "03A_Q_全边界语义审计.md"
SCHEMA = PROJECT_ROOT / "schemas" / "stage1_boundary_audit.schema.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage1 全边界语义审计")
    parser.add_argument("material", type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--prompt",
        type=Path,
        default=PROMPT,
        help="可替换的独立审计提示词；输出 schema 保持不变",
    )
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    api_key, _ = resolve_api_key(args.api_key_env)
    if not api_key:
        raise RuntimeError("未配置 Stage1 LLM API key")
    material = read(args.material)
    plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    payload = "\n\n".join(
        [
            "# INPUT_MATERIAL\n" + material,
            "# CURRENT_PLAN\n" + json.dumps(plan, ensure_ascii=False),
        ]
    )
    result, telemetry = call_model(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        system_prompt="\n\n".join(
            [
                read(args.prompt),
                shared_context(material),
                "# OUTPUT_SCHEMA\n" + read(SCHEMA),
            ]
        ),
        user_payload=payload,
        timeout=args.timeout,
        max_tokens=16384,
        json_mode=True,
        thinking_mode="enabled",
        reasoning_effort="high",
        request_attempts=2,
    )
    if result.get("schema_version") != "substar.stage1.boundary-audit.v1":
        raise RuntimeError("边界审计 schema_version 错误")
    write_json(args.output, result)
    write_json(args.output.with_suffix(".telemetry.json"), telemetry)
    print(json.dumps({"issues": len(result.get("issues", []))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
