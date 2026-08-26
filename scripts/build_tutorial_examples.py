from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from substar_core.domain import ChangeKind, ChangeProvenance
from substar_core.domain.editor_document import TranslationTrack
from substar_core.storage import ProjectStore


SOURCE_PROJECT = ROOT / "data" / "projects" / "20260824_215951_split_a104e4"
EXAMPLES = ROOT / "assets" / "examples" / "tutorials"


TARGETS = (
    "中国现在最大的新闻是茉莉奶白被路易威登起诉了",
    "中国现在最大的新闻是茉莉奶白被路易威登起诉了",
    "因使用其标志性的四叶草设计而侵犯了该品牌商标",
    "因使用其标志性的四叶草设计而侵犯了该品牌商标",
    "苏州法院刚判路易威登胜诉",
    "因此茉莉奶白刚被责令向路易威登付款",
    "金额为1030万元人民币，约合150万美元",
    "并须在10天内付清",
    "不过，茉莉奶白将对该判决提出上诉",
    "但中国网友并不买账",
    "多数人站在茉莉奶白一边",
    "尽管两个标志颇为相似",
    "许多中国网友表示",
    "这种图案在中国很早就出现了",
    "而且已有上千年历史",
    "至少可追溯到唐代",
    "在中国，这种图案被称为海棠纹",
    "也叫海棠花图案",
    "你常能看到这种图案用于中国建筑中",
    "尤其常见于中国建筑的窗棂",
    "以及门檐上",
    "不过这种图案其实很有代表性",
    "对茉莉奶白来说尤其如此，不仅在于其悠久历史",
    "还因为 Molly 源自中文",
    "“茉莉”这个词",
    "茉莉直译成英文就是 Jasmine",
    "因此有了花卉联想",
    "如果上诉失败",
    "茉莉奶白就需要更换标志",
    "许多中国网友已经开始提供",
    "新的标志方案",
    "中国网友还表示，路易威登",
    "不够了解中国市场",
    "因为连迪奥都还没起诉霸王茶姬",
)


MULTI_GROUPS = ((0, 1), (2, 3), (23, 24), (29, 30), (31, 32))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def translated_document(document):
    if len(document.cues) != len(TARGETS):
        raise RuntimeError(f"advanced tutorial expected {len(TARGETS)} cues, got {len(document.cues)}")
    group_for_position = {
        position: positions for positions in MULTI_GROUPS for position in positions
    }
    provenance = ChangeProvenance(
        kind=ChangeKind.AI,
        operation="tutorial_snapshot_translation",
        actor="packaged-example",
        metadata={"case_id": "advanced-ai-v1", "source": "accepted-model-result"},
    )
    cues = []
    for position, (cue, target_text) in enumerate(zip(document.cues, TARGETS)):
        group_positions = group_for_position.get(position, (position,))
        source_ids = [document.cues[item].cue_id for item in group_positions]
        mapping = {
            **dict(cue.mapping),
            "mapping_type": "N:M" if len(group_positions) > 1 else "1:1",
            "group_mapping_type": "model-authored-meaning-units",
            "source_cue_ids": source_ids,
            "source_evidence_cue_ids": source_ids,
            "meaning_unit_id": f"tutorial_mu_{min(group_positions) + 1:04d}",
        }
        cues.append(replace(
            cue,
            target=TranslationTrack(
                target_text=target_text,
                original_text=target_text,
                language="zh-CN",
                provenance=provenance,
            ),
            mapping=mapping,
        ))
    return replace(document, cues=tuple(cues))


def review_snapshot(document) -> dict[str, object]:
    by_id = {token.token_id: token for token in document.display_tokens}

    def token_ids(cue, words: tuple[str, ...]) -> list[str]:
        wanted = {word.casefold() for word in words}
        return [
            token_id for token_id in cue.display_token_ids
            if by_id[token_id].text.strip(".,?!\"'“”").casefold() in wanted
        ]

    specifications = (
        (10, ("Molly", "Tea"), "entity_consistency", "检查品牌名 Molly Tea 在全文中的拼写是否一致。", "Molly Tea"),
        (16, ("Haitang", "Wen"), "suspected_misrecognition", "Haitang Wen 是音译专名，建议结合音频与热词表确认。", None),
        (24, ("��",), "mixed_script", "此处出现无法辨识的中文字符，应结合音频确认原词 Moli。", "Moli"),
        (33, ("Chaji",), "entity_consistency", "Chaji 是品牌专名，建议确认是否采用正式品牌写法。", "Chaji"),
    )
    issues = []
    for number, words, issue_type, description, suggestion in specifications:
        cue = document.cues[number]
        issue = {
            "issue_id": f"tutorial_review_{number + 1:04d}",
            "track": "source",
            "issue_type": issue_type,
            "cue_ids": [cue.cue_id],
            "token_ids": token_ids(cue, words),
            "impact": "moderate",
            "confidence": "medium",
            "description": description,
            "evidence": "进阶教程预存的审阅结果；用于展示问题定位、跳转和处理状态。",
            "suggested_text": suggestion,
            "recommended_action": "verify_entity" if suggestion is None else "replace_source",
            "status": "open",
        }
        issues.append(issue)
    return {
        "schema_version": "substar.tutorial-review-snapshot.v1",
        "review_id": "tutorial_review_advanced_ai_v1",
        "based_on_stage": "translation",
        "source_issues": issues,
        "translation_issues": [],
        "issues": issues,
        "failed_blocks": [],
        "rejected_issue_count": 0,
        "failed_block_errors": {},
    }


def main() -> None:
    beginner = EXAMPLES / "beginner"
    beginner.mkdir(parents=True, exist_ok=True)
    if not (beginner / "media.mp3").is_file() or not (beginner / "reference.txt").is_file():
        raise RuntimeError("beginner tutorial assets must already exist in assets/examples/tutorials/beginner")
    write_json(beginner / "manifest.json", {
        "schema_version": "substar.tutorial-example.v1",
        "case_id": "reference-script-v1",
        "level": "beginner",
        "display_name": "初级教程",
        "assets": {"media": "media.mp3", "reference": "reference.txt"},
    })

    advanced = EXAMPLES / "advanced-ai"
    advanced.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE_PROJECT / "audio_16k_mono.wav", advanced / "media.wav")
    store = ProjectStore.open(SOURCE_PROJECT / "project")
    segmented = store.load_revision("rev_5277581ebf22869c97beba00").document
    calibrated = store.load_revision("rev_ae3aed4cc063f5267d21d098").document
    translated = translated_document(calibrated)
    write_json(advanced / "segmented.json", segmented.to_dict())
    write_json(advanced / "calibrated.json", calibrated.to_dict())
    write_json(advanced / "translated.json", translated.to_dict())
    write_json(advanced / "review.json", review_snapshot(translated))
    files = ("media.wav", "segmented.json", "calibrated.json", "translated.json", "review.json")
    write_json(advanced / "manifest.json", {
        "schema_version": "substar.tutorial-example.v1",
        "case_id": "advanced-ai-v1",
        "level": "advanced",
        "display_name": "进阶教程",
        "source_language": "en",
        "target_language": "zh-CN",
        "source_hard_limit": 55,
        "target_hard_limit": 25,
        "split_workflow": "one_step",
        "assets": {
            "media": "media.wav",
            "segmentation": "segmented.json",
            "calibration": "calibrated.json",
            "translation": "translated.json",
            "review": "review.json",
        },
        "sha256": {name: digest(advanced / name) for name in files},
    })


if __name__ == "__main__":
    main()
