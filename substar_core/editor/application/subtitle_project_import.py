"""Explicit, lossless import preview for token-free subtitle projects.

Unlike srt_import (translation replacement), this module never infers a track
from language or manufactures word timing. Preview output retains every input
entry and each track's own timing, including unmatched bilingual entries.
"""
from .srt_import import parse_srt


def build_subtitle_document(preview, document_id):
    from substar_core.domain import EditorDocument, DisplayCue, TranslationTrack, ChangeKind, ChangeProvenance, stable_id
    if not preview["can_import"]:
        raise ValueError("请先解决字幕预览中的问题")
    provenance = ChangeProvenance(ChangeKind.MANUAL, "subtitle_import")
    targets = {row["import_id"]: row for row in preview["tracks"]["target"]}
    pairs = {pair["source"]: pair["target"] for pair in preview["pairs"]}
    consumed = set()
    rows = []
    for source in preview["tracks"]["source"]:
        target_id = pairs.get(source["import_id"])
        target = targets.get(target_id)
        if target_id:
            consumed.add(target_id)
        rows.append((source, source["text"], target["text"] if target else None))
    for target_id, target in targets.items():
        if target_id not in consumed:
            rows.append((target, "", target["text"]))
    rows.sort(key=lambda row: (row[0]["start_ms"], row[0]["end_ms"]))
    cues = tuple(DisplayCue(
        cue_id=stable_id("cue", {"document": document_id, "import": row["import_id"]}),
        index=index, display_token_ids=(), start=row["start_ms"] / 1000, end=row["end_ms"] / 1000,
        source_text=source, target=TranslationTrack(target, provenance) if target else None,
    ) for index, (row, source, target) in enumerate(rows))
    return EditorDocument(document_id, (), (), cues)


def preview_subtitle_project(primary, *, mode="single", secondary=None,
                             separator=None, first_track="source"):
    if mode not in {"single", "two-files", "bilingual-lines", "bilingual-inline"}:
        raise ValueError("不支持的字幕导入方式")
    if first_track not in {"source", "target"}:
        raise ValueError("请指定第一轨为原文或译文")
    if mode == "two-files" and not secondary:
        raise ValueError("双文件模式需要两个字幕文件")
    if mode != "two-files" and secondary is not None:
        raise ValueError("当前模式只接受一个字幕文件")
    if mode == "bilingual-inline" and (not separator or not separator.strip()):
        raise ValueError("同行双语需要明确的非空白分隔符")
    rows = parse_srt(primary)
    other = "target" if first_track == "source" else "source"
    tracks = {"source": [], "target": []}
    issues = []

    def entry(row, text, track, index):
        return {"import_id": f"{track}:{index}", "original_number": row["number"],
                "start_ms": row["start"], "end_ms": row["end"], "text": text}

    for index, row in enumerate(rows):
        if mode in {"single", "two-files"}:
            tracks[first_track].append(entry(row, row["text"], first_track, index))
            continue
        parts = (row["text"].splitlines() if mode == "bilingual-lines"
                 else row["text"].split(separator))
        if len(parts) != 2 or not all(part.strip() for part in parts):
            issues.append({"code": "ambiguous_track_split", "entry": index,
                           "original_text": row["text"],
                           "message": "不能明确拆成两轨，请调整格式后重新预览"})
            continue
        for track, part in zip((first_track, other), parts):
            tracks[track].append(entry(row, part.strip(), track, index))
    if mode == "two-files":
        tracks[other] = [entry(row, row["text"], other, index)
                         for index, row in enumerate(parse_srt(secondary))]

    # Only unique exact timing pairs are suggested. No index-based alignment,
    # tolerance snap, forced overlap merge, or dropped unmatched entries.
    by_time = {}
    for track in tracks:
        for row in tracks[track]:
            key = (row["start_ms"], row["end_ms"])
            by_time.setdefault(key, {"source": [], "target": []})[track].append(row["import_id"])
    pairs = []
    unmatched = []
    for sides in by_time.values():
        if len(sides["source"]) == len(sides["target"]) == 1:
            pairs.append({"source": sides["source"][0], "target": sides["target"][0]})
        else:
            unmatched.extend(sides["source"] + sides["target"])
    return {"mode": mode, "tracks": tracks, "pairs": pairs,
            "unmatched": unmatched, "issues": issues, "can_import": not issues}
