"""分页：偏移量只能来自 :func:`textkit.slices.window_start`。

在 ``page`` 里另写一遍偏移量算术是这个库出现过错位的直接原因：两处一旦不同步，
调用方看到的就是"第 2 页从第 2 条开始"。
"""

from __future__ import annotations

from textkit.slices import window_start


def page(values: list[int], number: int, size: int) -> list[int]:
    """第 ``number`` 页（从 0 开始）的元素。"""
    start = window_start(number, size)
    return values[start : start + size]
