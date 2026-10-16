"""把纯文本表格解析成行。

单元格清洗一律走 :func:`textkit.normalise.split_fields`；这里再切一遍就等于
第二套规则，而两套规则不会同时修好。
"""

from __future__ import annotations

from textkit.normalise import split_fields


def parse_row(line: str) -> list[str]:
    """一行 -> 去掉两端空白的单元格列表。"""
    return split_fields(line)


def parse_table(text: str) -> list[list[str]]:
    """整个文本块 -> 行列表；空行被跳过。"""
    return [parse_row(line) for line in text.splitlines() if line.strip()]
