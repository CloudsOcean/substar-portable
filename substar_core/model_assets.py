from __future__ import annotations

import importlib.util
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from .config import PROJECT_MODELS_ROOT, load_settings
from .model_paths import find_cached_snapshot, whisper_repo_id


DOWNLOAD_SOURCES = {
    "china_mirror": {
        "label": "中国友好源（推荐）",
        "endpoint": "https://hf-mirror.com",
        "description": "优先经 Hugging Face 镜像下载，模型来源与许可证不变。",
    },
    "official": {
        "label": "Hugging Face 官方",
        "endpoint": "https://huggingface.co",
        "description": "直接访问模型发布者的官方仓库。",
    },
    "custom": {
        "label": "自定义兼容源",
        "endpoint": "",
        "description": "使用自定义 Hugging Face 兼容端点。",
    },
}


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, AttributeError):
        return False


def _asset_definitions(settings: dict[str, Any]) -> list[dict[str, Any]]:
    whisper_repo = whisper_repo_id(str(settings.get("whisper_model", "large-v3-turbo")))
    qwen_asr_repo = str(settings.get("qwen_asr_model", "Qwen/Qwen3-ASR-1.7B"))
    qwen_aligner_repo = str(settings.get("qwen_aligner_model", "Qwen/Qwen3-ForcedAligner-0.6B"))
    parakeet_repo = str(settings.get("parakeet_model", "nvidia/parakeet-tdt-0.6b-v3"))
    return [
        {
            "id": "faster_whisper_large_v3_turbo",
            "label": "Faster-Whisper Large-v3 Turbo",
            "provider": "Whisper",
            "purpose": "英语主稿与原生词时间",
            "repo_id": whisper_repo,
            "official_url": f"https://huggingface.co/{whisper_repo}",
            "required_files": ("config.json", "model.bin"),
            "explicit_setting": "whisper_model_path",
            "modules": ("faster_whisper",),
            "profiles": ("faster_whisper_native", "whisperx_forced"),
            "downloadable": True,
        },
        {
            "id": "qwen3_asr_1_7b",
            "label": "Qwen3-ASR 1.7B",
            "provider": "Qwen",
            "purpose": "多语言主稿识别",
            "repo_id": qwen_asr_repo,
            "official_url": f"https://huggingface.co/{qwen_asr_repo}",
            "required_files": ("config.json", "model.safetensors.index.json"),
            "explicit_setting": "qwen_asr_model_path",
            "modules": ("qwen_asr",),
            "profiles": ("qwen3_full",),
            "downloadable": True,
        },
        {
            "id": "qwen3_forced_aligner_0_6b",
            "label": "Qwen3 ForcedAligner 0.6B",
            "provider": "Qwen",
            "purpose": "剪映与 Qwen 主稿的词级强制对齐",
            "repo_id": qwen_aligner_repo,
            "official_url": f"https://huggingface.co/{qwen_aligner_repo}",
            "required_files": ("config.json", "model.safetensors"),
            "explicit_setting": "qwen_aligner_model_path",
            "modules": ("qwen_asr",),
            "profiles": ("jianying_qwen3", "qwen3_full"),
            "downloadable": True,
        },
        {
            "id": "parakeet_tdt_0_6b_v3",
            "label": "NVIDIA Parakeet TDT 0.6B v3",
            "provider": "NVIDIA",
            "purpose": "英语主稿与原生词时间",
            "repo_id": parakeet_repo,
            "official_url": f"https://huggingface.co/{parakeet_repo}",
            "explicit_setting": "parakeet_model_path",
            "modules": ("nemo.collections.asr",),
            "profiles": ("parakeet_tdt_native",),
            "downloadable": True,
            "local_file": PROJECT_MODELS_ROOT / "parakeet" / "parakeet-tdt-0.6b-v3.nemo",
        },
        {
            "id": "whisperx_english_ctc",
            "label": "WhisperX English CTC",
            "provider": "WhisperX / PyTorch",
            "purpose": "英语词级 CTC 精对齐",
            "repo_id": "WAV2VEC2_ASR_BASE_960H",
            "official_url": "https://pytorch.org/audio/stable/pipelines.html",
            "explicit_setting": "whisperx_alignment_model_path",
            "modules": ("whisperx",),
            "profiles": ("whisperx_forced",),
            "downloadable": False,
            "local_glob": PROJECT_MODELS_ROOT / "whisperx",
        },
    ]


def _explicit_or_cached(asset: dict[str, Any], settings: dict[str, Any]) -> Path | None:
    explicit = str(settings.get(asset.get("explicit_setting", ""), "")).strip()
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.exists():
            return candidate
    local_file = asset.get("local_file")
    if isinstance(local_file, Path) and local_file.is_file():
        return local_file.resolve()
    local_glob = asset.get("local_glob")
    if isinstance(local_glob, Path) and local_glob.is_dir():
        matches = sorted(local_glob.glob("*.pth"))
        if matches:
            return matches[0].resolve()
    required = tuple(asset.get("required_files", ()))
    if required:
        return find_cached_snapshot(
            PROJECT_MODELS_ROOT,
            str(asset.get("repo_id", "")),
            required_files=required,
        )
    return None


def selected_download_source(settings: dict[str, Any]) -> dict[str, Any]:
    source_id = str(settings.get("model_download_source", "china_mirror"))
    source = dict(DOWNLOAD_SOURCES.get(source_id, DOWNLOAD_SOURCES["china_mirror"]))
    source["id"] = source_id if source_id in DOWNLOAD_SOURCES else "china_mirror"
    if source["id"] == "custom":
        source["endpoint"] = str(settings.get("hf_endpoint", "")).strip()
    return source


def model_asset_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = settings or load_settings(include_secret=False)
    source = selected_download_source(settings)
    assets: list[dict[str, Any]] = []
    for definition in _asset_definitions(settings):
        path = _explicit_or_cached(definition, settings)
        missing_modules = [
            module for module in definition.get("modules", ()) if not _module_available(module)
        ]
        installed = path is not None
        ready = installed and not missing_modules
        assets.append({
            "id": definition["id"],
            "label": definition["label"],
            "provider": definition["provider"],
            "purpose": definition["purpose"],
            "repo_id": definition["repo_id"],
            "official_url": definition["official_url"],
            "mirror_url": (
                f"{str(source.get('endpoint', '')).rstrip('/')}/{definition['repo_id']}"
                if str(source.get("endpoint", "")).strip() and "/" in str(definition["repo_id"])
                else ""
            ),
            "path": str(path) if path else "",
            "installed": installed,
            "ready": ready,
            "missing_modules": missing_modules,
            "profiles": list(definition.get("profiles", ())),
            "downloadable": bool(definition.get("downloadable")),
        })
    return {
        "model_root": str(PROJECT_MODELS_ROOT),
        "download_source": source,
        "download_sources": [dict(value, id=key) for key, value in DOWNLOAD_SOURCES.items()],
        "assets": assets,
    }


_DOWNLOAD_LOCK = threading.Lock()
_DOWNLOAD_JOBS: dict[str, dict[str, Any]] = {}


def start_asset_download(asset_id: str) -> dict[str, Any]:
    settings = load_settings(include_secret=False)
    definition = next((row for row in _asset_definitions(settings) if row["id"] == asset_id), None)
    if definition is None:
        raise ValueError("未知模型资产")
    if not definition.get("downloadable"):
        raise ValueError("该资产由对应引擎管理，请使用官方页面或首次运行自动下载")
    job_id = f"mdl_{uuid.uuid4().hex[:12]}"
    job = {
        "job_id": job_id,
        "asset_id": asset_id,
        "status": "queued",
        "message": "等待下载",
        "path": "",
    }
    with _DOWNLOAD_LOCK:
        _DOWNLOAD_JOBS[job_id] = job

    def run() -> None:
        try:
            from huggingface_hub import snapshot_download

            source = selected_download_source(settings)
            with _DOWNLOAD_LOCK:
                job.update(status="running", message=f"正在通过{source['label']}下载")
            try:
                path = snapshot_download(
                    repo_id=str(definition["repo_id"]),
                    cache_dir=str(PROJECT_MODELS_ROOT),
                    endpoint=str(source.get("endpoint", "")).strip() or None,
                )
            except Exception:
                if source["id"] == "official" or not settings.get("model_download_fallback_official", True):
                    raise
                with _DOWNLOAD_LOCK:
                    job.update(message="推荐源不可用，正在回退 Hugging Face 官方源")
                path = snapshot_download(
                    repo_id=str(definition["repo_id"]),
                    cache_dir=str(PROJECT_MODELS_ROOT),
                    endpoint="https://huggingface.co",
                )
            if asset_id == "parakeet_tdt_0_6b_v3":
                nemo_files = sorted(Path(path).rglob("*.nemo"))
                if nemo_files:
                    target = PROJECT_MODELS_ROOT / "parakeet" / "parakeet-tdt-0.6b-v3.nemo"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(nemo_files[0], target)
                    path = str(target)
            with _DOWNLOAD_LOCK:
                job.update(status="completed", message="下载完成并已登记", path=str(path))
        except Exception as exc:
            with _DOWNLOAD_LOCK:
                job.update(status="failed", message=str(exc))

    threading.Thread(target=run, daemon=True, name=f"download-{asset_id}").start()
    return dict(job)


def download_job(job_id: str) -> dict[str, Any] | None:
    with _DOWNLOAD_LOCK:
        value = _DOWNLOAD_JOBS.get(job_id)
        return dict(value) if value else None
