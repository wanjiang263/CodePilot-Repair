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
    planned_new_tests: list[str] = Field(default_factory=list)
    """模型**打算新增**的测试（文件 + 可选 NodeID），不是可执行目标。

    分开的理由不是审美：``tests_to_run`` 会被送进 Sandbox 真的执行，而 GLM 在首次
    真实实验里往那里写了 ``planned:tests/test_ledger.py::test_new`` ——一个还没写出来
    的测试。旧校验器把 ``planned:`` 当成"待创建文件"去 ``must_exist=False`` 解析，
    撞上已存在的 ``tests/test_ledger.py`` 就抛 ``PathAccessError("already_exists")``，
    一次本可纠正的字段写法错误于是变成整次运行失败。

    带默认值的可选字段：历史 run 的计划与录制回放照样能解析。新增测试是否被允许写进
    补丁由 :class:`~codepilot.tools.base.WorkspacePathPolicy` 的可信配置
    （``allow_new_tests``，DA-009）决定，**不**由这个字段决定；它只表达意图。
    """

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
        if len(self.planned_new_tests) != len(set(self.planned_new_tests)):
            raise ValueError("duplicate planned new test")
        return self
