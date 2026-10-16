"""窗口与切片工具。"""

from __future__ import annotations


def first_n(values: list[int], count: int) -> list[int]:
    """前 ``count`` 个；不足时返回全部。"""
    return values[:count]


def window_start(page: int, size: int) -> int:
    """第 ``page`` 页（从 0 开始）的起始下标。"""
    return page * size


def tail(text: str, count: int) -> str:
    """结尾 ``count`` 个字符；``count`` 为 0 时返回空串。"""
    return text[len(text) - count - 1 :] if count else ""
