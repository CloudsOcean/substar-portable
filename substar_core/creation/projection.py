from __future__ import annotations

from typing import Any, Mapping

from substar_core.model_providers import MODEL_PROVIDER_CATALOG


_PROVIDER_CREDENTIAL_LABELS = {
    f"model_provider:{provider['id']}": str(provider["label"])
    for provider in MODEL_PROVIDER_CATALOG
}

_SUCCESS_STATES = frozenset({"succeeded", "succeeded_with_issues"})
_CANCEL_SETTLED_STATES = _SUCCESS_STATES | {"cancelled"}


def _error_message(task: Mapping[str, Any]) -> str:
    error = task.get("error")
    if not isinstance(error, Mapping):
        return ""
    if error.get("code") == "credential_unavailable":
        details = error.get("details")
        reference = str(
            details.get("credential_ref", "") if isinstance(details, Mapping) else ""
        )
        labels = {
            "asr_qwen": "ASR_Qwen",
            "asr_generic": "ASR_Generic",
            "segment_deepseek": "Segment_DeepSeek",
            **_PROVIDER_CREDENTIAL_LABELS,
        }
        return f"{labels.get(reference, reference or '所需服务')} 密钥不可用，请在设置中保存密钥后重试。"
    return str(error.get("message") or error.get("code") or "")


def _review_required_count(task: Mapping[str, Any]) -> int:
    result = task.get("result")
    if not isinstance(result, Mapping):
        return 0
    summary = result.get("summary")
    if not isinstance(summary, Mapping):
        return 0
    try:
        return max(0, int(summary.get("review_required_count", 0)))
    except (TypeError, ValueError):
        return 0


def subtitle_creation_projection(
    *,
    transcription: Mapping[str, Any],
    segmentation: Mapping[str, Any],
    editor_ready: bool,
    cancel_requested: bool,
) -> dict[str, Any]:
    transcription_state = str(transcription.get("state", ""))
    segmentation_state = str(segmentation.get("state", ""))
    if (
        editor_ready
        and transcription_state in _SUCCESS_STATES
        and segmentation_state in _SUCCESS_STATES
        and not cancel_requested
    ):
        review_count = max(
            _review_required_count(transcription),
            _review_required_count(segmentation),
        )
        needs_attention = bool(
            transcription.get("needs_attention")
            or segmentation.get("needs_attention")
            or "succeeded_with_issues"
            in {transcription_state, segmentation_state}
        )
        return {
            "status": "awaiting_edit",
            "progress": 1.0,
            "message": (
                f"项目已创建，可以进入编辑模式；有 {review_count} 处需要复核"
                if review_count
                else (
                    "项目已创建，可以进入编辑模式；有结果需要复核"
                    if needs_attention
                    else "项目已创建，可以进入编辑模式"
                )
            ),
            "error": "",
        }
    progress = max(
        float(transcription.get("progress", 0.0)) * 0.35,
        min(0.99, 0.35 + float(segmentation.get("progress", 0.0)) * 0.65),
    )
    if cancel_requested or "cancelling" in {transcription_state, segmentation_state}:
        finished = (
            transcription_state in _CANCEL_SETTLED_STATES
            and segmentation_state in _CANCEL_SETTLED_STATES
        )
        return {
            "status": "cancelled" if finished else "running",
            "progress": progress,
            "message": "任务已取消，项目文件已保留" if finished else "正在安全取消任务",
            "error": "",
        }
    for task, label in ((transcription, "听写"), (segmentation, "字幕切分")):
        state = str(task.get("state", ""))
        if state in {"failed", "interrupted"}:
            return {"status": state, "progress": float(task.get("progress", 0.0)), "message": f"{label}未完成", "error": _error_message(task)}
        if state == "cancelled":
            return {"status": "cancelled", "progress": float(task.get("progress", 0.0)), "message": "任务已取消，项目文件已保留", "error": ""}
    if transcription_state not in _SUCCESS_STATES:
        return {
            "status": "queued" if transcription_state == "queued" else "running",
            "progress": float(transcription.get("progress", 0.0)) * 0.35,
            "message": str(transcription.get("progress_message") or "正在生成词级听写证据"),
            "error": "",
        }
    if segmentation_state not in _SUCCESS_STATES:
        return {
            "status": "queued" if segmentation_state == "queued" else "running",
            "progress": min(0.99, 0.35 + float(segmentation.get("progress", 0.0)) * 0.65),
            "message": str(segmentation.get("progress_message") or "正在生成可编辑字幕草稿"),
            "error": "",
        }
    return {"status": "failed", "progress": 0.99, "message": "编辑器就绪校验未通过", "error": "切分任务已完成，但编辑文档、媒体或波形文件不可读"}
