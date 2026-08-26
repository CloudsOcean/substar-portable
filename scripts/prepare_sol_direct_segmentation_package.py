from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from substar_core.stage1 import extract_alignment, extract_master  # noqa: E402


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


PROMPT = """# Substar 全片直接切分盲测

你只负责把完整源文划成可人工审阅的显示 cue，不翻译、不改写、不纠正 ASR。
输入 alignment 的 index 从首项连续到末项。输出每条 cue 结束位置
`cuts_after`；末 index 不放入 cuts。

## 硬约束

1. 每个 alignment 恰好覆盖一次，顺序不变，不增删文本。
2. 英文字母语言每条最多 55 个字符，计空格和标点。
3. 中文源文每条最多 24 个汉字；混合语言同时满足适用的硬上限。
4. 不得在词或 alignment 单元内部切分。
5. 不得形成空 cue、倒序或重复切点。
6. 不处理翻译、时间吸附、ASR 修正或专名改写。

## 质量优先级

1. 每条应能独立、自然地朗读和理解。
2. 优先保住固定搭配、短语动词、限定词与中心名词、介词与宾语、系词与
   补语、助动词与主要谓语、专名、数字单位及标题。
3. 避免把冠词、介词、连词、代词、助动词或单个修饰词悬空在任一侧。
4. 同时考虑语法闭合、表意中心和长度观感；长度均衡权重较低。
5. 独立言语行为、话轮变化、明确句界、语言切换和主题转折通常建立边界。
6. 不因 ASR 原句、停顿、技术窗口或追求等长而机械切分。
7. 不为减少 cue 数而把多个可独立翻译的交际中心塞入同一条。
8. 若完整结构超过 55，只在其内部真实成分边界切分；选择使左右两侧都最
   自然的边界，不得留下 `an / object`、`broadcast / program` 一类弱残片。
9. 语气词、笑声、极短问答可在语用独立时单独成条，不强行并入邻句。
10. 中文或混合语言只忠实切分现有单元，不猜测英文译文长度。

## 说话人旁路元数据

alignment 可能包含 `speaker_id`、`speaker_confidence` 和
`speaker_turn_start`，也可能全部为空。它们只是不完全可靠的软证据：

- 高置信度说话人变化通常支持建立边界，但不得覆盖语法和硬约束；
- 同一说话人不妨碍在完整意义或显示长度需要处切分；
- 低置信度或 unknown 必须忽略；
- 不推断说话人身份、性别或角色，也不因标签编号本身改变切分；
- 说话人变化附近仍要选择使左右语言结构自然的实际词边界。

## 构造例子

构造文本：

`Welcome to Harbor Signals the weekly science programme`

较自然的切法是：

`Welcome to Harbor Signals / the weekly science programme`

而不是：

`Welcome to Harbor / Signals the weekly science programme`

构造文本：

`turning each field visit into a public learning experience`

若必须拆，可采用：

`turning each field visit / into a public learning experience`

构造文本：

`The archive was prepared by Northwind Studio I am your guide Mira`

两个独立交际中心应分开：

`The archive was prepared by Northwind Studio / I am your guide Mira`

## 全片自检

输出前逐条复核：

- 是否超过 55；
- 是否切开强依附结构；
- 是否存在弱残片或多个交际中心；
- 是否漏掉、重复或越界；
- 开头、中段、结尾是否使用同一标准。

只输出一个 JSON 对象，不输出 Markdown 或思维链。
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="准备 Sol 全片直接切分/反思盲测白名单包"
    )
    parser.add_argument("--material", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=["generate", "reflect"], required=True)
    parser.add_argument("--initial-cuts", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--english-hard-limit", type=int, default=55)
    parser.add_argument("--chinese-hard-limit", type=int, default=24)
    args = parser.parse_args()
    if args.mode == "reflect" and not args.initial_cuts:
        parser.error("reflect 模式必须提供 --initial-cuts")

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    material = args.material.read_text(encoding="utf-8-sig")
    master = extract_master(material)
    units = extract_alignment(material)
    task = PROMPT.replace(
        "55", str(args.english_hard_limit)
    ).replace("24", str(args.chinese_hard_limit))
    profile_digest = ""
    if args.profile:
        profile_payload = json.loads(
            args.profile.read_text(encoding="utf-8-sig")
        )
        profile_digest = hashlib.sha256(
            json.dumps(
                profile_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        task += f"""

## 冻结配置摘要

本任务配置摘要为 `{profile_digest}`。输出必须原样写入
`profile_sha256`，以防把其他配置生成的旧响应粘贴到本任务。

配置文件同时保存了后续翻译和双行显示参数。本阶段只使用源语种、字符计数和
硬上限；不得执行翻译、改变标点或提前应用上下行显示策略。
"""
    if args.glossary:
        task += """

## 冻结术语表

`08_relay_glossary.json` 只用于识别并保护人名、地名、机构、品牌、节目名和
固定专业术语的内部边界。不得据此翻译、改写或纠正源文。
"""
    task += f"""

## 本次覆盖声明

`coverage_check` 必须严格输出以下三个字段，不得改用同义字段名：

```json
{{
  "complete": true,
  "alignment_start": {int(units[0].index)},
  "alignment_end": {int(units[-1].index)}
}}
```
"""
    if args.mode == "reflect":
        task += """

## 反思修订任务

`06_initial_cuts.json` 是模型第一轮生成的粗切稿，尚未经过人工编辑。
这是进入人工词槽编辑器前的第二轮独立复核。请重新检查全片每个边界，
不要假设第一稿已经得到用户确认，也不要为了制造变化而修改。

只改能明确改善左右两侧语法完整性、表意独立性、阅读节奏或硬约束的边界。
优先修复切入固定搭配、句法成分、限定词与中心词，以及留下严重悬空碎片的
边界；允许保留合理但不唯一的风格取舍。最终仍输出完整 `cuts_after`，
并在 `changes` 中记录每项 add/remove 及可泛化原因。输出随后会交给用户
人工终审，因此不得虚构用户锁定边界或擅自改写源文。
"""
    else:
        task += "\n`changes` 输出空数组。\n"
    (output / "01_task.md").write_text(task, encoding="utf-8")
    (output / "03_master_transcript.txt").write_text(master, encoding="utf-8")
    (output / "04_alignment.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "index": int(unit.index),
                    "start": float(unit.start),
                    "end": float(unit.end),
                    "text": str(unit.text),
                    "sentence_id": unit.sentence_id,
                    "sentence_start": bool(unit.sentence_start),
                    "sentence_end": bool(unit.sentence_end),
                    "speaker_id": unit.speaker_id,
                    "speaker_confidence": float(unit.speaker_confidence),
                    "speaker_turn_start": bool(
                        index > 0
                        and unit.speaker_id
                        and unit.speaker_id != units[index - 1].speaker_id
                        and unit.speaker_confidence >= 0.8
                        and units[index - 1].speaker_confidence >= 0.8
                    ),
                },
                ensure_ascii=False,
            )
            for index, unit in enumerate(units)
        )
        + "\n",
        encoding="utf-8",
    )
    write_json(
        output / "05_output_schema.json",
        {
            "schema_version": "substar.sol.direct-cuts.v1",
            "required": [
                "schema_version",
                "cuts_after",
                "changes",
                "review_flags",
                "coverage_check",
                *(["profile_sha256"] if profile_digest else []),
            ],
            "output_file": "result/direct_cuts.json",
            **(
                {"profile_sha256": profile_digest}
                if profile_digest
                else {}
            ),
        },
    )
    allowed = [
        "00_manifest.json",
        "01_task.md",
        "03_master_transcript.txt",
        "04_alignment.jsonl",
        "05_output_schema.json",
    ]
    if args.initial_cuts:
        shutil.copy2(args.initial_cuts, output / "06_initial_cuts.json")
        allowed.append("06_initial_cuts.json")
    if args.profile:
        shutil.copy2(args.profile, output / "02_relay_profile.json")
        allowed.append("02_relay_profile.json")
    if args.glossary:
        shutil.copy2(args.glossary, output / "08_relay_glossary.json")
        allowed.append("08_relay_glossary.json")
    manifest = {
        "schema_version": "substar.sol-direct-package.v1",
        "stage": args.mode,
        "allowed_read_paths": allowed,
        "allowed_output_paths": [
            "result/direct_cuts.json",
            "provenance.json",
        ],
        "forbidden_actions": [
            "read_parent_directory",
            "search_workspace",
            "read_human_reference",
            "read_pipeline_stage_outputs",
            "network_or_api",
            "modify_source",
        ],
    }
    write_json(output / "00_manifest.json", manifest)
    manifest["input_sha256"] = {
        relative: sha256(output / relative)
        for relative in allowed
        if relative != "00_manifest.json"
    }
    write_json(output / "00_manifest.json", manifest)
    print(f"prepared {args.mode}: {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
