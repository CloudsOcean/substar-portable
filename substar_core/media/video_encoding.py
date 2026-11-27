"""Select and verify a video encoder on the current machine."""
from functools import lru_cache
from pathlib import Path
import os
import subprocess
import tempfile
import threading
import time

from substar_core.environment_doctor import _find_tool


_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_HARDWARE = {"nvenc": "NVIDIA NVENC", "amf": "AMD AMF", "qsv": "Intel QSV"}
_SOFTWARE = {"h264": "libx264", "hevc": "libx265"}


class EncodingError(RuntimeError):
    pass


class EncodingCancelled(Exception):
    pass


@lru_cache(maxsize=16)
def _encoders(tool, modified):
    result = subprocess.run(
        [tool, "-hide_banner", "-encoders"], capture_output=True,
        text=True, errors="replace", timeout=10, creationflags=_FLAGS,
    )
    return {line.split()[1] for line in result.stdout.splitlines()
            if line.startswith(" V") and len(line.split()) > 2}


@lru_cache(maxsize=48)
def _probe(tool, modified, encoder):
    result = subprocess.run(
        [tool, "-nostdin", "-hide_banner", "-v", "error", "-f", "lavfi",
         "-i", "color=black:s=640x360:r=25", "-pix_fmt", "yuv420p",
         "-frames:v", "4", "-c:v", encoder, "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=12, creationflags=_FLAGS,
    )
    return "" if result.returncode == 0 else next(
        (line.strip() for line in result.stderr.splitlines() if line.strip()), "硬件编码器初始化失败"
    )


def ffmpeg_tool():
    tool = _find_tool("ffmpeg")
    if not tool:
        raise EncodingError("未找到 FFmpeg")
    return tool


def _video_options(encoder, bitrate, quality, fast):
    args = ["-c:v", encoder, "-pix_fmt", "yuv420p"]
    if encoder.endswith("_nvenc"):
        args += ["-preset", "p2" if fast else "p5", "-rc", "vbr"]
        args += ["-b:v", bitrate] if bitrate else ["-cq", str(quality), "-b:v", "0"]
    elif encoder.endswith("_amf"):
        args += ["-quality", "speed" if fast else "quality"]
        args += ["-rc", "vbr_peak", "-b:v", bitrate] if bitrate else [
            "-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality)]
    elif encoder.endswith("_qsv"):
        args += ["-preset", "fast" if fast else "medium"]
        args += ["-b:v", bitrate] if bitrate else ["-global_quality", str(quality)]
    else:
        args += ["-preset", "veryfast" if fast else "fast"]
        args += ["-b:v", bitrate] if bitrate else ["-crf", str(quality)]
    return args


def run_ffmpeg(tool, args, output, duration, update=lambda progress: None, *, check_cancel=lambda: None):
    """Run FFmpeg with a deadline and bounded error reporting."""
    check_cancel()
    deadline = time.monotonic() + max(900, min(14400, duration * 8))
    with tempfile.TemporaryFile() as errors:
        command = [tool, "-nostdin", "-hide_banner", "-v", "error", "-y",
                   "-progress", "pipe:1", *args, str(output)]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=errors, creationflags=_FLAGS)
        def report():
            for line in process.stdout:
                key, _, value = line.strip().partition(b"=")
                if key == b"out_time_us" and value.lstrip(b"-").isdigit():
                    update(max(0, min(.99, int(value) / max(duration, .001) / 1_000_000)))

        reader = threading.Thread(target=report, daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                check_cancel()
                try:
                    process.wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() >= deadline:
                    raise EncodingError("FFmpeg 处理超时")
            check_cancel()
            reader.join()
            if process.returncode:
                errors.seek(max(0, os.fstat(errors.fileno()).st_size - 5000))
                raise EncodingError(errors.read().decode("utf-8", "replace").strip() or "FFmpeg 处理失败")
            update(1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join()
            process.stdout.close()


def encode_video(make_args, output, *, codec, duration, bitrate=None, quality=20, fast=False,
                 update=lambda progress, encoder, reason: None, check_cancel=lambda: None):
    failures = []
    check_cancel()
    tool = ffmpeg_tool()
    try:
        modified = Path(tool).stat().st_mtime_ns
        available = _encoders(tool, modified)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EncodingError(f"无法读取 FFmpeg 编码器列表：{exc}") from exc
    attempted_hardware = False
    for backend, label in _HARDWARE.items():
        encoder = f"{codec}_{backend}"
        if encoder not in available:
            continue
        check_cancel()
        attempted_hardware = True
        try:
            reason = _probe(tool, modified, encoder)
        except (OSError, subprocess.SubprocessError) as exc:
            reason = str(exc)
        if reason:
            failures.append(f"{label} · {reason}")
            continue
        update(0, label, "")
        try:
            run_ffmpeg(tool, make_args(_video_options(encoder, bitrate, quality, fast)), output,
                       duration, lambda progress: update(progress, label, ""), check_cancel=check_cancel)
            return label
        except EncodingError as exc:
            output.unlink(missing_ok=True)
            message = str(exc)
            if not any(word in message.lower() for word in (
                encoder, "opening encoder", "hardware device", "nvenc", "mfx", "amf", "cuda"
            )):
                raise
            failures.append(f"{label} · {message.splitlines()[0]}")
    encoder = _SOFTWARE[codec]
    reason = (failures[0] if failures else
              "硬件编码初始化失败" if attempted_hardware else "没有可用的硬件编码器")
    if encoder in available:
        label = f"CPU ({encoder})"
        update(0, label, reason)
        run_ffmpeg(tool, make_args(_video_options(encoder, bitrate, quality, fast)), output,
                   duration, lambda progress: update(progress, label, reason), check_cancel=check_cancel)
        return label
    raise EncodingError(f"没有可用的 {codec.upper()} 编码器。{reason}")
