from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json
from .config import INSTALL_ROOT, PROJECT_MODELS_ROOT, PROJECT_ROOT, DATA_ROOT, load_settings
from .model_assets import model_asset_status
from .recognition.registry import list_recognition_profiles


def _command_output(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr).strip()


def _find_tool(name: str) -> str:
    suffix = ".exe" if sys.platform == "win32" else ""
    for candidate in (
        INSTALL_ROOT / "ffmpeg" / "bin" / f"{name}{suffix}",
        INSTALL_ROOT / "runtime" / "ffmpeg" / "bin" / f"{name}{suffix}",
        INSTALL_ROOT / "tools" / "ffmpeg" / "bin" / f"{name}{suffix}",
    ):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name) or ""


def environment_status() -> dict[str, Any]:
    package: dict[str, Any] = {"edition": "development"}
    package_manifest = PROJECT_ROOT / "portable_manifest.json"
    if package_manifest.is_file():
        try:
            loaded = json.loads(package_manifest.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                package = loaded
        except (OSError, json.JSONDecodeError):
            pass

    nvidia = _command_output([
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total",
        "--format=csv,noheader",
    ])
    torch_info: dict[str, Any] = {
        "installed": False,
        "cuda_available": False,
        "version": "",
        "cuda_version": "",
    }
    try:
        import torch

        torch_info = {
            "installed": True,
            "version": str(torch.__version__),
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_version": str(torch.version.cuda or ""),
        }
    except Exception:
        pass

    ffmpeg, ffprobe = _find_tool("ffmpeg"), _find_tool("ffprobe")
    disk = shutil.disk_usage(INSTALL_ROOT)
    checks = {
        "python": {
            "ok": sys.version_info >= (3, 10),
            "status": "ready" if sys.version_info >= (3, 10) else "incompatible",
            "value": sys.version.split()[0],
            "path": sys.executable,
            "message": "程序当前使用的 Python 解释器",
        },
        "ffmpeg": {
            "ok": bool(ffmpeg),
            "status": "ready" if ffmpeg else "missing",
            "path": ffmpeg,
            "message": "视频转码与音频抽取" if ffmpeg else "未找到 FFmpeg",
        },
        "ffprobe": {
            "ok": bool(ffprobe),
            "status": "ready" if ffprobe else "missing",
            "path": ffprobe,
            "message": "媒体信息读取" if ffprobe else "未找到 FFprobe",
        },
        "nvidia_gpu": {
            "ok": bool(nvidia),
            "status": "ready" if nvidia else "optional",
            "value": nvidia,
            "message": "NVIDIA GPU 与驱动" if nvidia else "未检测到 NVIDIA GPU，可使用 CPU 路线",
        },
        "torch": {
            "ok": bool(torch_info["installed"]),
            "status": (
                "ready" if torch_info["cuda_available"] else
                "degraded" if torch_info["installed"] else "missing"
            ),
            **torch_info,
            "value": (
                f"PyTorch {torch_info['version']} · CUDA {torch_info['cuda_version']} 可用"
                if torch_info["cuda_available"] else
                f"PyTorch {torch_info['version']} · CUDA 不可用"
                if torch_info["installed"] else "未安装 PyTorch"
            ),
            "message": "本地神经网络推理运行时",
        },
        "disk_free": {
            "ok": disk.free >= 5 * 1024**3,
            "status": "ready" if disk.free >= 5 * 1024**3 else "warning",
            "bytes": disk.free,
            "value": f"{disk.free / 1024**3:.1f} GB 可用",
            "path": str(INSTALL_ROOT),
            "message": "建议至少保留 5 GB",
        },
    }

    asset_report = model_asset_status(load_settings(include_secret=False))
    assets_by_id = {row["id"]: row for row in asset_report["assets"]}
    profile_assets = {
        "faster_whisper_native": ("faster_whisper_large_v3_turbo",),
        "jianying_qwen3": ("qwen3_forced_aligner_0_6b",),
        "parakeet_tdt_native": ("parakeet_tdt_0_6b_v3",),
        "whisperx_forced": ("faster_whisper_large_v3_turbo", "whisperx_english_ctc"),
        "qwen3_full": ("qwen3_asr_1_7b", "qwen3_forced_aligner_0_6b"),
    }
    profiles: list[dict[str, Any]] = []
    for profile in list_recognition_profiles():
        required = [assets_by_id[item] for item in profile_assets.get(profile["id"], ())]
        missing_assets = [item["id"] for item in required if not item["installed"]]
        missing_modules = sorted({
            module
            for item in required
            for module in item.get("missing_modules", [])
        })
        ready = not missing_assets and not missing_modules
        profiles.append({
            "id": profile["id"],
            "label": profile["label"],
            "description": profile["description"],
            "ready": ready,
            "missing_assets": missing_assets,
            "missing_modules": missing_modules,
            "assets": [item["id"] for item in required],
        })

    ready_profiles = sum(1 for profile in profiles if profile["ready"])
    core_runtime_ready = all(
        checks[name]["ok"] for name in ("python", "ffmpeg", "ffprobe", "disk_free")
    )
    environment_ready = core_runtime_ready and ready_profiles > 0
    return {
        "schema_version": "substar.environment-report.v2",
        "package": package,
        "checks": checks,
        # Overall readiness means that this edition can execute at least one
        # advertised recognition path.  Optional local inference dependencies
        # must never make the cloud-only portable edition look broken.
        "ready": environment_ready,
        "core_runtime_ready": core_runtime_ready,
        "local_inference_ready": bool(torch_info["installed"]),
        "gpu_acceleration": bool(nvidia and torch_info["cuda_available"]),
        "assets": asset_report["assets"],
        "asset_root": asset_report["model_root"],
        "download_source": asset_report["download_source"],
        "download_sources": asset_report["download_sources"],
        "profiles": profiles,
        "ready_profile_count": ready_profiles,
        "profile_count": len(profiles),
        "full_models_ready": all(asset["installed"] for asset in asset_report["assets"]),
    }


def configure_portable_environment() -> dict[str, Any]:
    for path in (
        PROJECT_MODELS_ROOT,
        INSTALL_ROOT / "runtime",
        DATA_ROOT,
        INSTALL_ROOT / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": "substar.portable-environment.v2",
        "model_cache_dir": str(PROJECT_MODELS_ROOT),
        "data_root": str(DATA_ROOT),
        "ffmpeg": _find_tool("ffmpeg"),
        "ffprobe": _find_tool("ffprobe"),
    }
    atomic_write_json(INSTALL_ROOT / "portable_environment.json", config)
    return {"configured": True, "configuration": config, "status": environment_status()}
