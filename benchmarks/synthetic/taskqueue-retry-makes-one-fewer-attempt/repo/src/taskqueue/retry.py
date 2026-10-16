"""重试策略。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


class Exhausted(Exception):
    """所有尝试都失败了。"""


@dataclass(frozen=True)
class RetryPolicy:
    """最多尝试 ``attempts`` 次（含第一次）。"""

    attempts: int = 3

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts 至少为 1")


def run(function: Callable[[], str], policy: RetryPolicy) -> str:
    """执行 ``function``，失败时按策略重试。"""
    last: Exception | None = None
    for _ in range(policy.attempts - 1):
        try:
            return function()
        except Exception as exc:
            last = exc
    raise Exhausted(f"all {policy.attempts} attempts failed") from last
