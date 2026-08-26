from __future__ import annotations

from pathlib import Path


WHISPER_MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def whisper_repo_id(model_name: str) -> str:
    return WHISPER_MODEL_REPOS.get(model_name.strip(), model_name.strip())


def _repo_cache_name(model_id: str) -> str:
    return f"models--{model_id.replace('/', '--')}"


def find_cached_snapshot(
    cache_dir: str | Path,
    model_id: str,
    *,
    required_files: tuple[str, ...],
) -> Path | None:
    """Return a complete local Hugging Face snapshot without any network access."""

    if not str(cache_dir or "").strip():
        return None
    root = Path(cache_dir).expanduser()
    repo_name = _repo_cache_name(model_id)
    for repo_dir in (root / repo_name, root / "hub" / repo_name):
        snapshots_dir = repo_dir / "snapshots"
        if not snapshots_dir.is_dir():
            continue
        candidates: list[Path] = []
        ref = repo_dir / "refs" / "main"
        if ref.is_file():
            try:
                candidates.append(snapshots_dir / ref.read_text(encoding="utf-8").strip())
            except OSError:
                pass
        candidates.extend(
            sorted(
                (item for item in snapshots_dir.iterdir() if item.is_dir()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        )
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_dir() and all((candidate / name).is_file() for name in required_files):
                return candidate.resolve()
    return None


def resolve_local_model_path(
    explicit_path: str,
    cache_dir: str | Path,
    model_id: str,
    *,
    required_files: tuple[str, ...],
) -> Path | None:
    value = str(explicit_path or "").strip()
    if value:
        candidate = Path(value).expanduser().resolve()
        if not candidate.is_dir():
            raise RuntimeError(f"本地模型路径不存在：{candidate}")
        missing = [name for name in required_files if not (candidate / name).is_file()]
        if missing:
            raise RuntimeError(
                f"本地模型路径不完整：{candidate}（缺少 {', '.join(missing)}）"
            )
        return candidate
    return find_cached_snapshot(cache_dir, model_id, required_files=required_files)
