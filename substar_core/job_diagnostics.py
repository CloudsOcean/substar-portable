from __future__ import annotations

import errno
import re
import subprocess
from collections.abc import Mapping
from urllib.parse import urlsplit

import requests


def safe_text(value, *, secret: str = "", limit: int = 1000) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ""
    text = str(value)
    if secret:
        text = text.replace(secret, "[已隐藏]")
    text = re.sub(r'(?i)https?://[^\s<>"\x27]+', "[链接已隐藏]", text)
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[^\s,;\"']+", "[凭据已隐藏]", text)
    text = re.sub(
        r'''(?i)\b(api[-_ ]?key|authorization|password|secret|(?:access[-_ ]?)?token|cookie)\b["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
        r"\1=[已隐藏]",
        text,
    )
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    return text.strip()[:limit]


def response_diagnostics(response, *, body=None, secret: str = "") -> dict:
    if body is None:
        try:
            body = response.json()
        except (AttributeError, ValueError):
            body = {}
    body = body if isinstance(body, Mapping) else {}
    headers = getattr(response, "headers", {}) or {}
    nested = body.get("error")
    error = nested if isinstance(nested, Mapping) else body
    return {
        key: value
        for key, value in {
            "http_status": response.status_code,
            "provider_code": safe_text(error.get("code") or body.get("code"), secret=secret, limit=120),
            "provider_type": safe_text(error.get("type"), secret=secret, limit=120),
            "provider_message": safe_text(
                error.get("message") or body.get("message") or nested, secret=secret,
            ),
            "request_id": safe_text(
                body.get("request_id") or error.get("request_id")
                or headers.get("x-request-id")
                or headers.get("x-dashscope-request-id"),
                secret=secret, limit=160,
            ),
        }.items()
        if value
    }


def request_failure(exc: requests.RequestException) -> dict:
    if isinstance(exc, requests.Timeout):
        return {"code": "request_timeout", "message": "请求超时，未收到完整响应"}
    if isinstance(exc, requests.ConnectionError):
        return {"code": "connection_failed", "message": "连接中断或无法连接到服务"}
    return {"code": "request_failed", "message": "请求未完成，未收到可用响应"}


def model_failure(base_url: str, details: Mapping) -> str:
    host = urlsplit(base_url).hostname or ""
    status, code = details.get("http_status"), details.get("provider_code")
    if host == "open.bigmodel.cn":
        message = {
            "1113": "智谱 GLM 账户欠费，请充值后继续",
            "1302": "智谱 GLM 请求达到速率上限，可降低并发后重试",
            "1305": "智谱 GLM 当前访问量过大，请稍后重试",
            "1308": "智谱 GLM 已达到周期用量上限，请查看限额重置时间",
            "1310": "智谱 GLM 已达到周期用量上限，请查看限额重置时间",
            "1309": "智谱 GLM 套餐已到期，请续订后重试",
            "1210": "智谱 GLM 请求参数有误",
            "1211": "智谱 GLM 模型不存在，请检查模型设置",
        }.get(code)
        if message:
            return message
    if host == "api.deepseek.com":
        message = {
            401: "DeepSeek 认证失败，请检查 API 密钥",
            402: "DeepSeek 账户余额不足，请充值后继续",
            429: "DeepSeek 请求达到速率或并发上限",
            503: "DeepSeek 服务繁忙，请稍后重试",
        }.get(status)
        if message:
            return message
    suffix = code or (f"HTTP {status}" if status else "")
    return f"模型服务返回错误 · {suffix}" if suffix else "模型服务返回错误"


def local_failure(exc: BaseException) -> dict:
    if isinstance(exc, subprocess.CalledProcessError):
        return {
            "code": "media_command_failed", "message": "本地媒体处理失败",
            "media_exit_code": exc.returncode,
        }
    if isinstance(exc, subprocess.TimeoutExpired):
        return {"code": "media_command_timeout", "message": "本地媒体处理超时"}
    if isinstance(exc, OSError):
        code = errno.errorcode.get(exc.errno)
        message = {
            "ENOSPC": "磁盘空间不足，请释放项目所在磁盘的空间后重试",
            "EACCES": "无法访问所需文件，请检查文件权限或是否被占用",
            "EPERM": "文件操作被拒绝，请检查权限",
            "ENOENT": "找不到所需文件，请检查素材或项目文件",
        }.get(code)
        if getattr(exc, "winerror", None) in {32, 33}:
            return {"code": "file_in_use", "message": "文件正被其他程序占用，请关闭占用程序后重试"}
        if message:
            return {"code": code, "message": message}
    return {"code": type(exc).__name__, "message": "Substar 处理时发生内部错误"}
