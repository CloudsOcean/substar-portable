from __future__ import annotations

from substar_core.chinese_script import convert_chinese_script


def test_taiwan_conversion_includes_regional_vocabulary() -> None:
    assert convert_chinese_script("鼠标软件网络", "traditional_tw") == "滑鼠軟體網路"


def test_hong_kong_conversion_includes_regional_vocabulary() -> None:
    assert convert_chinese_script("鼠标软件网络", "traditional_hk") == "鼠標軟件網絡"


def test_generic_simplified_and_traditional_routes_remain_available() -> None:
    assert convert_chinese_script("鼠标软件网络", "traditional") == "鼠標軟件網絡"
    assert convert_chinese_script("鼠標軟件網絡", "simplified") == "鼠标软件网络"
