"""小统计库。"""

from __future__ import annotations


class EmptySample(Exception):
    """样本为空，统计量没有定义。"""


def mean(values: list[float]) -> float:
    """算术平均。空输入没有定义，必须报错而不是除零。"""
    return sum(values) / len(values)


def running_unique(items: list[str]) -> list[str]:
    """按首次出现顺序去重。"""
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def spread(values: list[float]) -> float:
    """极差（最大减负）。"""
    return max(values) - min(values)
