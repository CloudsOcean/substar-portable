from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_RECOGNITION_PROFILE = "qwen_cloud"


@dataclass(frozen=True)
class RecognitionProfile:
    id: str
    label: str
    short_label: str
    description: str
    transcript_adapter: str
    alignment_adapter: str
    diarization_adapter: str | None = None
    requires_srt: bool = False
    supported_languages: tuple[str, ...] = ("en",)
    required_modules: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value["supported_languages"] = list(self.supported_languages)
        value["available"] = True
        value["missing_modules"] = []
        return value


_PROFILES = (
    RecognitionProfile(
        id="qwen_cloud",
        label="Qwen 云端 · 原生词级时间",
        short_label="Qwen 云端",
        description="阿里云百炼文件听写，直接返回原生词级时间戳和说话人。",
        transcript_adapter="qwen_cloud_filetrans",
        alignment_adapter="qwen_cloud_native",
        diarization_adapter="qwen_cloud_native",
        supported_languages=("auto", "en", "zh", "ja", "ko", "de", "fr", "es"),
    ),
)
_BY_ID = {profile.id: profile for profile in _PROFILES}


def list_recognition_profiles() -> list[dict[str, Any]]:
    return [profile.public() for profile in _PROFILES]


def get_recognition_profile(profile_id: str) -> RecognitionProfile:
    requested = str(profile_id).strip()
    try:
        return _BY_ID[requested]
    except KeyError as exc:
        raise ValueError(f"未知识别方案：{profile_id}") from exc


def profile_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Resolve the canonical cloud recognition profile."""

    resolved = dict(settings)
    requested = str(resolved.get("recognition_profile_id", "")).strip()
    if not requested:
        requested = DEFAULT_RECOGNITION_PROFILE
    profile = get_recognition_profile(requested)
    resolved["recognition_profile_id"] = profile.id
    resolved["recognition_profile_label"] = profile.label
    resolved["recognition_transcript_adapter"] = profile.transcript_adapter
    resolved["recognition_alignment_adapter"] = profile.alignment_adapter
    resolved["recognition_diarization_adapter"] = profile.diarization_adapter
    resolved["transcript_source"] = "qwen_cloud"
    resolved["alignment_source"] = "qwen_cloud_native"
    return resolved
