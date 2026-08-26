from __future__ import annotations

import base64
import ctypes
import os
import secrets
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .artifacts import atomic_write_text


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04
PORTABLE_PREFIX = "substar-portable-aesgcm-v1:"
PORTABLE_AAD = b"substar.credentials.v2"


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _portable_key(key_path: Path, *, create: bool) -> bytes:
    if key_path.is_file():
        key = base64.b64decode(key_path.read_text(encoding="ascii").strip())
        if len(key) != 32:
            raise ValueError("便携凭据密钥长度无效")
        return key
    if not create:
        raise FileNotFoundError("便携凭据密钥不存在")
    key = secrets.token_bytes(32)
    atomic_write_text(key_path, base64.b64encode(key).decode("ascii"), encoding="ascii")
    return key


def protect_text(value: str, *, key_path: Path) -> str:
    """Encrypt text with the data-directory key so the whole data root is portable."""
    if not value:
        return ""
    key = _portable_key(key_path, create=True)
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(key).encrypt(nonce, value.encode("utf-8"), PORTABLE_AAD)
    return PORTABLE_PREFIX + base64.b64encode(nonce + encrypted).decode("ascii")


def _unprotect_legacy_dpapi(value: str) -> str:
    """Read the previous machine-bound envelope only for one-time migration."""
    if os.name != "nt":
        raise RuntimeError("旧 DPAPI 凭据只能在原 Windows 机器上迁移")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    source, source_buffer = _blob(base64.b64decode(value))
    result = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(result),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.pbData, result.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(result.pbData)
        del source_buffer


def unprotect_text(value: str, *, key_path: Path) -> str:
    if not value:
        return ""
    if not value.startswith(PORTABLE_PREFIX):
        return _unprotect_legacy_dpapi(value)
    encoded = base64.b64decode(value[len(PORTABLE_PREFIX):])
    if len(encoded) < 13:
        raise ValueError("便携凭据信封无效")
    key = _portable_key(key_path, create=False)
    return AESGCM(key).decrypt(encoded[:12], encoded[12:], PORTABLE_AAD).decode("utf-8")

