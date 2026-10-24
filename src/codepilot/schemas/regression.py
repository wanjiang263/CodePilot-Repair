"""回归判定结果（02 §16）。

文档在 ``RepairState`` 中引用了 ``RegressionResult`` 但没有定义，这里补齐。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from codepilot.schemas.execution import ExecutionResult

__all__ = ["RegressionLevel", "RegressionMode", "RegressionResult"]


class RegressionLevel(StrEnum):
    """02 §16.1 的三级测试。"""

    TARGET = "L1_target"
    IMPACT = "L2_impact"
    FULL = "L3_full"


class RegressionMode(StrEnum):
    FULL = "full"
    SAMPLED = "sampled"
    """L3 预计耗时超出剩余时间预算时，改为 L2 + 固定 seed 抽样 20%。"""


class RegressionResult(BaseModel):
    """回归判定的核心是 ``new_failures``：

    只有"base 上 passed、patch 后 failed/error"才算回归。base 上本来就失败的
    测试不算（否则任何仓库都会永远报回归），flaky 测试也不算。
    """

    model_config = ConfigDict(extra="forbid")

    complete: bool = Field(default=False, strict=True)
    status_changes: dict[str, tuple[str, str]] = Field(default_factory=dict)
    baseline_commit: str | None = None
    baseline_hash: str | None = None
    status: str = "NOT RUN"
    executed_tests: list[str] = Field(default_factory=list)
    unexecuted_tests: list[str] = Field(default_factory=list)
    existing_failures: list[str] = Field(default_factory=list)
    executions: dict[str, ExecutionResult] = Field(default_factory=dict)
    retries: dict[str, list[ExecutionResult]] = Field(default_factory=dict)
    retry_notes: list[str] = Field(default_factory=list)
    seed: int | None = None
    failure_reason: str | None = None

    new_failures: list[str] = Field(default_factory=list)
    fixed_tests: list[str] = Field(default_factory=list)
    """base 上失败、patch 后通过的测试。只在报告里展示，不参与成功判定。"""

    flaky: set[str] = Field(default_factory=set)
    mode: RegressionMode = RegressionMode.FULL
    sampled_ratio: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    levels_run: list[RegressionLevel] = Field(default_factory=list)

    @property
    def has_regression(self) -> bool:
        return bool(self.new_failures)

    @property
    def regression_mode(self) -> str:
        return self.mode.value
