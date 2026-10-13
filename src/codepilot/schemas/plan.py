"""修复计划（02 §10.4）。"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, Field, field_validator, model_validator

from codepilot.schemas.enums import ChangeAction, RiskLevel

__all__ = ["PlannedChange", "RepairPlan"]


class PlannedChange(BaseModel):
    file: str = Field(min_length=1)
    symbol: str | None = None
    action: ChangeAction = ChangeAction.MODIFY
    description: str = Field(min_length=1)

    @field_validator("file")
    @classmethod
    def _repository_relative_file(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        windows = PureWindowsPath(value)
        posix = PurePosixPath(normalized)
        if windows.is_absolute() or windows.anchor or posix.is_absolute() or ".." in posix.parts:
            raise ValueError("planned change file must be repository-relative")
        if not normalized.strip() or normalized.endswith("/"):
            raise ValueError("planned change file must identify a file")
        return normalized


class RepairPlan(BaseModel):
    root_cause_hypothesis: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    changes: list[PlannedChange] = Field(default_factory=list)
    tests_to_run: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.LOW

    alternatives: list[str]
    """当前方案失败时下一步可以尝试的方向。

    ``revert_and_retry`` 策略会把这里的内容提示给 Planner（02 §15.6），
    所以它是控制流程的一部分，不是装饰性字段。
    """

    @model_validator(mode="after")
    def _unique_targets(self) -> RepairPlan:
        identities = [(change.file, change.symbol, change.action) for change in self.changes]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate planned change target")
        return self
