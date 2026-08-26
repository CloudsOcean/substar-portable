from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_global_planner_ab import call_streaming_model  # noqa: E402
from scripts.run_stage1_pipeline import (  # noqa: E402
    Stage1PipelineError,
    resolve_api_key,
    write_json,
)


def load_text(package: Path, name: str) -> str:
    path = package / name
    if not path.is_file():
        raise Stage1PipelineError(f"盲测包缺少文件：{name}")
    return path.read_text(encoding="utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="用同一盲测包调用 DeepSeek 做全片一步/反思切分"
    )
    parser.add_argument("--package-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-tokens", type=int, default=128000)
    args = parser.parse_args()

    package = args.package_dir.resolve()
    manifest = json.loads(load_text(package, "00_manifest.json"))
    stage = str(manifest.get("stage", ""))
    if stage not in {"generate", "reflect"}:
        raise Stage1PipelineError(f"未知盲测阶段：{stage}")

    task = load_text(package, "01_task.md")
    schema = load_text(package, "05_output_schema.json")
    user_parts = [
        "# MASTER_TRANSCRIPT\n" + load_text(package, "03_master_transcript.txt"),
        "# ALIGNMENT_JSONL\n" + load_text(package, "04_alignment.jsonl"),
    ]
    if stage == "reflect":
        user_parts.append(
            "# INITIAL_CUTS\n" + load_text(package, "06_initial_cuts.json")
        )

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise Stage1PipelineError("未配置 DeepSeek API 密钥")
    result, telemetry = call_streaming_model(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        system="\n\n".join(
            [
                task,
                "# OUTPUT_SCHEMA\n" + schema,
                (
                    "# EXECUTION_CONTRACT\n"
                    "直接执行任务，只返回 JSON，不解释。必须完整扫描输入；"
                    "不得读取或假设任何外部参考。"
                ),
            ]
        ),
        user="\n\n".join(user_parts),
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        reasoning_effort="max",
        raw_response_path=output / "raw_response.txt",
    )
    write_json(output / "direct_cuts.json", result)
    telemetry["stage"] = stage
    telemetry["api_key_source"] = key_source
    telemetry["package_dir"] = str(package)
    write_json(output / "api_call.json", telemetry)
    write_json(
        output / "provenance.json",
        {
            "schema_version": "substar.direct-api-provenance.v1",
            "model": args.model,
            "stage": stage,
            "package_dir": str(package),
            "external_reference_used": False,
            "request_count": 1,
        },
    )
    print(
        f"complete model={args.model} stage={stage} "
        f"cuts={len(result.get('cuts_after', []))}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
