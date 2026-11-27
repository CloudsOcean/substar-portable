from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import queue
import threading
from typing import Any, BinaryIO, Iterator
import uuid
import zipfile

from substar_core.artifacts import atomic_write_json
from substar_core.domain import ChangeKind, ChangeProvenance, EditorDocument
from substar_core.storage import ProjectStore
from substar_core.task_info import save_task_info


EXCHANGE_SCHEMA = "substar.subtitle-project.v1"
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


def _portable_media_entries(entries, job_dir):
    from substar_core.media_reference import read_reference, resolve_reference
    manifest_path=job_dir/'run_manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    media=None
    reference = read_reference(job_dir)
    if reference:
        media=resolve_reference(job_dir)
        if reference.get("media"):
            manifest["media"] = reference["media"]
    else:
        raw=Path(str(manifest.get('source_path') or ''))
        if str(raw) != '.':
            candidate=raw if raw.is_absolute() else job_dir/raw
            if candidate.is_file() and candidate.resolve().is_relative_to(job_dir.resolve()): media=candidate
        if media is None:
            name=Path(str(manifest.get('source_file') or '')).name
            candidate=job_dir/'input'/name
            if name and candidate.is_file(): media=candidate
    if media is None:
        legacy = sorted(p for p in (job_dir/'input').glob('*') if p.is_file() and p.suffix.lower() in {'.mp4','.mov','.mkv','.mp3','.wav','.m4a','.webm','.avi'})
        if not read_reference(job_dir) and not manifest.get('media_relinked') and legacy:
            media=legacy[0]
    if media is None:
        raise ProjectExchangeError('原媒体不存在，请重新链接后导出')
    suffixes={'.mp4','.mov','.mkv','.avi','.webm','.m4v','.mp3','.wav','.m4a','.aac','.flac','.ogg'}
    entries=[e for e in entries if e[0]!='project/run_manifest.json' and not (e[0].startswith('project/input/') and Path(e[0]).suffix.lower() in suffixes)]
    relative='input/'+media.name
    manifest.update(source_path=relative,source_file=media.name,media_relinked=False)
    entries.append(('project/'+relative,media,None))
    entries.append(('project/run_manifest.json',None,json.dumps(manifest,ensure_ascii=False).encode('utf-8')))
    return entries

def _portable_document_bytes(job_dir: Path, revision: Any) -> bytes:
    # Carry the necessary ancestry in the snapshot itself. Older revisions may
    # predate embedded parent_sources; recover those records from local history.
    value = json.loads(json.dumps(revision.document.to_dict(), ensure_ascii=False, allow_nan=False))
    known = {s["token_id"]: s for s in value.get("source_tokens", [])}
    history_loaded = False

    def load_history():
        nonlocal history_loaded
        if history_loaded:
            return
        history_loaded = True
        root = job_dir / "project"
        if not (root / "project.sqlite3").is_file():
            return
        store = ProjectStore.open(root)
        for row in store.list_revision_metadata():
            historical = store.load_revision(row["revision_id"])
            for source in historical.document.source_tokens:
                known.setdefault(source.token_id, source.to_dict())

    def complete(source, ancestors):
        timing = source.get("timing")
        if not timing or not timing.get("parent_source_ids"):
            return
        embedded = {s["token_id"]: s for s in timing.get("parent_sources", [])}
        parents, missing = [], []
        for parent_id in dict.fromkeys(timing["parent_source_ids"]):
            parent = embedded.get(parent_id) or known.get(parent_id)
            if parent is None:
                load_history()
                parent = known.get(parent_id)
            if parent is None or parent_id in ancestors:
                missing.append(parent_id)
                continue
            parent = json.loads(json.dumps(parent, allow_nan=False))
            complete(parent, ancestors | {parent_id})
            parents.append(parent)
        timing["parent_sources"] = parents
        if missing:
            timing.update(kind="unknown", method="missing_parent_evidence",
                          missing_parent_source_ids=missing,
                          parent_source_ids=[p["token_id"] for p in parents])

    for source in value.get("source_tokens", []):
        complete(source, {source["token_id"]})
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def export_subtitle_project(target: Path, *, project_id: str, job_dir: Path, revision: Any) -> None:
    entries = _subtitle_project_entries(job_dir=job_dir, revision=revision)
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
    if sum(row["size"] for row in manifest_files) + len(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_EXPANDED_BYTES:
        raise ProjectExchangeError("字幕工程超过导入大小限制")
    # Stored entries guarantee every legal export meets the import ratio limit,
    # including silent PCM and repetitive JSON, without a second compression pass.
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        for name, path, content in entries:
            if path:
                archive.write(path, name)
            else:
                archive.writestr(name, content or b"")


class _StreamingZipWriter:
    """A non-seekable file object that forwards ZIP bytes to a consumer."""

    def __init__(self, chunks: "queue.Queue[bytes | BaseException | None]", cancelled: threading.Event) -> None:
        self._chunks = chunks
        self._cancelled = cancelled
        self._position = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._position

    def flush(self) -> None:
        return None

    def write(self, value: bytes | bytearray | memoryview) -> int:
        content = bytes(value)
        if not content:
            return 0
        while not self._cancelled.is_set():
            try:
                self._chunks.put(content, timeout=0.25)
                self._position += len(content)
                return len(content)
            except queue.Full:
                continue
        raise BrokenPipeError("subtitle project download was cancelled")


def _subtitle_project_entries(
    *, job_dir: Path, revision: Any
) -> list[tuple[str, Path | None, bytes | None]]:
    document_bytes = _portable_document_bytes(job_dir, revision)
    entries: list[tuple[str, Path | None, bytes | None]] = [
        ("document/latest.json", None, document_bytes)
    ]
    from substar_core.media_reference import waveform_path
    audio = waveform_path(job_dir)
    if audio.is_file():
        entries.append(("project/audio_16k_mono.wav", audio, None))
    candidates = list(job_dir.rglob("recognition_evidence.json"))
    input_dir = job_dir / "input"
    if input_dir.is_dir():
        candidates.extend(path for path in input_dir.iterdir() if path.is_file())
    for name in (
        "run_manifest.json", "task_info.json", "subtitle_import.json",
        "reference_alignment.json", "reference_script_alignment.json", "recognition_evidence.json",
    ):
        candidates.append(job_dir / name)
    for folder in ("calibration", "review", "recognition_evidence"):
        root = job_dir / folder
        if root.is_dir():
            candidates.extend(path for path in root.rglob("*") if path.is_file())
    seen: set[Path] = set()
    for path in candidates:
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        entries.append((f"project/{path.relative_to(job_dir).as_posix()}", path, None))
    entries = _portable_media_entries(entries, job_dir)
    if len(entries) + 1 > MAX_ARCHIVE_FILES:
        raise ProjectExchangeError("字幕工程文件数量超出导入限制")
    if sum(path.stat().st_size if path else len(content or b"") for _, path, content in entries) > MAX_EXPANDED_BYTES:
        raise ProjectExchangeError("字幕工程超过导入大小限制")
    return entries


def stream_subtitle_project(
    *, project_id: str, job_dir: Path, revision: Any
) -> Iterator[bytes]:
    """Stream a portable project without first building a potentially huge temp ZIP."""

    entries = _subtitle_project_entries(job_dir=job_dir, revision=revision)
    files = [{"path": name, "size": path.stat().st_size if path else len(content or b""), "sha256": "0" * 64}
             for name, path, content in entries]
    manifest = {"schema_version": EXCHANGE_SCHEMA, "source_project_id": project_id,
                "document_id": revision.document.document_id, "revision_id": revision.revision_id,
                "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"), "files": files}
    if sum(row["size"] for row in files) + len(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_EXPANDED_BYTES:
        raise ProjectExchangeError("字幕工程超过导入大小限制")
    return _stream_entries(entries, manifest)


def _stream_entries(entries, manifest) -> Iterator[bytes]:
    chunks: "queue.Queue[bytes | BaseException | None]" = queue.Queue(maxsize=8)
    cancelled = threading.Event()

    def produce() -> None:
        writer = _StreamingZipWriter(chunks, cancelled)
        manifest_files: list[dict[str, Any]] = []
        try:
            with zipfile.ZipFile(
                writer, "w", compression=zipfile.ZIP_STORED, allowZip64=True
            ) as archive:
                for name, path, content in entries:
                    size = path.stat().st_size if path else len(content or b"")
                    digest = hashlib.sha256()
                    compression = zipfile.ZIP_STORED  # Every exported member obeys the import ratio limit.
                    info = zipfile.ZipInfo(name)
                    info.compress_type = compression
                    with archive.open(info, "w", force_zip64=size >= zipfile.ZIP64_LIMIT) as destination:
                        if path:
                            with path.open("rb") as source:
                                for block in iter(lambda: source.read(1024 * 1024), b""):
                                    digest.update(block)
                                    destination.write(block)
                        else:
                            block = content or b""
                            digest.update(block)
                            destination.write(block)
                    manifest_files.append({
                        "path": name,
                        "size": size,
                        "sha256": digest.hexdigest(),
                    })
                manifest["files"] = manifest_files
                if sum(row["size"] for row in manifest_files) + len(json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_EXPANDED_BYTES:
                    raise ProjectExchangeError("字幕工程超过导入大小限制")
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                )
        except BaseException as exc:
            if not cancelled.is_set():
                chunks.put(exc)
        finally:
            if not cancelled.is_set():
                chunks.put(None)

    worker = threading.Thread(target=produce, name="subtitle-project-export", daemon=True)
    worker.start()
    try:
        while True:
            item = chunks.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        cancelled.set()


def _archive_relative_path(name: str) -> PurePosixPath:
    # Validate raw components: PurePath normalizes away empty and dot segments.
    from pathlib import PureWindowsPath
    parts = name.split("/")
    if (not name or any(p in {"", ".", ".."} for p in parts)
            or "\\" in name or PureWindowsPath(name).drive
            or any(":" in p or p.endswith((".", " ")) or PureWindowsPath(p).is_reserved()
                   or any(ord(c) < 32 for c in p) for p in parts)):
        raise ProjectExchangeError("字幕工程包含不安全路径")
    return PurePosixPath(*parts)


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_FILES:
        raise ProjectExchangeError("字幕工程文件数量超出限制")
    expanded = 0
    seen = set()
    for member in members:
        path = _archive_relative_path(member.filename[:-1] if member.is_dir() else member.filename)
        key = str(path).casefold()
        if key in seen:
            raise ProjectExchangeError("字幕工程包含不安全路径")
        seen.add(key)
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
            if len(declared) != len(manifest.get("files", [])):
                raise ProjectExchangeError("字幕工程包含重复文件清单")
            outputs = {}
            for name, row in declared.items():
                member = by_name.get(name)
                if member is None or member.is_dir() or member.file_size != int(row.get("size", -1)):
                    raise ProjectExchangeError(f"字幕工程文件清单不匹配：{name}")
                if name.startswith("project/"):
                    relative = _archive_relative_path(name.removeprefix("project/"))
                    if str(relative).casefold() == "media_reference.json":
                        raise ProjectExchangeError('工程包不得携带外部媒体引用')
                    output = temporary.joinpath(*relative.parts).resolve()
                    if not output.is_relative_to(temporary.resolve()) or output == temporary.resolve():
                        raise ProjectExchangeError("字幕工程包含不安全路径")
                    outputs[name] = output
            destinations = set(outputs.values())
            if any(parent in destinations for output in destinations for parent in output.parents):
                raise ProjectExchangeError("字幕工程包含冲突路径")
            document_content: bytes | None = None
            for name, row in declared.items():
                member = by_name.get(name)
                if member is None or member.file_size != int(row.get("size", -1)):
                    raise ProjectExchangeError(f"字幕工程文件清单不匹配：{name}")
                output = outputs.get(name)
                if output is not None:
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
        manifest_path = temporary / "run_manifest.json"
        if manifest_path.is_file():
            media_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_path = str(media_manifest.get("source_path") or media_manifest.get("source_file") or "")
            name = Path(raw_path).name
            internal = temporary / "input" / name
            media_manifest.pop("source_path", None)
            media_manifest.update(media_relinked=False, source_file=name if internal.is_file() else "")
            if internal.is_file():
                media_manifest["source_path"] = "input/" + name
            atomic_write_json(manifest_path, media_manifest)
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
        atomic_write_json(temporary / "project_creation.json", {
            "schema_version": "substar.project-creation.v2",
            "input_mode": "subtitle_project_import",
            "source_file": filename,
            "settings_overrides": {},
            "created_at": datetime.now().timestamp(),
        })
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
