from __future__ import annotations

import base64
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .artifacts import atomic_write_text


PORTABLE_PREFIX = "substar-portable-aesgcm-v1:"
PORTABLE_AAD = b"substar.credentials.v2"


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


def unprotect_text(value: str, *, key_path: Path) -> str:
    if not value:
        return ""
    if not value.startswith(PORTABLE_PREFIX):
        raise ValueError("不支持的凭据信封格式")
    encoded = base64.b64decode(value[len(PORTABLE_PREFIX):])
    if len(encoded) < 13:
        raise ValueError("便携凭据信封无效")
    key = _portable_key(key_path, create=False)
    return AESGCM(key).decrypt(encoded[:12], encoded[12:], PORTABLE_AAD).decode("utf-8")

