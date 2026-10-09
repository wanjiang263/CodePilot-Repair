"""修复计划（02 §10.4）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codepilot.schemas.enums import ChangeAction, RiskLevel

__all__ = ["PlannedChange", "RepairPlan"]


class PlannedChange(BaseModel):
    file: str
    symbol: str | None = None
    action: ChangeAction = ChangeAction.MODIFY
    description: str = ""


class RepairPlan(BaseModel):
    root_cause_hypothesis: str
    evidence: list[str] = Field(default_factory=list)
    changes: list[PlannedChange] = Field(default_factory=list)
    tests_to_run: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW

    alternatives: list[str] = Field(default_factory=list)
    """当前方案失败时下一步可以尝试的方向。

    ``revert_and_retry`` 策略会把这里的内容提示给 Planner（02 §15.6），
    所以它是控制流程的一部分，不是装饰性字段。
    """
