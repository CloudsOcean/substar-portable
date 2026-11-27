from __future__ import annotations

from typing import Any, Mapping

from substar_core.model_providers import MODEL_PROVIDER_CATALOG
from substar_core.job_diagnostics import safe_text


_PROVIDER_CREDENTIAL_LABELS = {
    f"model_provider:{provider['id']}": str(provider["label"])
    for provider in MODEL_PROVIDER_CATALOG
}

_SUCCESS_STATES = frozenset({"succeeded", "succeeded_with_issues"})
_CANCEL_SETTLED_STATES = _SUCCESS_STATES | {"cancelled"}
_PROVIDER_LABELS = {provider["id"]: provider["label"] for provider in MODEL_PROVIDER_CATALOG}


def creation_error_report(task, task_input, *, fallback="", reason="") -> dict:
    error = task.get("error") or {}
    details = error.get("details") or {}
    diagnostics = details.get("diagnostics") or {}
    task_type = task.get("task_type")
    step = task.get("step") or ""
    code = error.get("code") or reason or ("editor_not_ready" if not task else "task_interrupted")
    report = {
        "code": code,
        "step": step,
        "phase": {"transcription": "听写", "segmentation": "字幕切分"}.get(task_type, "打开编辑器"),
        "task_id": task.get("task_id"),
        "attempt": task.get("attempt"),
        "returncode": details.get("returncode"),
        "protocol_error_count": details.get("protocol_error_count"),
    }
    if code == "worker_failed":
        if details.get("status") == "timed_out":
            message = "任务执行超时，请从断点继续"
        elif details.get("protocol_error_count") and not details.get("worker_message"):
            message = "Substar 执行进程未能正常报告结果"
        else:
            message = details.get("worker_message") or details.get("algorithm_stderr_tail") or "Substar 执行进程异常退出"
    else:
        message = {
            "worker_start_failed": "Substar 无法启动或登记执行进程",
            "worker_event_invalid": "执行进程返回的事件或产物未通过校验",
            "finalization_failed": "处理结果未能保存到项目",
            "application_shutdown": "Substar 关闭时任务尚未完成",
            "startup_reconcile_owner_lost": "Substar 重启后无法接管原任务",
            "resource_claim_race": "Substar 未能分配执行资源",
            "task_interrupted": "任务已中断",
        }.get(code) or fallback
    if step == "transcription.provider_submit" and diagnostics.get("code") in {"request_timeout", "connection_failed"}:
        message = "提交听写请求未完成，暂未确认云端是否接收任务"
    report["message"] = safe_text(message)
    for key in ("http_status", "provider_code", "provider_type", "provider_message",
                "request_id", "cloud_task_id", "finish_reason"):
        if key in diagnostics:
            report[key] = safe_text(diagnostics[key])
    if diagnostics.get("code"):
        report["diagnostic_code"] = safe_text(diagnostics["code"], limit=120)
    elif code == "finalization_failed":
        report["diagnostic_code"] = safe_text(details.get("exception_type"), limit=120)
    if code == "worker_event_invalid":
        report["validation_issue_count"] = details.get("error_count")

    if task_type == "segmentation" and task_input.get("mode") == "semantic":
        provider = task_input.get("provider") or {}
        provider_id = provider.get("id")
        report["provider"] = _PROVIDER_LABELS.get(provider_id, provider_id)
        report["model"] = safe_text(diagnostics.get("model") or (provider.get("grouping") or {}).get("model"), limit=160)
    elif task_type == "transcription":
        profile = task_input.get("profile_id")
        options = task_input.get("options") or {}
        if profile == "qwen_cloud":
            from substar_core.qwen_cloud_asr import DEFAULT_MODEL

            report.update(provider="阿里云百炼 · Qwen ASR",
                          model=safe_text(options.get("qwen_cloud_model") or DEFAULT_MODEL, limit=160))
        elif profile == "api":
            report.update(provider="自定义 ASR API", model=safe_text(options.get("api_model"), limit=160))
    if diagnostics.get("code") in {"ENOSPC", "ENOENT", "EACCES", "EPERM", "file_in_use"}:
        report["source"] = "本地文件系统"
        report["message"] = safe_text(diagnostics.get("message")) or report["message"]
    if "media_exit_code" in diagnostics:
        report["media_exit_code"] = diagnostics["media_exit_code"]
    return report


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
    # One project-wide scale, including interrupted and cancelled states.
    # ASR owns 0-33%; segmentation owns 33-99%; readiness alone grants 100%.
    asr_progress = max(0.0, min(1.0, float(transcription.get("progress", 0.0))))
    split_progress = max(0.0, min(1.0, float(segmentation.get("progress", 0.0))))
    progress = (
        min(0.99, 0.33 + split_progress * 0.67)
        if transcription_state in _SUCCESS_STATES
        else asr_progress * 0.33
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
            return {"status": state, "progress": progress, "message": f"{label}未完成", "error": _error_message(task)}
        if state == "cancelled":
            return {"status": "cancelled", "progress": progress, "message": "任务已取消，项目文件已保留", "error": ""}
    if transcription_state not in _SUCCESS_STATES:
        return {
            "status": "queued" if transcription_state == "queued" else "running",
            "progress": progress,
            "message": str(transcription.get("progress_message") or "正在生成词级听写证据"),
            "error": "",
        }
    if segmentation_state not in _SUCCESS_STATES:
        return {
            "status": "queued" if segmentation_state == "queued" else "running",
            "progress": progress,
            "message": str(segmentation.get("progress_message") or "正在生成可编辑字幕草稿"),
            "error": "",
        }
    return {"status": "failed", "progress": 0.99, "message": "编辑器就绪校验未通过", "error": "切分任务已完成，但编辑文档、媒体或波形文件不可读"}
