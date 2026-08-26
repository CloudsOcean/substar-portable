from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import requests

from .artifacts import atomic_write_json
from .config import INSTALL_ROOT


Progress = Callable[[str, float], None]
DEFAULT_MODEL = "qwen-audio-3.0-asr-flash-filetrans"
_REGION_BASE_URLS = {
    "beijing": "https://dashscope.aliyuncs.com/api/v1",
    "singapore": "https://dashscope-intl.aliyuncs.com/api/v1",
}
_TERMINAL_FAILURES = {"FAILED", "UNKNOWN", "CANCELED", "CANCELLED"}
_ACTIVE_STATUS_PROGRESS = {"PENDING": 0.40, "RUNNING": 0.46}


class QwenCloudAsrError(RuntimeError):
    pass


def _speaker_id(value: Any) -> str | None:
    """Normalize DashScope diarization labels to the editor's stable slots."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    match = re.fullmatch(r"(?:speaker[_-]?)?(\d+)", text)
    return f"speaker_{int(match.group(1))}" if match else text


def _base_url(settings: dict[str, Any]) -> str:
    explicit = str(settings.get("qwen_cloud_base_url", "")).strip().rstrip("/")
    if explicit:
        return explicit
    region = str(settings.get("qwen_cloud_region", "beijing")).strip().lower()
    return _REGION_BASE_URLS.get(region, _REGION_BASE_URLS["beijing"])


def _headers(api_key: str, *, async_call: bool = False) -> dict[str, str]:
    value = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if async_call:
        value["X-DashScope-Async"] = "enable"
        value["X-DashScope-OssResourceResolve"] = "enable"
    return value


def _detail(response: requests.Response) -> str:
    try:
        body = response.json()
        return str(body.get("message") or body.get("code") or body)
    except (ValueError, AttributeError):
        return response.text.strip()[:1000]


def _request(
    method: str,
    url: str,
    *,
    attempts: int,
    timeout: float,
    **kwargs: Any,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code < 400:
                return response
            if response.status_code not in {408, 409, 425, 429} and response.status_code < 500:
                raise QwenCloudAsrError(
                    f"Qwen 云端请求失败（HTTP {response.status_code}）：{_detail(response)}"
                )
            last_error = QwenCloudAsrError(
                f"Qwen 云端暂时不可用（HTTP {response.status_code}）：{_detail(response)}"
            )
        except QwenCloudAsrError:
            raise
        except requests.RequestException as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(min(8.0, 0.8 * (2 ** (attempt - 1))))
    raise QwenCloudAsrError(str(last_error or "Qwen 云端请求失败"))


def get_upload_policy(
    *, api_key: str, model: str, base_url: str, timeout: int, attempts: int = 3
) -> dict[str, Any]:
    response = _request(
        "GET",
        f"{base_url.rstrip('/')}/uploads",
        attempts=attempts,
        timeout=timeout,
        headers=_headers(api_key),
        params={"action": "getPolicy", "model": model},
    )
    value = response.json().get("data")
    if not isinstance(value, dict) or not value.get("upload_host") or not value.get("upload_dir"):
        raise QwenCloudAsrError("Qwen 云端上传凭证响应不完整")
    return value


def upload_temporary_file(
    path: Path,
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout: int,
    attempts: int = 3,
) -> str:
    policy = get_upload_policy(
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout=timeout,
        attempts=attempts,
    )
    maximum = int(float(policy.get("max_file_size_mb", 1024)) * 1024 * 1024)
    if path.stat().st_size > maximum:
        raise QwenCloudAsrError(
            f"临时上传音频超过当前凭证上限（{policy.get('max_file_size_mb')} MB）"
        )
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name) or "audio.mp3"
    key = f"{str(policy['upload_dir']).rstrip('/')}/{safe_name}"
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with path.open("rb") as handle:
                response = requests.post(
                    str(policy["upload_host"]),
                    files={
                        "OSSAccessKeyId": (None, str(policy["oss_access_key_id"])),
                        "Signature": (None, str(policy["signature"])),
                        "policy": (None, str(policy["policy"])),
                        "x-oss-object-acl": (None, str(policy["x_oss_object_acl"])),
                        "x-oss-forbid-overwrite": (None, str(policy["x_oss_forbid_overwrite"])),
                        "key": (None, key),
                        "success_action_status": (None, "200"),
                        "file": (safe_name, handle, "audio/mpeg"),
                    },
                    timeout=timeout,
                )
            if response.status_code == 200:
                return f"oss://{key}"
            last_error = QwenCloudAsrError(
                f"Qwen 临时文件上传失败（HTTP {response.status_code}）：{_detail(response)}"
            )
        except (OSError, requests.RequestException) as exc:
            last_error = exc
        if attempt < attempts:
            policy = get_upload_policy(
                api_key=api_key,
                model=model,
                base_url=base_url,
                timeout=timeout,
                attempts=attempts,
            )
            time.sleep(min(5.0, attempt * 0.8))
    raise QwenCloudAsrError(str(last_error or "Qwen 临时文件上传失败"))


def _ffmpeg_executable() -> str:
    candidates = (
        INSTALL_ROOT / "ffmpeg" / "bin" / "ffmpeg.exe",
        INSTALL_ROOT / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    resolved = shutil.which("ffmpeg")
    if resolved:
        return resolved
    raise QwenCloudAsrError("FFmpeg is unavailable for cloud-audio preparation")


def _encode_cloud_audio(
    wav_path: Path, output: Path, *, timeout_seconds: float = 900.0
) -> None:
    if output.is_file() and output.stat().st_size > 0:
        return
    output.unlink(missing_ok=True)
    # Keep the media extension last so FFmpeg can infer the output container.
    temporary = output.with_name(
        f".{output.stem}.{os.getpid()}.encoding{output.suffix}"
    )
    temporary.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            [
                _ffmpeg_executable(), "-hide_banner", "-loglevel", "error",
                "-nostdin", "-y", "-i", str(wav_path), "-vn", "-ac", "1",
                "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30.0, float(timeout_seconds)),
        )
    except subprocess.TimeoutExpired as exc:
        temporary.unlink(missing_ok=True)
        raise QwenCloudAsrError(
            f"Cloud-audio encoding exceeded {int(timeout_seconds)} seconds"
        ) from exc
    if (
        completed.returncode != 0
        or not temporary.is_file()
        or temporary.stat().st_size <= 0
    ):
        temporary.unlink(missing_ok=True)
        raise QwenCloudAsrError(
            "Cloud-audio encoding failed: "
            + (completed.stderr.strip() or f"ffmpeg {completed.returncode}")
        )
    os.replace(temporary, output)


def _transcription_url(output: dict[str, Any]) -> str:
    results = output.get("results")
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict):
                continue
            if str(result.get("subtask_status", "SUCCEEDED")).upper() in _TERMINAL_FAILURES:
                raise QwenCloudAsrError(
                    f"Qwen 云端子任务失败：{result.get('message') or result.get('code') or result}"
                )
            if result.get("transcription_url"):
                return str(result["transcription_url"])
    result = output.get("result")
    if isinstance(result, dict) and result.get("transcription_url"):
        return str(result["transcription_url"])
    return ""


def _submission_body(
    model: str, oss_url: str, settings: dict[str, Any]
) -> dict[str, Any]:
    language = str(settings.get("language", "Auto")).strip().lower()
    requested_language = (
        "" if language in {"", "auto", "automatic"} else language.split("-")[0]
    )
    context = str(settings.get("context", "")).strip()
    parameters: dict[str, Any] = {"channel_id": [0]}

    if model.startswith("qwen3-asr-flash-filetrans"):
        # Qwen3 file transcription uses a singular file_url and its own
        # language/context fields. It does not expose speaker diarization.
        parameters["enable_words"] = True
        if requested_language:
            parameters["language"] = requested_language
        if context:
            parameters["corpus"] = {"text": context[:20000]}
        provider_input: dict[str, Any] = {"file_url": oss_url}
    else:
        # Qwen-Audio 3.0 uses the shared file-transcription contract. Word
        # timestamps are always enabled; it accepts immediate vocabulary,
        # conversation context and native speaker diarization.
        parameters["diarization_enabled"] = True
        if requested_language:
            parameters["language_hints"] = [requested_language]
        raw_hotwords = settings.get("hotwords")
        if isinstance(raw_hotwords, dict):
            vocabulary = {
                str(text): int(weight)
                for text, weight in raw_hotwords.items()
                if str(text).strip()
                and not isinstance(weight, bool)
                and isinstance(weight, int)
                and (1 <= weight <= 5 or weight == 50)
            }
            if vocabulary:
                parameters["vocabulary"] = vocabulary
        provider_input = {"file_urls": [oss_url]}
        if context:
            provider_input["context"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": context[:400]}
                    ],
                }
            ]
    return {"model": model, "input": provider_input, "parameters": parameters}


def _submission_audit(
    body: dict[str, Any], settings: dict[str, Any], *, resumed: bool
) -> dict[str, Any]:
    """Describe the exact provider payload without publishing its signed URL."""

    public_body = json.loads(json.dumps(body))
    provider_input = public_body.get("input", {})
    if isinstance(provider_input, dict):
        if "file_url" in provider_input:
            provider_input["file_url"] = "[MEDIA]"
        if "file_urls" in provider_input:
            provider_input["file_urls"] = ["[MEDIA]"]
    canonical = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    context = str(settings.get("context", ""))
    hotwords = settings.get("hotwords")
    normalized_hotwords = (
        {
            str(text): int(weight)
            for text, weight in hotwords.items()
            if isinstance(weight, int) and not isinstance(weight, bool)
        }
        if isinstance(hotwords, dict)
        else {}
    )
    return {
        "schema_version": "substar.provider-submission-audit.v1",
        "provider": "alibaba_model_studio",
        "model": str(body.get("model", "")),
        "input_fingerprint": str(
            settings.get("_transcription_input_fingerprint", "")
        ),
        "resumed_remote_task": bool(resumed),
        "submitted_body_sha256": hashlib.sha256(canonical).hexdigest(),
        "public_body": public_body,
        "compilation": {
            "requested_prompt_sha256": hashlib.sha256(
                context.encode("utf-8")
            ).hexdigest(),
            "submitted_context_sha256": hashlib.sha256(
                json.dumps(
                    public_body.get("input", {}).get("context", []),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "requested_prompt_characters": len(context),
            "hotwords_sha256": hashlib.sha256(
                json.dumps(
                    normalized_hotwords,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "submitted_vocabulary": public_body.get("parameters", {}).get(
                "vocabulary", {}
            ),
        },
    }


_CJK_TEXT = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_SPACE_DELIMITED_FRAGMENT = re.compile(
    r"^[A-Za-z0-9\u00c0-\u024f'\u2019._\-\u2013\u2014/:+%&$\u20ac\u00a3\u00a5]+$"
)


def _can_join_qwen_fragments(previous: str, current: str) -> bool:
    """Return whether two no-space Qwen pieces form one natural word/token.

    Qwen file transcription exposes model tokenizer pieces in ``words``.  For
    space-delimited languages a leading space marks a new natural word, while
    pieces such as ``V`` + ``uit`` + ``ton`` or ``1`` + ``0`` + ``.`` + ``3``
    have no leading space.  Keep CJK records independent because those scripts
    do not use this whitespace convention.
    """

    if not previous or not current:
        return False
    if _CJK_TEXT.search(previous) or _CJK_TEXT.search(current):
        return False
    return bool(
        _SPACE_DELIMITED_FRAGMENT.fullmatch(previous)
        and _SPACE_DELIMITED_FRAGMENT.fullmatch(current)
        and re.search(r"[A-Za-z0-9\u00c0-\u024f]", previous + current)
    )


def _natural_qwen_units(
    words: list[Any], sentence: dict[str, Any]
) -> tuple[list[dict[str, Any]], int, int]:
    """Collapse Qwen tokenizer pieces into editor-level natural word units."""

    result: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    boundary_before_next = True
    raw_fragment_count = 0
    joined_fragment_count = 0

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        core = str(pending.pop("_core", ""))
        pending["kind"] = (
            "character" if len(core) == 1 and _CJK_TEXT.search(core) else "word"
        )
        result.append(pending)
        pending = None

    for word in words:
        if not isinstance(word, dict):
            continue
        raw_core = str(word.get("text", ""))
        punctuation = str(word.get("punctuation", "")).strip()
        core = raw_core.strip()
        if not core and not punctuation:
            # Qwen sometimes emits a whitespace-only record before a number.
            boundary_before_next = True
            continue
        raw_fragment_count += 1
        token = f"{core}{punctuation}"
        start = round(
            float(word.get("begin_time", sentence.get("begin_time", 0))) / 1000,
            3,
        )
        end = round(
            float(word.get("end_time", sentence.get("end_time", 0))) / 1000,
            3,
        )
        starts_new = (
            pending is None
            or boundary_before_next
            or bool(raw_core[:1].isspace())
        )
        if not starts_new and pending is not None and _can_join_qwen_fragments(
            str(pending["_core"]), core
        ):
            pending["text"] = f"{pending['text']}{token}"
            pending["_core"] = f"{pending['_core']}{core}"
            pending["end"] = max(float(pending["end"]), end)
            joined_fragment_count += 1
        else:
            flush()
            pending = {
                "text": token,
                "start": start,
                "end": end,
                "timing_source": "qwen_cloud_native",
                "_core": core,
            }
        boundary_before_next = False
    flush()
    return result, raw_fragment_count, joined_fragment_count


def _parse_result(
    value: dict[str, Any], *, model: str = DEFAULT_MODEL
) -> dict[str, Any]:
    transcripts = value.get("transcripts")
    if not isinstance(transcripts, list):
        raise QwenCloudAsrError("Qwen 云端转写结果缺少 transcripts")
    text_parts: list[str] = []
    chunks: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    languages: list[str] = []
    raw_word_fragment_count = 0
    joined_fragment_count = 0
    sentence_index = 0
    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        transcript_text = str(transcript.get("text", "")).strip()
        if transcript_text:
            text_parts.append(transcript_text)
        for sentence in transcript.get("sentences", []) or []:
            if not isinstance(sentence, dict):
                continue
            language = str(sentence.get("language", "")).strip()
            if language and language not in languages:
                languages.append(language)
            speaker_id = _speaker_id(sentence.get("speaker_id"))
            chunks.append({
                "index": len(chunks),
                "start": round(float(sentence.get("begin_time", 0)) / 1000, 3),
                "end": round(float(sentence.get("end_time", 0)) / 1000, 3),
                "text": str(sentence.get("text", "")).strip(),
                "language": language,
                "speaker_id": speaker_id,
            })
            sentence_units, raw_count, joined_count = _natural_qwen_units(
                sentence.get("words", []) or [], sentence
            )
            if sentence_units:
                for position, unit in enumerate(sentence_units):
                    unit["sentence_id"] = sentence_index
                    unit["sentence_start"] = position == 0
                    unit["sentence_end"] = position == len(sentence_units) - 1
                    unit["speaker_id"] = speaker_id
                    unit["speaker_confidence"] = (
                        1.0 if speaker_id is not None else 0.0
                    )
                sentence_index += 1
            raw_word_fragment_count += raw_count
            joined_fragment_count += joined_count
            for unit in sentence_units:
                unit["index"] = len(units)
                units.append(unit)
    if not units:
        raise QwenCloudAsrError("Qwen 云端转写没有返回词级时间戳；请确认模型支持词级时间并已开启该选项")
    text = " ".join(text_parts).strip()
    if not text:
        text = " ".join(str(item["text"]) for item in units).strip()
    return {
        "text": text,
        "language": ",".join(languages),
        "units": units,
        "chunks": chunks,
        "audit": {
            "schema_version": "substar.asr-ingest-report.v1",
            "status": "pass",
            "engine": model,
            "timing_source": "qwen_cloud_native",
            "sentence_count": len(chunks),
            "word_count": len(units),
            "raw_word_fragment_count": raw_word_fragment_count,
            "joined_fragment_count": joined_fragment_count,
        },
    }


def test_connection(settings: dict[str, Any], api_key: str) -> dict[str, Any]:
    if not str(api_key).strip():
        raise QwenCloudAsrError("尚未配置 Qwen 云端听写 API Key")
    model = str(settings.get("qwen_cloud_model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    policy = get_upload_policy(
        api_key=api_key,
        model=model,
        base_url=_base_url(settings),
        timeout=max(10, int(settings.get("qwen_cloud_request_timeout_seconds", 120))),
    )
    return {
        "ok": True,
        "message": f"Qwen 云端听写已联通 · {model} · 临时上传上限 {policy.get('max_file_size_mb', '—')} MB",
    }


def run_qwen_cloud_asr(
    wav_path: Path, settings: dict[str, Any], progress: Progress
) -> dict[str, Any]:
    api_key = str(settings.get("api_key", "")).strip()
    checkpoint_dir = Path(str(settings.get("_checkpoint_dir") or wav_path.parent / "ingest_chunks"))
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    state_path = checkpoint_dir / "qwen_cloud_state.json"
    result_path = checkpoint_dir / "qwen_cloud_result.json"
    audio_path = checkpoint_dir / "qwen_cloud_audio_64k.mp3"
    state: dict[str, Any] = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    model = str(settings.get("qwen_cloud_model", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    base_url = _base_url(settings)
    input_fingerprint = str(
        settings.get("_transcription_input_fingerprint", "")
    ).strip()
    state_matches_input = not input_fingerprint or (
        state.get("input_fingerprint") == input_fingerprint
    )
    state_matches_provider = (
        state.get("model") in {None, model}
        and state.get("base_url") in {None, base_url}
    )
    if not state_matches_input or not state_matches_provider:
        state = {}
    elif result_path.is_file():
        cached_body = _submission_body(
            model,
            str(state.get("oss_url") or "[CACHED_MEDIA]"),
            settings,
        )
        atomic_write_json(
            wav_path.parent / "provider_submission_audit.json",
            _submission_audit(cached_body, settings, resumed=True),
        )
        progress("复用已完成的 Qwen 云端听写结果", 0.78)
        return _parse_result(
            json.loads(result_path.read_text(encoding="utf-8")),
            model=model,
        )

    # Explicitly opt-in, process-local provider fake used by runtime and
    # packaging acceptance tests.  Production never sets this variable, so
    # credential checks and network behaviour remain identical there.
    if os.environ.get("SUBSTAR_MOCK_QWEN") == "1":
        mock_submission = _submission_body(model, "[TEST_MEDIA]", settings)
        atomic_write_json(
            wav_path.parent / "provider_submission_audit.json",
            _submission_audit(mock_submission, settings, resumed=False),
        )
        mock_result = {
            "transcripts": [
                {
                    "text": "Substar works.",
                    "sentences": [
                        {
                            "begin_time": 0,
                            "end_time": 1000,
                            "language": "en",
                            "text": "Substar works.",
                            "speaker_id": 1,
                            "words": [
                                {"begin_time": 0, "end_time": 500, "text": " Substar"},
                                {"begin_time": 500, "end_time": 1000, "text": " works."},
                            ],
                        }
                    ],
                }
            ]
        }
        atomic_write_json(result_path, mock_result)
        atomic_write_json(
            state_path,
            {
                "schema_version": "substar.qwen-cloud-asr.v1",
                "model": model,
                "base_url": base_url,
                "input_fingerprint": input_fingerprint,
                "completed_at": time.time(),
                "test_provider": True,
            },
        )
        progress("测试听写完成", 0.78)
        return _parse_result(mock_result, model=model)

    # A completed, input-bound provider result is an immutable local artifact
    # and can be finalized after restart without reopening credential authority.
    if not api_key:
        raise QwenCloudAsrError("尚未配置 Qwen 云端听写 API Key")

    cancel_requested = settings.get("_cancel_requested")

    def check_cancelled() -> None:
        if callable(cancel_requested) and cancel_requested():
            raise InterruptedError("transcription cancellation requested")

    def cancellable_wait(seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            check_cancelled()
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    timeout = max(10, int(settings.get("qwen_cloud_request_timeout_seconds", 120)))
    poll_seconds = max(1.0, float(settings.get("qwen_cloud_poll_interval_seconds", 3)))
    total_timeout = max(60, int(settings.get("qwen_cloud_task_timeout_seconds", 7200)))
    attempts = max(1, int(settings.get("http_retry_attempts", 2)) + 1)

    check_cancelled()
    progress("准备 Qwen 云端听写音频", 0.22)
    _encode_cloud_audio(wav_path, audio_path)
    check_cancelled()
    oss_url = str(state.get("oss_url", ""))
    uploaded_at = float(state.get("uploaded_at", 0) or 0)
    if not oss_url or time.time() - uploaded_at > 47 * 3600:
        progress("上传音频到 Qwen 临时存储", 0.28)
        oss_url = upload_temporary_file(
            audio_path,
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            attempts=attempts,
        )
        state = {
            "schema_version": "substar.qwen-cloud-asr.v1",
            "model": model,
            "base_url": base_url,
            "input_fingerprint": input_fingerprint,
            "oss_url": oss_url,
            "uploaded_at": time.time(),
        }
        atomic_write_json(state_path, state)

    task_id = str(state.get("task_id", ""))
    submission_body = _submission_body(model, oss_url, settings)
    atomic_write_json(
        wav_path.parent / "provider_submission_audit.json",
        _submission_audit(submission_body, settings, resumed=bool(task_id)),
    )
    if not task_id:
        progress("提交 Qwen 云端听写任务", 0.33)
        check_cancelled()
        response = _request(
            "POST",
            f"{base_url}/services/audio/asr/transcription",
            attempts=attempts,
            timeout=timeout,
            headers=_headers(api_key, async_call=True),
            json=submission_body,
        )
        output = response.json().get("output") or {}
        task_id = str(output.get("task_id", ""))
        if not task_id:
            raise QwenCloudAsrError(f"Qwen 云端没有返回 task_id：{response.text[:1000]}")
        state.update({"task_id": task_id, "submitted_at": time.time()})
        atomic_write_json(state_path, state)

    progress("Qwen 云端正在听写", 0.38)
    started = time.monotonic()
    last_status = ""
    while time.monotonic() - started < total_timeout:
        check_cancelled()
        response = _request(
            "GET",
            f"{base_url}/tasks/{task_id}",
            attempts=attempts,
            timeout=timeout,
            headers=_headers(api_key),
        )
        body = response.json()
        output = body.get("output") or {}
        status = str(output.get("task_status", "")).upper()
        if status and status != last_status:
            status_progress = _ACTIVE_STATUS_PROGRESS.get(status)
            if status_progress is not None:
                progress(f"Qwen 云端听写：{status}", status_progress)
            last_status = status
        if status == "SUCCEEDED":
            url = _transcription_url(output)
            if not url:
                raise QwenCloudAsrError("Qwen 云端任务成功但未返回转写结果 URL")
            result_response = _request("GET", url, attempts=attempts, timeout=timeout)
            value = result_response.json()
            atomic_write_json(result_path, value)
            state.update({"completed_at": time.time(), "transcription_url": url})
            atomic_write_json(state_path, state)
            progress("Qwen 云端词级听写完成", 0.78)
            return _parse_result(value, model=model)
        if status in _TERMINAL_FAILURES:
            state.pop("task_id", None)
            state["last_failure"] = output.get("message") or output.get("code") or body
            atomic_write_json(state_path, state)
            raise QwenCloudAsrError(
                f"Qwen 云端听写失败：{output.get('message') or output.get('code') or body}"
            )
        cancellable_wait(poll_seconds)
    raise QwenCloudAsrError(f"Qwen 云端听写等待超过 {total_timeout} 秒；任务 {task_id} 已保留，可重试继续查询")
