"""进度判定（02 §15.4）。

``made_progress`` 的逻辑很短，但它是 ``no_progress`` 停止规则的唯一依据，
判错了会让系统在错误之间来回振荡直到烧完预算，所以单独成模块并被单测覆盖。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["ProgressSnapshot", "made_progress"]


class ProgressSnapshot(BaseModel):
    """一轮修复结束时的进度快照。"""

    target_failed: int
    """目标测试（复现测试或 Planner 选出的相关测试）中的失败数。"""

    regression_new_failures: int
    """相对 base 基线新增的失败数。"""

    signatures: frozenset[str] = Field(default_factory=frozenset)
    """本轮全部失败签名。用于判断"错误是否推进到了别处"。"""


def made_progress(prev: ProgressSnapshot, cur: ProgressSnapshot) -> bool:
    """当前轮相对上一轮是否取得进展。

    签名变化算进展，是因为"先报 ImportError，修完后报 AssertionError"通常
    意味着修复在推进。但这也可能是在两个错误之间来回振荡，因此单独调用方还
    必须检查签名集合是否曾在更早的轮次出现过（见 ``graph.progress``）。
    """
    if cur.target_failed < prev.target_failed:
        return True
    if cur.regression_new_failures < prev.regression_new_failures:
        return True
    return cur.target_failed == prev.target_failed and cur.signatures != prev.signatures
