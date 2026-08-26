from __future__ import annotations

import sys
from functools import lru_cache
from typing import Any


LCMAP_SIMPLIFIED_CHINESE = 0x02000000
LCMAP_TRADITIONAL_CHINESE = 0x04000000


_OPENCC_CONFIGS = {
    "simplified": "t2s",
    "traditional": "s2t",
    "traditional_tw": "s2twp",
    "traditional_hk": "s2hk",
}


@lru_cache(maxsize=len(_OPENCC_CONFIGS))
def _opencc_converter(target: str) -> Any | None:
    """Return one shared OpenCC converter per projection target."""
    if target not in _OPENCC_CONFIGS:
        raise ValueError("unsupported Chinese script target")
    try:
        from opencc import OpenCC
    except ImportError:
        return None
    return OpenCC(_OPENCC_CONFIGS[target])


def convert_chinese_script(text: str, target: str) -> str:
    """Convert Chinese script and regional vocabulary with cached OpenCC."""
    if target == "original" or not text:
        return text
    converter = _opencc_converter(target)
    if converter is not None:
        return converter.convert(text)
    # Keep the two historical character-only routes usable in a development
    # checkout before optional dependencies are installed. Regional routes
    # intentionally fail instead of pretending Windows NLS localizes phrases.
    if target not in {"simplified", "traditional"}:
        raise RuntimeError("地区繁体转换需要安装 OpenCC")
    if not text or sys.platform != "win32":
        return text
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.LCMapStringEx
    function.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_long,
    ]
    function.restype = ctypes.c_int
    flag = (
        LCMAP_SIMPLIFIED_CHINESE
        if target == "simplified"
        else LCMAP_TRADITIONAL_CHINESE
    )
    locale = "zh-CN" if target == "simplified" else "zh-TW"
    required = function(locale, flag, text, -1, None, 0, None, None, 0)
    if required <= 0:
        raise OSError(ctypes.get_last_error(), "LCMapStringEx failed")
    output = ctypes.create_unicode_buffer(required)
    written = function(
        locale, flag, text, -1, output, required, None, None, 0
    )
    if written <= 0:
        raise OSError(ctypes.get_last_error(), "LCMapStringEx failed")
    return output.value
