"""CSV 导出的列定义。

列顺序在 :data:`COLUMNS` 里唯一定义，:func:`budget.report.build_rows` 依赖它决定
每行有哪些字段。所以"给导出加一列"必须同时改这两个文件——少改一个，导出的表头
与数据就会错位，而 :func:`render_row` 会按下标取到一个根本不存在的键。
"""

from __future__ import annotations

COLUMNS = ("category", "note", "amount")


def headers() -> tuple[str, ...]:
    """导出文件的列名，顺序即写入顺序。"""
    return COLUMNS


def render_row(values: dict[str, object]) -> str:
    """按列顺序把一条记录拼成一行 CSV。"""
    return ",".join(str(values[column]) for column in COLUMNS)
