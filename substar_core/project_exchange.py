from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any, BinaryIO, Iterable, Mapping
import uuid
import zipfile

from substar_core.artifacts import atomic_write_json
from substar_core.document_operations import apply_document_operation
from substar_core.domain import (
    ChangeKind,
    ChangeProvenance,
    DisplayCue,
    EditorDocument,
    EntityState,
    SemanticGroup,
    stable_id,
)
from substar_core.language_layout import editor_token_fragments, layout_tokens, normalize_language
from substar_core.policy import count_visible_characters
from substar_core.semantic_execution import validate_presentation_plan
from substar_core.segmentation.execution_planner import execution_block_plan
from substar_core.export import SubtitleExportMode, render_document_srt
from substar_core.prompt_registry import (
    normalize_source_language,
    opposite_language,
    render_prompt,
    source_language_for_text,
    translation_variant,
)
from substar_core.storage import ProjectStore
from substar_core.task_info import save_task_info


EXCHANGE_SCHEMA = "substar.subtitle-project.v1"
EXTERNAL_PROOFTRANSLATION_SCHEMA = "substar.external-ai-prooftranslation.v1"
EXTERNAL_SPLIT_SCHEMA = "substar.external-ai-split.v1"
EXTERNAL_EDIT_SCHEMA = "substar.external-ai-edit.v1"
EXTERNAL_GENERATION_CHECKPOINT_SCHEMA_V2 = (
    "substar.external-ai-generation-checkpoint.prototype.v2"
)
EXTERNAL_GENERATION_CHECKPOINT_SCHEMA = (
    "substar.external-ai-generation-checkpoint.prototype.v3"
)
EXTERNAL_GENERATION_CHECKPOINT_SCHEMA_V1 = (
    "substar.external-ai-generation-checkpoint.v1"
)
MAX_ARCHIVE_FILES = 256
MAX_EXPANDED_BYTES = 8 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 250


class ProjectExchangeError(ValueError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cue_text(document: EditorDocument, cue: Any) -> str:
    tokens = {item.token_id: item for item in document.display_tokens}
    values = [tokens[token_id].text for token_id in cue.display_token_ids
              if tokens[token_id].state is not EntityState.DELETED]
    if not values:
        return ""
    return "".join(values) if any("\u3400" <= char <= "\u9fff" for value in values for char in value) else " ".join(values)


def _cue_hash(document: EditorDocument, cue: Any) -> str:
    return _sha256_bytes(_cue_text(document, cue).encode("utf-8"))


def _external_language_route(
    document: EditorDocument, source_language: str, target_language: str,
) -> tuple[str, str, str]:
    source = normalize_source_language(source_language)
    if source == "Auto":
        source = source_language_for_text(
            "\n".join(
                _cue_text(document, cue)
                for cue in document.cues
                if cue.state is EntityState.ACTIVE
            )
        )
    target = str(target_language or "auto_opposite").strip()
    if target.lower() in {"auto", "auto_opposite", "opposite"}:
        target = opposite_language(source)
    target = normalize_source_language(target)
    if target == "Auto":
        target = opposite_language(source)
    if source == target:
        raise ProjectExchangeError(f"外部 AI 的源语言与目标语言相同：{source}")
    return source, target, translation_variant(source, target)


def _active_route_block(
    *, source_language: str, target_language: str, translation_style: str,
    source_hard_limit: int, target_hard_limit: int,
    glossary: Iterable[Mapping[str, Any]] = (),
) -> str:
    language_names = {"zh-CN": "简体中文", "en": "英文", "ja": "日文", "ko": "韩文", "mixed": "中英混合"}
    block = "\n".join((
        "# 本项目的活动参数",
        f"源语言：{language_names.get(source_language, source_language)}（{source_language}）",
        f"目标语言：{language_names.get(target_language, target_language)}（{target_language}）",
        f"翻译风格：{translation_style}",
        f"源文每 Cue 硬上限：{int(source_hard_limit)} 个显示字符",
        f"译文每 Cue 硬上限：{int(target_hard_limit)} 个显示字符",
        "语言方向是本任务的硬约束；不得把源文润色稿当作目标语译文。",
    ))
    glossary_rows = [dict(row) for row in glossary if isinstance(row, Mapping)]
    if glossary_rows:
        block += "\n# 当前权威热词表\n" + json.dumps(
            glossary_rows, ensure_ascii=False, separators=(",", ":")
        )
    return block


def _calibration_variant(source_language: str) -> str:
    # This is deliberately identical to the editor calibration route.  The
    # current production calibration contract has Chinese and non-Chinese
    # variants; adding another external-only route would create prompt drift.
    return "zh" if source_language == "zh-CN" else "en"


def _prompt_bundle(*, orchestration_key: str, source_language: str,
                   translation_route: str | None = None) -> tuple[dict[str, Any], str]:
    orchestration = render_prompt(orchestration_key)
    snapshots: dict[str, Any] = {"orchestration": orchestration}
    if orchestration_key == "external_ai_split":
        split_variant = "zh" if source_language == "zh-CN" else source_language
        if split_variant not in {"en", "zh", "ja", "ko", "mixed"}:
            split_variant = "mixed"
        snapshots["segmentation"] = render_prompt("semantic_grouping", variant=split_variant)
    else:
        snapshots["calibration"] = render_prompt(
            "calibration", variant=_calibration_variant(source_language)
        )
        snapshots["translation"] = render_prompt(
            "contextual_translation", variant=translation_route or "generic"
        )
    metadata = {name: prompt.metadata() for name, prompt in snapshots.items()}
    digest_source = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = _sha256_bytes(digest_source.encode("utf-8"))
    sections = [snapshots["orchestration"].text]
    sections.append("# 当前活动业务提示词（逐字快照）")
    for name, title in (
        ("segmentation", "切分"), ("calibration", "校准"), ("translation", "翻译")
    ):
        prompt = snapshots.get(name)
        if prompt is not None:
            sections.append(f"## {title}\n\n{prompt.text}")
    return {"sha256": digest, "stages": metadata}, "\n\n".join(sections).strip()


def _exchange_items(document: EditorDocument) -> list[dict[str, Any]]:
    tokens = {item.token_id: item for item in document.display_tokens}
    items: list[dict[str, Any]] = []
    for cue in document.cues:
        if cue.state is EntityState.DELETED:
            continue
        items.append({
            "cue_id": cue.cue_id,
            "index": cue.index,
            "start": cue.start,
            "end": cue.end,
            "source_hash": _cue_hash(document, cue),
            "tokens": [
                {"token_id": token_id, "text": tokens[token_id].text}
                for token_id in cue.display_token_ids
                if tokens[token_id].state is not EntityState.DELETED
            ],
            "current_target_text": cue.target.target_text if cue.target else "",
        })
    return items


def external_prooftranslation_files(
    project_id: str, revision: Any, *, source_language: str, target_language: str,
    translation_style: str = "自然、准确、适合字幕阅读",
    source_hard_limit: int = 55, target_hard_limit: int = 55,
    glossary: Iterable[Mapping[str, Any]] = (),
) -> dict[str, bytes]:
    document = revision.document
    source_language, target_language, variant = _external_language_route(
        document, source_language, target_language
    )
    prompt_snapshot, execution_prompt = _prompt_bundle(
        orchestration_key="external_ai_prooftranslation",
        source_language=source_language,
        translation_route=variant,
    )
    items = _exchange_items(document)
    task = {
        "schema_version": EXTERNAL_PROOFTRANSLATION_SCHEMA,
        "task": "staged_correction_translation",
        "project_id": project_id,
        "document_id": document.document_id,
        "revision_id": revision.revision_id,
        "document_hash": revision.document_hash,
        "source_language": source_language,
        "target_language": target_language,
        "translation_style": translation_style,
        "source_hard_limit": int(source_hard_limit),
        "target_hard_limit": int(target_hard_limit),
        "prompt_snapshot": prompt_snapshot,
        "items": items,
        "return_schema": {
            "schema_version": EXTERNAL_PROOFTRANSLATION_SCHEMA,
            "task": "staged_correction_translation",
            "revision_id": "原值",
            "source_language": source_language,
            "target_language": target_language,
            "source_results": [{
                "cue_id": "原值", "source_hash": "原值",
                "tokens": [{"token_id": "原值", "text": "修正后文字"}],
            }],
            "translation_results": [{
                "cue_id": "原值", "source_hash": "原值",
                "target_text": "基于修正版的译文", "language": target_language,
            }],
        },
    }
    route_block = _active_route_block(
        source_language=source_language, target_language=target_language,
        translation_style=translation_style,
        source_hard_limit=source_hard_limit, target_hard_limit=target_hard_limit,
        glossary=glossary,
    )
    return {
        "01_外部AI校译任务.json": json.dumps(
            task, ensure_ascii=False, indent=2
        ).encode("utf-8-sig"),
        "02_执行提示词.md": f"{execution_prompt}\n\n{route_block}\n".encode("utf-8-sig"),
    }


def external_edit_files(
    project_id: str, revision: Any, *, source_language: str, target_language: str,
    translation_style: str = "自然、准确、适合字幕阅读",
    source_hard_limit: int = 55, target_hard_limit: int = 55,
    glossary: Iterable[Mapping[str, Any]] = (),
) -> dict[str, bytes]:
    document = revision.document
    source_language, target_language, variant = _external_language_route(
        document, source_language, target_language
    )
    prompt_snapshot, execution_prompt = _prompt_bundle(
        orchestration_key="external_ai_edit",
        source_language=source_language,
        translation_route=variant,
    )
    task = {
        "schema_version": EXTERNAL_EDIT_SCHEMA,
        "task": "external_finished_subtitle",
        "project_id": project_id,
        "document_id": document.document_id,
        "revision_id": revision.revision_id,
        "document_hash": revision.document_hash,
        "source_language": source_language,
        "target_language": target_language,
        "translation_style": translation_style,
        "source_hard_limit": int(source_hard_limit),
        "target_hard_limit": int(target_hard_limit),
        "prompt_snapshot": prompt_snapshot,
        "items": _exchange_items(document),
        "final_output_modes": ["source", "target", "ab-double", "ab-single", "all"],
        "default_output": {"mode": "ab-double", "order": "source-above-target", "encoding": "UTF-8"},
    }
    route_block = _active_route_block(
        source_language=source_language, target_language=target_language,
        translation_style=translation_style,
        source_hard_limit=source_hard_limit, target_hard_limit=target_hard_limit,
        glossary=glossary,
    )
    current_srt = render_document_srt(document, SubtitleExportMode.AB_DOUBLE)
    return {
        "01_外部AI编辑任务.json": json.dumps(
            task, ensure_ascii=False, indent=2
        ).encode("utf-8-sig"),
        "02_执行提示词.md": f"{execution_prompt}\n\n{route_block}\n".encode("utf-8-sig"),
        "03_当前版本.srt": current_srt.encode("utf-8-sig"),
    }


def _external_split_material(
    document: EditorDocument, *, allow_translations: bool = False
) -> dict[str, Any]:
    cues = sorted(document.cues, key=lambda item: item.index)
    if not cues or any(cue.state is not EntityState.ACTIVE for cue in cues):
        raise ProjectExchangeError("外部 AI 切分要求先恢复或清理所有已删除 Cue")
    if not allow_translations and any(cue.target is not None for cue in cues):
        raise ProjectExchangeError("外部 AI 切分必须在翻译前执行，不能丢弃或程序化重写既有译文")

    token_map = {token.token_id: token for token in document.display_tokens}
    source_map = {token.token_id: token for token in document.source_tokens}
    ordered_ids = [token_id for cue in cues for token_id in cue.display_token_ids]
    if len(ordered_ids) != len(set(ordered_ids)):
        raise ProjectExchangeError("外部 AI 切分不支持重复投影词元")
    if set(ordered_ids) != set(token_map):
        raise ProjectExchangeError("外部 AI 切分要求 Cue 完整且唯一地覆盖全部显示词元")
    if any(not token_map[token_id].source_token_ids for token_id in ordered_ids):
        raise ProjectExchangeError("外部 AI 切分不支持没有 ASR 时间血缘的手工词元")

    bounds: dict[str, tuple[float, float]] = {}
    visible_ids: list[str] = []
    records: list[dict[str, Any]] = []
    for token_id in ordered_ids:
        token = token_map[token_id]
        lineage = [source_map[source_id] for source_id in token.source_token_ids]
        bounds[token_id] = (
            min(item.start for item in lineage),
            max(item.end for item in lineage),
        )
        if token.state is EntityState.ACTIVE:
            visible_ids.append(token_id)
            records.append({
                "token_id": token_id,
                "text": token.text,
                "start": bounds[token_id][0],
                "end": bounds[token_id][1],
            })
    if len(visible_ids) < 2:
        raise ProjectExchangeError("外部 AI 切分至少需要两个可见词元")

    current_boundaries: list[str] = []
    for cue in cues[:-1]:
        candidates = [
            token_id for token_id in cue.display_token_ids
            if token_map[token_id].state is EntityState.ACTIVE
        ]
        if not candidates:
            raise ProjectExchangeError("外部 AI 切分不支持没有可见文字的 Cue")
        current_boundaries.append(cue.display_token_ids[-1])
    fingerprint = _sha256_bytes(json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    return {
        "cues": cues,
        "token_map": token_map,
        "source_map": source_map,
        "ordered_ids": ordered_ids,
        "visible_ids": visible_ids,
        "records": records,
        "bounds": bounds,
        "current_boundaries": current_boundaries,
        "fingerprint": fingerprint,
    }


def _external_generation_source_records(document: EditorDocument) -> list[dict[str, Any]]:
    material = _external_split_material(document, allow_translations=True)
    token_map = material["token_map"]
    source_map = material["source_map"]
    records: list[dict[str, Any]] = []
    for cue in material["cues"]:
        for token_id in cue.display_token_ids:
            token = token_map[token_id]
            if token.state is not EntityState.ACTIVE:
                continue
            lineage = [source_map[source_id] for source_id in token.source_token_ids]
            speaker = lineage[0].speaker
            records.append({
                "token_id": token.token_id,
                "index": len(records),
                "text": token.text,
                "start": min(item.start for item in lineage),
                "end": max(item.end for item in lineage),
                "speaker": speaker,
                "speaker_id": speaker,
                "current_cue_id": cue.cue_id,
            })
    return records


def external_generation_files(
    project_id: str, revision: Any, *, source_language: str, target_language: str,
    translation_style: str = "自然、准确、适合字幕阅读",
    source_hard_limit: int = 55, target_hard_limit: int = 55,
    glossary: Iterable[Mapping[str, Any]] = (),
    target_block_seconds: int = 90,
) -> dict[str, bytes]:
    """Build the self-running external generation package from production contracts."""
    document = revision.document
    source, target, route = _external_language_route(
        document, source_language, target_language
    )
    records = _external_generation_source_records(document)
    fingerprint = _sha256_bytes(json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    cue_group = {cue.cue_id: str(cue.group_id or "") for cue in document.cues}
    allowed_group_ends = {
        int(record["index"])
        for index, record in enumerate(records[:-1])
        if cue_group.get(str(record["current_cue_id"]), "")
        != cue_group.get(str(records[index + 1]["current_cue_id"]), "")
    }
    planned = execution_block_plan(
        records,
        target_seconds=max(75, min(100, int(target_block_seconds))),
        minimum_seconds=75,
        maximum_seconds=100,
        allowed_after=allowed_group_ends or None,
        basis="accepted_ai_semantic_groups",
    )
    by_index = {int(record["index"]): record for record in records}
    blocks: list[dict[str, Any]] = []
    for planned_block in planned["blocks"]:
        owned = [
            by_index[index]
            for index in range(
                int(planned_block["alignment_start"]),
                int(planned_block["alignment_end"]) + 1,
            )
        ]
        blocks.append({
            **planned_block,
            "owned_token_ids": [row["token_id"] for row in owned],
        })
    work_plan = {
        "schema_version": "substar.external-ai-work-plan.v1",
        "task_id": f"external_generation_{project_id}",
        "basis": planned["basis"],
        "target_block_seconds": int(planned["target_seconds"]),
        "minimum_block_seconds": 75,
        "maximum_block_seconds": 100,
        "duration_seconds": max(0.0, float(records[-1]["end"]) - float(records[0]["start"])),
        "block_count": len(blocks),
        "blocks": blocks,
        "exceptions": list(planned["exceptions"]),
    }
    work_plan_sha256 = _sha256_bytes(json.dumps(
        work_plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    task = {
        "schema_version": "substar.external-ai-command-task.v1",
        "task": "substar_external_generation",
        "task_id": work_plan["task_id"],
        "project_id": project_id,
        "source_revision_id": revision.revision_id,
        "source_document_hash": revision.document_hash,
        "source_fingerprint": fingerprint,
        "work_plan_sha256": work_plan_sha256,
        "languages": {"source": source, "target": target},
        "hard_limits": {"source": int(source_hard_limit), "target": int(target_hard_limit)},
        "counting": {
            "source_count_spaces": normalize_language(source) in {"en", "mixed"},
            "target_count_spaces": normalize_language(target) in {"en", "mixed"},
        },
        "translation_style": translation_style,
        "accepted_user_commands": {
            "执行切分": "split", "执行校正": "calibration", "执行翻译": "translation",
        },
        "program_entrypoint": "substar_runner.py",
    }
    source_material = {
        "schema_version": "substar.external-ai-source-material.v1",
        "task_id": task["task_id"],
        "tokens": records,
    }
    split_variant = "zh" if source == "zh-CN" else source
    if split_variant not in {"en", "zh", "ja", "ko", "mixed"}:
        split_variant = "mixed"
    prompts = {
        "prompts/01_split.md": render_prompt("semantic_grouping", variant=split_variant).text,
        "prompts/02_calibration.md": render_prompt(
            "calibration", variant=_calibration_variant(source)
        ).text,
        "prompts/03_translation.md": render_prompt(
            "contextual_translation", variant=route
        ).text,
    }
    route_block = _active_route_block(
        source_language=source, target_language=target,
        translation_style=translation_style,
        source_hard_limit=source_hard_limit, target_hard_limit=target_hard_limit,
        glossary=glossary,
    )
    protocol = """# Substar 外部 AI 生成协议

用户只需要说“执行切分”“执行校正”或“执行翻译”。你必须自行读取本包，不要要求用户解压或解释文件。

每个阶段：读取对应 prompts、stage_material 和 decisions；调用该阶段提示词完成语义工作；只把模型结果写入 decisions；然后运行 `python substar_runner.py finalize <stage>`。程序负责组装、计数、哈希与结构校验，模型不得手写最终 checkpoint。

- 执行切分：读取 source_material.json、work_plan.json 与 prompts/01_split.md，为每个块写 decisions/split/block_XXXX.json，再 finalize split。程序会在接受新的意义组后重新计算 downstream_work_plan.json。
- 执行校正：先运行 `python substar_runner.py prepare calibration`，读取 prompts/02_calibration.md 和生成的 stage_material，写增量 actions，再 finalize calibration。
- 执行翻译：先运行 `python substar_runner.py prepare translation`，读取 prompts/03_translation.md 和生成的多 Cue semantic groups，写 group_results（meaning_units + cue_assignments），再 finalize translation。

本包来自已经通过 AI 切分的版本，但仍必须先执行本包的切分任务；现有意义组仅用于组织输入 WorkPlan，不作为可跳过硬限制验收的结果。每一完整阶段都向用户交付根目录生成的 JSON 和 SRT；翻译阶段 SRT 为原文、译文双行。不得把独立 Cue 强制变成独立语义组；不得用程序生成或改写模型翻译。
"""
    files: dict[str, bytes] = {
        "SUBSTAR_TASK.json": json.dumps(task, ensure_ascii=False, indent=2).encode("utf-8-sig"),
        "source_material.json": json.dumps(source_material, ensure_ascii=False, indent=2).encode("utf-8-sig"),
        "work_plan.json": json.dumps(work_plan, ensure_ascii=False, indent=2).encode("utf-8-sig"),
        "AGENT_PROTOCOL.md": f"{protocol}\n{route_block}\n".encode("utf-8-sig"),
        "README_先读.md": "把 ZIP 交给支持文件与代码执行的 Web AI，然后只需说：执行切分、执行校正、执行翻译。\n".encode("utf-8-sig"),
        "substar_runner.py": (
            Path(__file__).resolve().parents[1] / "assets" / "external_ai_generation" / "substar_runner.py"
        ).read_bytes(),
        "semantic_execution.py": (
            Path(__file__).with_name("semantic_execution.py")
        ).read_bytes(),
        "execution_planner.py": (
            Path(__file__).with_name("segmentation") / "execution_planner.py"
        ).read_bytes(),
    }
    files.update({name: text.encode("utf-8-sig") for name, text in prompts.items()})
    for block in blocks:
        block_id = block["block_id"]
        owned = set(block["owned_token_ids"])
        block_material = {
            "task_id": task["task_id"], "block": block,
            "tokens": [row for row in records if row["token_id"] in owned],
        }
        files[f"blocks/{block_id}.json"] = json.dumps(
            block_material, ensure_ascii=False, indent=2
        ).encode("utf-8-sig")
        files[f"decisions/split/{block_id}.json"] = json.dumps({
            "task_id": task["task_id"], "block_id": block_id, "semantic_result": None,
        }, ensure_ascii=False, indent=2).encode("utf-8-sig")
    return files


def external_split_files(
    project_id: str,
    revision: Any,
    *,
    source_language: str,
    source_hard_limit: int = 55,
    glossary: Iterable[Mapping[str, Any]] = (),
) -> dict[str, bytes]:
    document = revision.document
    material = _external_split_material(document)
    source = normalize_source_language(source_language)
    if source == "Auto":
        source = source_language_for_text(
            layout_tokens(record["text"] for record in material["records"])
        )
    prompt_snapshot, execution_prompt = _prompt_bundle(
        orchestration_key="external_ai_split", source_language=source
    )
    task = {
        "schema_version": EXTERNAL_SPLIT_SCHEMA,
        "task": "external_segmentation",
        "project_id": project_id,
        "document_id": document.document_id,
        "revision_id": revision.revision_id,
        "document_hash": revision.document_hash,
        "source_language": source,
        "source_hard_limit": int(source_hard_limit),
        "token_sequence_sha256": material["fingerprint"],
        "current_boundary_after_token_ids": material["current_boundaries"],
        "tokens": material["records"],
        "prompt_snapshot": prompt_snapshot,
        "return_schema": {
            "schema_version": EXTERNAL_SPLIT_SCHEMA,
            "task": "external_segmentation",
            "revision_id": revision.revision_id,
            "document_hash": revision.document_hash,
            "token_sequence_sha256": material["fingerprint"],
            "boundaries": [
                {"after_token_id": "从 tokens 中选择；不得选择最后一个词元", "reason": "简短语义依据"}
            ],
        },
    }
    route_block = "\n".join((
        "# 本项目的活动切分参数",
        f"源语言：{source}",
        f"源文每 Cue 硬上限：{int(source_hard_limit)} 个显示字符",
        "只选择附件 tokens 中的边界 ID；不得改写、遗漏、重复或调序词元。",
    ))
    glossary_rows = [dict(row) for row in glossary if isinstance(row, Mapping)]
    if glossary_rows:
        route_block += "\n# 当前权威热词表\n" + json.dumps(
            glossary_rows, ensure_ascii=False, separators=(",", ":")
        )
    return {
        "01_外部AI切分任务.json": json.dumps(
            task, ensure_ascii=False, indent=2
        ).encode("utf-8-sig"),
        "02_执行提示词.md": f"{execution_prompt}\n\n{route_block}\n".encode("utf-8-sig"),
    }


def _external_split_plan(
    document: EditorDocument,
    material: Mapping[str, Any],
    boundary_ids: list[str],
) -> list[dict[str, Any]]:
    ordered_ids = list(material["ordered_ids"])
    positions = [ordered_ids.index(token_id) + 1 for token_id in boundary_ids]
    stops = [*positions, len(ordered_ids)]
    starts = [0, *positions]
    cues = list(material["cues"])
    overall_start = float(cues[0].start)
    overall_end = float(cues[-1].end)
    if overall_end - overall_start < len(stops) * 0.001:
        raise ProjectExchangeError("外部 AI 切分结果的总时长不足以容纳全部 Cue")

    times = [overall_start]
    for position in positions:
        left_end = float(material["bounds"][ordered_ids[position - 1]][1])
        right_start = float(material["bounds"][ordered_ids[position]][0])
        boundary = (left_end + right_start) / 2
        remaining = len(stops) - len(times)
        lower = times[-1] + 0.001
        upper = overall_end - remaining * 0.001
        if lower > upper:
            raise ProjectExchangeError("外部 AI 切分边界无法形成有效时间顺序")
        times.append(min(max(boundary, lower), upper))
    times.append(overall_end)

    token_map = material["token_map"]
    source_map = material["source_map"]
    cue_owner = {
        token_id: cue
        for cue in cues
        for token_id in cue.display_token_ids
    }
    plan: list[dict[str, Any]] = []
    for index, (start, stop) in enumerate(zip(starts, stops)):
        token_ids = ordered_ids[start:stop]
        visible_text = layout_tokens(
            token_map[token_id].text
            for token_id in token_ids
            if token_map[token_id].state is EntityState.ACTIVE
        )
        if not visible_text:
            raise ProjectExchangeError("外部 AI 切分产生了没有可见文字的 Cue")
        source_tokens = [
            source_map[source_id]
            for token_id in token_ids
            for source_id in token_map[token_id].source_token_ids
        ]
        speakers = {item.speaker for item in source_tokens if item.speaker}
        source_cues = list(dict.fromkeys(cue_owner[token_id].cue_id for token_id in token_ids))
        source_groups = list(dict.fromkeys(
            cue_owner[token_id].group_id
            for token_id in token_ids
            if cue_owner[token_id].group_id
        ))
        plan.append({
            "index": index,
            "token_ids": token_ids,
            "text": visible_text,
            "start": times[index],
            "end": times[index + 1],
            "speaker": next(iter(speakers)) if len(speakers) == 1 else None,
            "source_cue_ids": source_cues,
            "source_group_ids": source_groups,
        })
    return plan


def inspect_external_split(
    document: EditorDocument,
    payload: Mapping[str, Any],
    *,
    revision_id: str,
    document_hash: str,
    source_hard_limit: int,
) -> dict[str, Any]:
    if payload.get("schema_version") != EXTERNAL_SPLIT_SCHEMA:
        raise ProjectExchangeError("外部 AI 切分文件版本不受支持")
    if payload.get("task") != "external_segmentation":
        raise ProjectExchangeError("外部 AI 切分任务类型无效")
    if str(payload.get("revision_id", "")) != revision_id:
        raise ProjectExchangeError("外部 AI 切分结果绑定的项目版本已经变化")
    if str(payload.get("document_hash", "")) != document_hash:
        raise ProjectExchangeError("外部 AI 切分结果绑定的文档内容已经变化")

    material = _external_split_material(document)
    if str(payload.get("token_sequence_sha256", "")) != material["fingerprint"]:
        raise ProjectExchangeError("外部 AI 切分结果的词元序列指纹不匹配")
    rows = payload.get("boundaries")
    if not isinstance(rows, list):
        raise ProjectExchangeError("外部 AI 切分结果缺少 boundaries 数组")
    boundary_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProjectExchangeError("外部 AI 切分边界必须是对象")
        token_id = str(row.get("after_token_id", "")).strip()
        if not token_id:
            raise ProjectExchangeError("外部 AI 切分边界缺少 after_token_id")
        boundary_ids.append(token_id)
    if len(boundary_ids) != len(set(boundary_ids)):
        raise ProjectExchangeError("外部 AI 切分结果包含重复边界")
    valid_ids = set(material["visible_ids"][:-1])
    unknown = [token_id for token_id in boundary_ids if token_id not in valid_ids]
    if unknown:
        raise ProjectExchangeError(f"外部 AI 切分包含无效边界：{unknown}")
    positions = [material["ordered_ids"].index(token_id) for token_id in boundary_ids]
    if positions != sorted(positions):
        raise ProjectExchangeError("外部 AI 切分边界没有保持词元顺序")

    plan = _external_split_plan(document, material, boundary_ids)
    over_limit = [
        {"index": item["index"], "length": count_visible_characters(
            item["text"], count_spaces=True, count_punctuation=True
        )}
        for item in plan
        if count_visible_characters(
            item["text"], count_spaces=True, count_punctuation=True
        ) > int(source_hard_limit)
    ]
    if over_limit:
        raise ProjectExchangeError(f"外部 AI 切分结果超过源文硬上限：{over_limit}")
    current = list(material["current_boundaries"])
    return {
        "schema_version": "substar.external-ai-split-inspection.v1",
        "task": "external_segmentation",
        "boundary_ids": boundary_ids,
        "proposed_cues": plan,
        "summary": {
            "current_cues": len(material["cues"]),
            "proposed_cues": len(plan),
            "boundary_changes": len(set(current).symmetric_difference(boundary_ids)),
            "applicable": boundary_ids != current,
        },
    }


def apply_external_split(
    document: EditorDocument, inspection: Mapping[str, Any], *, allow_translations: bool = False
) -> EditorDocument:
    if inspection.get("task") != "external_segmentation":
        raise ProjectExchangeError("外部 AI 切分检查结果类型无效")
    material = _external_split_material(document, allow_translations=allow_translations)
    plan = _external_split_plan(
        document, material, [str(item) for item in inspection.get("boundary_ids", [])]
    )
    provenance = ChangeProvenance(
        kind=ChangeKind.IMPORT,
        operation="external_ai_split",
        actor="external-ai",
        metadata={"label": "外部 AI 切分", "cue_count": len(plan)},
    )
    groups: list[SemanticGroup] = []
    cues: list[DisplayCue] = []
    for item in plan:
        group_id = stable_id("grp", {
            "operation": "external_ai_split",
            "document_id": document.document_id,
            "index": item["index"],
            "token_ids": item["token_ids"],
        })
        groups.append(SemanticGroup(
            group_id=group_id,
            origin="manual",
            provenance=provenance,
            source_group_ids=tuple(item["source_group_ids"]),
            dirty_flags=("membership", "structure"),
        ))
        cues.append(DisplayCue(
            cue_id=stable_id("cue", {
                "operation": "external_ai_split",
                "document_id": document.document_id,
                "index": item["index"],
                "token_ids": item["token_ids"],
            }),
            index=item["index"],
            display_token_ids=tuple(item["token_ids"]),
            start=item["start"],
            end=item["end"],
            target=None,
            speaker=item["speaker"],
            state=EntityState.ACTIVE,
            group_id=group_id,
            mapping={
                "operation": "external_ai_split",
                "source_cue_ids": item["source_cue_ids"],
            },
        ))
    return replace(
        document,
        cues=tuple(cues),
        groups=tuple(groups),
        changes=(*document.changes, provenance),
    )


def write_bytes_zip(target: Path, files: Mapping[str, bytes]) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def export_subtitle_project(target: Path, *, project_id: str, job_dir: Path, revision: Any) -> None:
    document_bytes = json.dumps(revision.document.to_dict(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    entries: list[tuple[str, Path | None, bytes | None]] = [("document/latest.json", None, document_bytes)]
    candidates = [job_dir / "audio_16k_mono.wav"]
    input_dir = job_dir / "input"
    if input_dir.is_dir():
        candidates.extend(path for path in input_dir.iterdir() if path.is_file())
    for name in (
        "run_manifest.json", "task_info.json",
        "reference_alignment.json", "reference_script_alignment.json",
    ):
        candidates.append(job_dir / name)
    for folder in ("calibration", "review"):
        root = job_dir / folder
        if root.is_dir():
            candidates.extend(path for path in root.rglob("*") if path.is_file())
    seen: set[Path] = set()
    for path in candidates:
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        relative = path.relative_to(job_dir).as_posix()
        entries.append((f"project/{relative}", path, None))
    manifest_files = []
    for name, path, content in entries:
        size = path.stat().st_size if path else len(content or b"")
        digest = _sha256_file(path) if path else _sha256_bytes(content or b"")
        manifest_files.append({"path": name, "size": size, "sha256": digest})
    manifest = {
        "schema_version": EXCHANGE_SCHEMA,
        "source_project_id": project_id,
        "document_id": revision.document.document_id,
        "revision_id": revision.revision_id,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": manifest_files,
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        for name, path, content in entries:
            if path:
                archive.write(path, name)
            else:
                archive.writestr(name, content or b"")


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_FILES:
        raise ProjectExchangeError("字幕工程文件数量超出限制")
    expanded = 0
    for member in members:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
            raise ProjectExchangeError("字幕工程包含不安全路径")
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise ProjectExchangeError("字幕工程不能包含符号链接")
        expanded += member.file_size
        if member.file_size > 0 and member.compress_size == 0:
            raise ProjectExchangeError("字幕工程压缩信息无效")
        if member.compress_size and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
            raise ProjectExchangeError("字幕工程压缩比超出限制")
    if expanded > MAX_EXPANDED_BYTES:
        raise ProjectExchangeError("字幕工程解压后体积超出限制")
    return members


def import_subtitle_project(source: BinaryIO, *, projects_root: Path) -> str:
    projects_root.mkdir(parents=True, exist_ok=True)
    project_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_import_" + uuid.uuid4().hex[:6]
    temporary = Path(tempfile.mkdtemp(prefix=".import-", dir=projects_root))
    final = projects_root / project_id
    try:
        with zipfile.ZipFile(source) as archive:
            members = _safe_members(archive)
            by_name = {item.filename: item for item in members}
            if "manifest.json" not in by_name or "document/latest.json" not in by_name:
                raise ProjectExchangeError("不是有效的字幕工程")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("schema_version") != EXCHANGE_SCHEMA:
                raise ProjectExchangeError("字幕工程版本不受支持")
            declared = {str(item["path"]): item for item in manifest.get("files", [])}
            document_content: bytes | None = None
            for name, row in declared.items():
                member = by_name.get(name)
                if member is None or member.file_size != int(row.get("size", -1)):
                    raise ProjectExchangeError(f"字幕工程文件清单不匹配：{name}")
                output = None
                if name.startswith("project/"):
                    output = temporary / name.removeprefix("project/")
                    output.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                captured = bytearray() if name == "document/latest.json" else None
                with archive.open(member) as reader:
                    writer = output.open("wb") if output else None
                    try:
                        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                            digest.update(chunk)
                            if writer:
                                writer.write(chunk)
                            if captured is not None:
                                captured.extend(chunk)
                    finally:
                        if writer:
                            writer.close()
                if digest.hexdigest() != str(row.get("sha256", "")):
                    raise ProjectExchangeError(f"字幕工程文件校验失败：{name}")
                if captured is not None:
                    document_content = bytes(captured)
            if document_content is None:
                raise ProjectExchangeError("字幕工程缺少最新文档")
            document = EditorDocument.from_dict(json.loads(document_content))
        store = ProjectStore.create(temporary / "project", project_id=project_id)
        provenance = ChangeProvenance(
            kind=ChangeKind.IMPORT,
            # ProjectStore treats this operation as an explicit acceptance-state
            # write, so a completed portable project keeps that state on import.
            operation="set_complete_attribute" if document.complete else "subtitle_project_import",
            actor="subtitle-project",
            metadata={"exchange_operation": "subtitle_project_import"},
        )
        store.save(document, provenance=provenance)
        input_dir = temporary / "input"
        media = next((path for path in input_dir.iterdir() if path.is_file()), None) if input_dir.is_dir() else None
        filename = media.name if media else f"{project_id}.srt"
        atomic_write_json(temporary / "creation_state.json", {
            "id": project_id,
            "filename": filename,
            "display_name": Path(filename).stem,
            "workflow_mode": "subtitle_creation",
            "status": "awaiting_edit",
            "message": "字幕工程已导入",
            "progress": 1.0,
            "error": "",
            "files": [],
            "created_at": datetime.now().timestamp(),
            "attempt": 1,
            "settings_overrides": {},
        })
        task_info_path = temporary / "task_info.json"
        if task_info_path.is_file():
            try:
                task_info = json.loads(task_info_path.read_text(encoding="utf-8"))
                save_task_info(temporary, project_id, task_info)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ProjectExchangeError("字幕工程任务信息无效") from exc
        temporary.rename(final)
        return project_id
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def inspect_external_prooftranslation(
    document: EditorDocument, payload: Mapping[str, Any]
) -> dict[str, Any]:
    if payload.get("schema_version") != EXTERNAL_PROOFTRANSLATION_SCHEMA:
        raise ProjectExchangeError("外部 AI 校译文件版本不受支持")
    task = str(payload.get("task", ""))
    if task != "staged_correction_translation":
        raise ProjectExchangeError("外部 AI 校译任务类型无效")
    source_language = str(payload.get("source_language", "")).strip()
    target_language = str(payload.get("target_language", "")).strip()
    if not source_language or target_language not in {"zh-CN", "en", "ja", "ko"}:
        raise ProjectExchangeError("外部 AI 校译语言路由无效")
    cues = {cue.cue_id: cue for cue in document.cues}
    tokens = {token.token_id: token for token in document.display_tokens}

    def inspect_rows(raw_rows: Any, *, source_track: bool) -> dict[str, list[dict[str, Any]]]:
        valid: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        seen: set[str] = set()
        if not isinstance(raw_rows, list):
            return {"applicable": [], "content_changed": [], "invalid": [{"reason": "结果列表无效"}]}
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                invalid.append({"reason": "结果项不是对象"})
                continue
            row = dict(raw)
            cue_id = str(raw.get("cue_id", ""))
            cue = cues.get(cue_id)
            if cue_id in seen:
                invalid.append({"item": row, "reason": "Cue 结果重复"})
            elif cue is None or cue.state is EntityState.DELETED:
                invalid.append({"item": row, "reason": "Cue 不存在"})
            elif str(raw.get("source_hash", "")) != _cue_hash(document, cue):
                changed.append({"item": row, "reason": "内容已变化"})
            elif source_track:
                raw_tokens = raw.get("tokens")
                expected_ids = [
                    token_id for token_id in cue.display_token_ids
                    if tokens[token_id].state is EntityState.ACTIVE
                ]
                actual_ids = [
                    str(item.get("token_id", "")) for item in raw_tokens
                ] if isinstance(raw_tokens, list) and all(isinstance(item, Mapping) for item in raw_tokens) else []
                texts_valid = bool(raw_tokens) and all(
                    str(item.get("text", "")).strip() for item in raw_tokens
                ) if isinstance(raw_tokens, list) else False
                if actual_ids != expected_ids or not texts_valid:
                    invalid.append({"item": row, "reason": "token 结构、归属或顺序无效"})
                else:
                    valid.append(row)
            elif str(raw.get("language", "")) != target_language:
                invalid.append({"item": row, "reason": "译文语言路由不匹配"})
            elif not str(raw.get("target_text", "")).strip():
                invalid.append({"item": row, "reason": "译文为空"})
            else:
                valid.append(row)
            seen.add(cue_id)
        return {"applicable": valid, "content_changed": changed, "invalid": invalid}

    return {
        "task": task,
        "source_language": source_language,
        "target_language": target_language,
        "source": inspect_rows(payload.get("source_results"), source_track=True),
        "translation": inspect_rows(payload.get("translation_results"), source_track=False),
    }


def apply_external_prooftranslation(
    document: EditorDocument, inspection: Mapping[str, Any]
) -> EditorDocument:
    result = document
    provenance = {"kind": "import", "operation": "external_ai_prooftranslation", "actor": "external-ai", "metadata": {"label": "外部 AI 校译"}}
    if inspection.get("task") != "staged_correction_translation":
        raise ProjectExchangeError("外部 AI 校译任务类型无效")
    replacements = []
    token_map = {token.token_id: token for token in document.display_tokens}
    for row in inspection["source"]["applicable"]:
        for item in row.get("tokens", []):
            token_id = str(item.get("token_id", ""))
            text = str(item.get("text", "")).strip()
            if token_id in token_map and text and text != token_map[token_id].text:
                replacements.append({"token_id": token_id, "text": text, "expected_text": token_map[token_id].text})
    if replacements:
        result = apply_document_operation(result, {"operation_id": f"external_{uuid.uuid4().hex}", "type": "batch_replace", "payload": {"replacements": replacements, "provenance": provenance}})
    for row in inspection["translation"]["applicable"]:
        result = apply_document_operation(result, {"operation_id": f"external_{uuid.uuid4().hex}", "type": "set_target", "payload": {"cue_id": row["cue_id"], "target_text": row["target_text"], "language": inspection["target_language"], "provenance": provenance}})
    return result


def _checkpoint_hash(payload: Mapping[str, Any]) -> str:
    unhashed = dict(payload)
    unhashed.pop("checkpoint_sha256", None)
    return _sha256_bytes(json.dumps(
        unhashed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))


def _checkpoint_text_fragments(text: str, source_language: str) -> list[str]:
    if normalize_language(source_language) in {"en", "mixed"}:
        return [part for part in text.split(" ") if part]
    return editor_token_fragments(text)


def _checkpoint_calibration_actions(
    raw: Mapping[str, Any],
    *,
    cue_number: int,
    token_ids: list[str],
    token_texts: list[str],
    source_language: str,
) -> tuple[list[str], int]:
    actions = raw.get("calibration_operations")
    if not isinstance(actions, list):
        raise ProjectExchangeError(
            f"外部 AI 生成 Cue {cue_number} 缺少增量校正操作"
        )
    positions = {token_id: index for index, token_id in enumerate(token_ids)}
    result = list(token_texts)
    changed_ids: set[str] = set()
    action_ids: set[str] = set()
    applied_count = 0
    for action_number, action in enumerate(actions, start=1):
        if not isinstance(action, Mapping):
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的校正操作 {action_number} 无效"
            )
        action_id = str(action.get("action_id", ""))
        kind = str(action.get("kind", ""))
        disposition = str(action.get("disposition", ""))
        ids = action.get("token_ids")
        if not action_id or action_id in action_ids:
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的校正操作 ID 无效"
            )
        action_ids.add(action_id)
        if kind not in {"set_case", "set_punctuation", "replace_token", "replace_span"}:
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的校正操作类型无效"
            )
        if disposition not in {"apply", "review"}:
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的校正操作处置无效"
            )
        if not isinstance(ids, list) or not ids or not all(
            isinstance(token_id, str) and token_id in positions for token_id in ids
        ):
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的校正操作词元无效"
            )
        indexes = [positions[token_id] for token_id in ids]
        if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的校正操作词元不连续"
            )
        before = " ".join(token_texts[index] for index in indexes)
        after = str(action.get("after_text", "")).strip()
        if action.get("before_text") != before or not after:
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的校正操作原文绑定无效"
            )
        fragments = _checkpoint_text_fragments(after, source_language)
        if len(fragments) != len(ids):
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的校正操作改变了词元数量"
            )
        if kind != "replace_span" and len(ids) != 1:
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 的单词校正操作绑定了多个词元"
            )
        if disposition == "review":
            continue
        if changed_ids.intersection(ids):
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {cue_number} 包含重叠的增量校正操作"
            )
        changed_ids.update(ids)
        for index, fragment in zip(indexes, fragments):
            result[index] = fragment
        applied_count += 1
    return result, applied_count


def inspect_external_generation_checkpoint(
    document: EditorDocument,
    payload: Mapping[str, Any],
    *,
    project_id: str,
    revision_id: str,
    document_hash: str,
    source_hard_limit: int,
    target_hard_limit: int,
) -> dict[str, Any]:
    checkpoint_schema = payload.get("schema_version")
    if checkpoint_schema not in {
        EXTERNAL_GENERATION_CHECKPOINT_SCHEMA_V2,
        EXTERNAL_GENERATION_CHECKPOINT_SCHEMA,
        EXTERNAL_GENERATION_CHECKPOINT_SCHEMA_V1,
    }:
        raise ProjectExchangeError("外部 AI 生成文件版本不受支持")
    if str(payload.get("project_id", "")) != project_id:
        raise ProjectExchangeError("外部 AI 生成结果属于另一个项目")
    if str(payload.get("source_revision_id", "")) != revision_id:
        raise ProjectExchangeError("外部 AI 生成所依据的项目版本已经变化")
    if str(payload.get("source_document_hash", "")) != document_hash:
        raise ProjectExchangeError("外部 AI 生成所依据的文档内容已经变化")
    if str(payload.get("checkpoint_sha256", "")) != _checkpoint_hash(payload):
        raise ProjectExchangeError("外部 AI 生成 checkpoint 哈希无效")

    stage = str(payload.get("checkpoint", ""))
    expected_stages = {
        "split": ["split"],
        "corrected": ["split", "calibration"],
        "translated": ["split", "calibration", "translation"],
    }
    if stage not in expected_stages or payload.get("completed_stages") != expected_stages[stage]:
        raise ProjectExchangeError("外部 AI 生成阶段或完成状态无效")
    parent = payload.get("parent_checkpoint_sha256")
    if stage == "split":
        if parent is not None:
            raise ProjectExchangeError("切分 checkpoint 不应包含父 checkpoint")
    elif not isinstance(parent, str) or len(parent) != 64:
        raise ProjectExchangeError("外部 AI 生成父 checkpoint 哈希无效")

    languages = payload.get("languages")
    if not isinstance(languages, Mapping):
        raise ProjectExchangeError("外部 AI 生成语言路由无效")
    source_language = str(languages.get("source", ""))
    target_language = str(languages.get("target", ""))
    if normalize_language(source_language) not in {"en", "zh", "ja", "ko", "mixed"}:
        raise ProjectExchangeError("外部 AI 生成源语言无效")
    if normalize_language(target_language) not in {"en", "zh", "ja", "ko"}:
        raise ProjectExchangeError("外部 AI 生成目标语言无效")
    if normalize_language(source_language) == normalize_language(target_language):
        raise ProjectExchangeError("外部 AI 生成源语言与目标语言不能相同")
    limits = payload.get("hard_limits")
    if not isinstance(limits, Mapping) or (
        limits.get("source") != int(source_hard_limit)
        or limits.get("target") != int(target_hard_limit)
    ):
        raise ProjectExchangeError("外部 AI 生成字符限制与当前项目不一致")

    records = _external_generation_source_records(document)
    fingerprint = _sha256_bytes(json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    if str(payload.get("source_fingerprint", "")) != fingerprint:
        raise ProjectExchangeError("外部 AI 生成源词元指纹不匹配")
    record_map = {record["token_id"]: record for record in records}
    expected_ids = [record["token_id"] for record in records]
    raw_cues = payload.get("cues")
    if not isinstance(raw_cues, list) or not raw_cues:
        raise ProjectExchangeError("外部 AI 生成结果缺少 cues 数组")

    generated_ids: set[str] = set()
    covered_ids: list[str] = []
    normalized_cues: list[dict[str, Any]] = []
    replacement_count = 0
    translation_count = 0
    source_counts_spaces = normalize_language(source_language) in {"en", "mixed"}
    target_counts_spaces = normalize_language(target_language) in {"en", "mixed"}
    for position, raw in enumerate(raw_cues):
        if not isinstance(raw, Mapping):
            raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 不是对象")
        cue_id = str(raw.get("cue_id", ""))
        if not cue_id or cue_id in generated_ids or raw.get("index") != position:
            raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 的 ID 或索引无效")
        generated_ids.add(cue_id)
        token_ids = raw.get("token_ids")
        if not isinstance(token_ids, list) or not token_ids or not all(
            isinstance(token_id, str) and token_id in record_map for token_id in token_ids
        ):
            raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 的词元无效")
        covered_ids.extend(token_ids)
        cue_records = [record_map[token_id] for token_id in token_ids]
        source_text = " ".join(record["text"] for record in cue_records)
        if raw.get("source_text") != source_text:
            raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 改写了原始词元")
        start = float(cue_records[0]["start"])
        end = float(cue_records[-1]["end"])
        try:
            returned_start = float(raw.get("start"))
            returned_end = float(raw.get("end"))
        except (TypeError, ValueError) as exc:
            raise ProjectExchangeError(
                f"外部 AI 生成 Cue {position + 1} 的时间码无效"
            ) from exc
        if abs(returned_start - start) > 0.001 or abs(returned_end - end) > 0.001:
            raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 的时间码无效")
        if raw.get("speaker") != cue_records[0]["speaker"]:
            raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 的说话人无效")
        source_count = count_visible_characters(
            source_text, count_spaces=source_counts_spaces, count_punctuation=True
        )
        if source_count > int(source_hard_limit) or raw.get("source_character_count") != source_count:
            raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 的源文字符计数无效")

        calibrated_text = raw.get("calibrated_text")
        token_texts = [record["text"] for record in cue_records]
        if stage == "split":
            if (
                calibrated_text is not None
                or raw.get("target_text") is not None
                or raw.get("calibration_operations") is not None
            ):
                raise ProjectExchangeError("切分 checkpoint 不得包含校正稿或译文")
        else:
            calibrated_text = str(calibrated_text or "").strip()
            if checkpoint_schema in {
                EXTERNAL_GENERATION_CHECKPOINT_SCHEMA,
                EXTERNAL_GENERATION_CHECKPOINT_SCHEMA_V1,
            }:
                fragments, applied_actions = _checkpoint_calibration_actions(
                    raw,
                    cue_number=position + 1,
                    token_ids=token_ids,
                    token_texts=token_texts,
                    source_language=source_language,
                )
                if " ".join(fragments) != calibrated_text:
                    raise ProjectExchangeError(
                        f"外部 AI 生成 Cue {position + 1} 的增量操作不能复现校正稿"
                    )
                replacement_count += applied_actions
            else:
                fragments = _checkpoint_text_fragments(calibrated_text, source_language)
                if len(fragments) != len(token_ids):
                    raise ProjectExchangeError(
                        f"外部 AI 生成 Cue {position + 1} 的校正稿不能安全映射回原词元"
                    )
                replacement_count += sum(
                    before != after for before, after in zip(token_texts, fragments)
                )
            calibrated_count = count_visible_characters(
                calibrated_text, count_spaces=source_counts_spaces, count_punctuation=True
            )
            if calibrated_count > int(source_hard_limit) or raw.get(
                "calibrated_character_count"
            ) != calibrated_count:
                raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 的校正字符计数无效")
            token_texts = fragments
        target_text = raw.get("target_text")
        if stage == "translated":
            target_text = str(target_text or "").strip()
            if not target_text:
                raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 的译文为空")
            target_count = count_visible_characters(
                target_text, count_spaces=target_counts_spaces, count_punctuation=True
            )
            if target_count > int(target_hard_limit) or raw.get(
                "target_character_count"
            ) != target_count:
                raise ProjectExchangeError(f"外部 AI 生成 Cue {position + 1} 的译文字符计数无效")
            translation_count += 1
        elif target_text is not None:
            raise ProjectExchangeError("未完成翻译的 checkpoint 不得包含译文")
        normalized_cues.append({
            "generated_cue_id": cue_id,
            "token_ids": token_ids,
            "token_texts": token_texts,
            "target_text": target_text,
        })
    if covered_ids != expected_ids:
        raise ProjectExchangeError("外部 AI 生成 Cue 没有按顺序完整且唯一覆盖源词元")

    semantic_groups: list[dict[str, Any]] = []
    presentation_plans: list[dict[str, Any]] = []
    if checkpoint_schema == EXTERNAL_GENERATION_CHECKPOINT_SCHEMA_V1:
        raw_groups = payload.get("semantic_groups")
        if not isinstance(raw_groups, list) or not raw_groups:
            raise ProjectExchangeError("外部 AI 生成结果缺少 semantic_groups")
        cue_rows = {row["generated_cue_id"]: row for row in normalized_cues}
        expected_cue_ids = list(cue_rows)
        covered_cue_ids: list[str] = []
        group_ids: set[str] = set()
        for group_number, raw_group in enumerate(raw_groups, start=1):
            if not isinstance(raw_group, Mapping):
                raise ProjectExchangeError(f"外部 AI 语义组 {group_number} 不是对象")
            group_id = str(raw_group.get("group_id", "")).strip()
            cue_ids = raw_group.get("cue_ids")
            if (
                not group_id
                or group_id in group_ids
                or not isinstance(cue_ids, list)
                or not cue_ids
                or not all(isinstance(cue_id, str) and cue_id in cue_rows for cue_id in cue_ids)
            ):
                raise ProjectExchangeError(f"外部 AI 语义组 {group_number} 的 ID 或 Cue 归属无效")
            group_ids.add(group_id)
            covered_cue_ids.extend(cue_ids)
            semantic_groups.append({"group_id": group_id, "cue_ids": list(cue_ids)})
        if covered_cue_ids != expected_cue_ids:
            raise ProjectExchangeError("外部 AI 语义组没有按顺序完整且唯一覆盖全部 Cue")

        raw_translation = payload.get("translation_plan")
        if stage == "translated":
            if not isinstance(raw_translation, Mapping) or not isinstance(
                raw_translation.get("group_results"), list
            ):
                raise ProjectExchangeError("外部 AI 翻译 checkpoint 缺少 translation_plan")
            translation_rows = {
                str(row.get("group_id", "")): row
                for row in raw_translation["group_results"]
                if isinstance(row, Mapping)
            }
            assignment_by_cue: dict[str, tuple[str, list[str]]] = {}
            raw_cue_by_id = {
                str(row.get("cue_id", "")): row
                for row in raw_cues if isinstance(row, Mapping)
            }
            for group in semantic_groups:
                validation_group = {
                    "group_id": group["group_id"],
                    "cues": [
                        {
                            "cue_id": cue_id,
                            "start": float(raw_cue_by_id[cue_id]["start"]),
                            "end": float(raw_cue_by_id[cue_id]["end"]),
                            "hard_limit": int(target_hard_limit),
                            "count_rule": (
                                "characters_including_spaces"
                                if target_counts_spaces
                                else "characters_excluding_spaces"
                            ),
                            "maximum_cps": 10_000,
                        }
                        for cue_id in group["cue_ids"]
                    ],
                }
                plan = validate_presentation_plan(
                    validation_group, translation_rows.get(group["group_id"])
                )
                if plan is None:
                    raise ProjectExchangeError(
                        f"外部 AI 翻译语义组 {group['group_id']} 的多对多呈现计划无效"
                    )
                presentation_plans.append(plan)
                units = {unit["meaning_unit_id"]: unit for unit in plan["meaning_units"]}
                for assignment in plan["cue_assignments"]:
                    unit = units[assignment["meaning_unit_id"]]
                    assignment_by_cue[assignment["cue_id"]] = (
                        unit["target_text"], list(unit["source_evidence_cue_ids"])
                    )
            if set(assignment_by_cue) != set(expected_cue_ids):
                raise ProjectExchangeError("外部 AI 翻译计划没有覆盖全部 Cue")
            for index, cue_id in enumerate(expected_cue_ids):
                expected_target, evidence = assignment_by_cue[cue_id]
                if normalized_cues[index]["target_text"] != expected_target:
                    raise ProjectExchangeError(
                        f"外部 AI Cue {index + 1} 的译文与多对多呈现计划不一致"
                    )
                normalized_cues[index]["source_evidence_cue_ids"] = evidence
        elif raw_translation is not None:
            raise ProjectExchangeError("未完成翻译的 checkpoint 不得包含 translation_plan")

    boundary_ids = [cue["token_ids"][-1] for cue in normalized_cues[:-1]]
    material = _external_split_material(document, allow_translations=True)
    plan = _external_split_plan(document, material, boundary_ids)
    current_boundaries = list(material["current_boundaries"])
    topology_changed = boundary_ids != current_boundaries
    targets_changed = stage == "translated" and translation_count > 0
    return {
        "schema_version": (
            "substar.external-ai-generation-inspection.v1"
            if checkpoint_schema == EXTERNAL_GENERATION_CHECKPOINT_SCHEMA_V1
            else (
                "substar.external-ai-generation-inspection.v3"
                if checkpoint_schema == EXTERNAL_GENERATION_CHECKPOINT_SCHEMA
                else "substar.external-ai-generation-inspection.v2"
            )
        ),
        "task": "external_generation_checkpoint",
        "checkpoint": stage,
        "source_language": source_language,
        "target_language": target_language,
        "boundary_ids": boundary_ids,
        "cues": normalized_cues,
        "semantic_groups": semantic_groups,
        "presentation_plans": presentation_plans,
        "summary": {
            "current_cues": len(material["cues"]),
            "proposed_cues": len(normalized_cues),
            "topology_changed": topology_changed,
            "source_replacements": replacement_count,
            "translations": translation_count,
            "applicable": topology_changed or replacement_count > 0 or targets_changed,
        },
    }


def apply_external_generation_checkpoint(
    document: EditorDocument, inspection: Mapping[str, Any]
) -> EditorDocument:
    if inspection.get("task") != "external_generation_checkpoint":
        raise ProjectExchangeError("外部 AI 生成检查结果类型无效")
    result = apply_external_split(document, {
        "task": "external_segmentation",
        "boundary_ids": list(inspection.get("boundary_ids", [])),
    }, allow_translations=True)
    cue_by_tokens = {
        tuple(cue.display_token_ids): cue for cue in result.cues
        if cue.state is EntityState.ACTIVE
    }
    generated_to_applied = {
        str(row["generated_cue_id"]): cue_by_tokens[tuple(row["token_ids"])]
        for row in inspection["cues"]
        if tuple(row["token_ids"]) in cue_by_tokens
    }
    if inspection.get("semantic_groups"):
        split_group_by_id = {group.group_id: group for group in result.groups}
        source_group_by_id = {group.group_id: group for group in document.groups}
        imported_groups: list[SemanticGroup] = []
        group_for_cue: dict[str, str] = {}
        for group_index, external_group in enumerate(inspection["semantic_groups"]):
            external_group_id = str(external_group["group_id"])
            applied_cues = [
                generated_to_applied[str(cue_id)]
                for cue_id in external_group["cue_ids"]
            ]
            source_group_ids = list(dict.fromkeys(
                source_group_id
                for cue in applied_cues
                for source_group_id in split_group_by_id[cue.group_id].source_group_ids
            ))
            execution_block_ids = list(dict.fromkeys(
                block_id
                for source_group_id in source_group_ids
                for block_id in source_group_by_id.get(
                    source_group_id,
                    split_group_by_id[applied_cues[0].group_id],
                ).execution_block_ids
            ))
            applied_group_id = stable_id("grp", {
                "operation": "external_ai_generation",
                "document_id": document.document_id,
                "external_group_id": external_group_id,
                "index": group_index,
                "cue_ids": list(external_group["cue_ids"]),
            })
            imported_groups.append(SemanticGroup(
                group_id=applied_group_id,
                origin="manual",
                provenance=split_group_by_id[applied_cues[0].group_id].provenance,
                source_group_ids=tuple(source_group_ids),
                execution_block_ids=tuple(execution_block_ids),
                dirty_flags=("membership", "structure"),
            ))
            for cue in applied_cues:
                group_for_cue[cue.cue_id] = applied_group_id
        if set(group_for_cue) != {cue.cue_id for cue in result.cues}:
            raise ProjectExchangeError("外部 AI 语义组无法绑定应用后的全部 Cue")
        result = replace(
            result,
            groups=tuple(imported_groups),
            cues=tuple(replace(
                cue,
                group_id=group_for_cue[cue.cue_id],
                mapping={
                    **dict(cue.mapping),
                    "external_generated_cue_id": next(
                        key for key, value in generated_to_applied.items()
                        if value.cue_id == cue.cue_id
                    ),
                },
            ) for cue in result.cues),
        )
    original_tokens = {token.token_id: token for token in document.display_tokens}
    provenance = {
        "kind": "import",
        "operation": "external_ai_generation",
        "actor": "external-ai",
        "metadata": {"label": "外部 AI 生成", "checkpoint": inspection["checkpoint"]},
    }
    replacements = []
    for row in inspection["cues"]:
        for token_id, text in zip(row["token_ids"], row["token_texts"]):
            before = original_tokens[token_id].text
            if text != before:
                replacements.append({
                    "token_id": token_id,
                    "text": text,
                    "expected_text": before,
                })
    if replacements:
        result = apply_document_operation(result, {
            "operation_id": f"external_generation_{uuid.uuid4().hex}",
            "type": "batch_replace",
            "payload": {"replacements": replacements, "provenance": provenance},
        })
    if inspection["checkpoint"] == "translated":
        for row in inspection["cues"]:
            cue = cue_by_tokens.get(tuple(row["token_ids"]))
            if cue is None:
                raise ProjectExchangeError("外部 AI 生成 Cue 无法绑定应用后的拓扑")
            result = apply_document_operation(result, {
                "operation_id": f"external_generation_{uuid.uuid4().hex}",
                "type": "set_target",
                "payload": {
                    "cue_id": cue.cue_id,
                    "target_text": row["target_text"],
                    "language": inspection["target_language"],
                    "provenance": provenance,
                },
            })
        if inspection.get("presentation_plans"):
            evidence_by_generated_cue: dict[str, dict[str, Any]] = {}
            for plan in inspection["presentation_plans"]:
                units = {unit["meaning_unit_id"]: unit for unit in plan["meaning_units"]}
                for assignment in plan["cue_assignments"]:
                    unit = units[assignment["meaning_unit_id"]]
                    evidence_by_generated_cue[assignment["cue_id"]] = {
                        "meaning_unit_id": assignment["meaning_unit_id"],
                        "source_evidence_cue_ids": [
                            generated_to_applied[cue_id].cue_id
                            for cue_id in unit["source_evidence_cue_ids"]
                        ],
                    }
            applied_to_generated = {
                cue.cue_id: generated_id
                for generated_id, cue in generated_to_applied.items()
            }
            result = replace(result, cues=tuple(
                replace(cue, mapping={
                    **dict(cue.mapping),
                    "group_mapping_type": "model-authored-meaning-units",
                    **evidence_by_generated_cue[applied_to_generated[cue.cue_id]],
                })
                for cue in result.cues
            ))
    return result
