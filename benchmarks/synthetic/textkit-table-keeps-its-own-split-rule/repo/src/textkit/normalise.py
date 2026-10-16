"""文本清洗。"""

from __future__ import annotations


def split_fields(line: str, separator: str = ",", *, max_split: int = -1) -> list[str]:
    """按分隔符切列并去掉每列两端空白。空行返回空列表。"""
    if not line.strip():
        return []
    return line.split(separator, max_split)


def collapse_whitespace(text: str) -> str:
    """把连续空白压成单个空格，并去掉首尾空白。"""
    return " ".join(text.split())


def title(value: str) -> str:
    """首字母大写，其余原样。"""
    return value[:1].upper() + value[1:] if value else value
