from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_global_planner_ab import call_streaming_model  # noqa: E402
from scripts.run_stage1_pipeline import (  # noqa: E402
    Stage1PipelineError,
    resolve_api_key,
    shared_context,
    write_json,
)
from substar_core.stage1 import split_groups  # noqa: E402


TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[’'][A-Za-z0-9]+)*|[\u3400-\u9fff]")


def token_stream(text: str) -> list[str]:
    return [match.group(0).casefold() for match in TOKEN_RE.finditer(text)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="保持 Stage1 换行和空行不变，用 Pro 规范大小写、标点和高置信 ASR 错误"
    )
    parser.add_argument("--draft", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--api-key-env", default="SUBSTAR_LLM_API_KEY")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "max", "xhigh"), default="max")
    args = parser.parse_args()

    draft = args.draft.read_text(encoding="utf-8-sig").strip()
    before = split_groups(draft)
    api_key, key_source = resolve_api_key(args.api_key_env)
    if not api_key:
        raise Stage1PipelineError("未找到 Substar LLM API 密钥")

    schema = {
        "type": "object",
        "required": ["source_draft", "correction_notes"],
        "properties": {
            "source_draft": {"type": "string"},
            "correction_notes": {"type": "array", "items": {"type": "string"}},
        },
    }
    system = "\n\n".join(
        [
            "你是 Substar 公司字幕源文校对员。只做源文规范化，不做翻译、不改切分。",
            "必须逐行、逐组保持输入结构：每个非空行对应原来的同一行，空行位置完全不变；"
            "不得合并、拆分、移动或增删行。",
            "恢复自然大小写与本次配置允许的标点；修正高置信专名和明显 ASR 同音错误；"
            "保留口语语法和说话者真实措辞，不得润色改写。",
            "节目固定信息：Let's Meet；主持人 Alex；制作方 WCICO。"
            "本期埃及创作者姓名按语境统一为 Abdallah。",
            "若修正会改变意义或不能高度确定，保留原文。",
            "输出 JSON，source_draft 为完整成稿，correction_notes 仅简记实际修正。",
            shared_context(draft),
            "# OUTPUT_SCHEMA\n" + json.dumps(schema, ensure_ascii=False),
        ]
    )
    result, telemetry = call_streaming_model(
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        system=system,
        user="# SOURCE_DRAFT\n" + draft,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        reasoning_effort=args.reasoning_effort,
    )
    normalized = str(result.get("source_draft", "")).strip()
    after = split_groups(normalized)
    structure_valid = (
        len(before) == len(after)
        and [len(group) for group in before] == [len(group) for group in after]
    )
    similarity = difflib.SequenceMatcher(
        None, token_stream(draft), token_stream(normalized), autojunk=False
    ).ratio()
    if not structure_valid:
        raise Stage1PipelineError("源文规范化改变了换行或意义组结构")
    if similarity < 0.90:
        raise Stage1PipelineError(f"源文规范化改写过多：token_similarity={similarity:.4f}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(normalized + "\n", encoding="utf-8")
    write_json(
        args.output.with_suffix(".audit.json"),
        {
            "schema_version": "substar.stage1.source-normalization.v1",
            "model": args.model,
            "key_source": key_source,
            "group_count": len(after),
            "cue_count": sum(len(group) for group in after),
            "structure_preserved": structure_valid,
            "token_similarity": similarity,
            "correction_notes": result.get("correction_notes", []),
            "api_call": telemetry,
        },
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "group_count": len(after),
                "cue_count": sum(len(group) for group in after),
                "token_similarity": similarity,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
