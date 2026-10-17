"""图状态（02 §15.1）。

这是全项目**最容易被改坏**的类型：图节点、诊断、回归、报告都读它。
因此约定：冻结之后只允许新增带默认值的可选字段，不允许改已有字段的名字或
语义；需要改语义时先改 02 再用迁移脚本处理历史 run。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from codepilot.schemas.audit import PlannerToolAudit
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.diagnosis import Diagnosis
from codepilot.schemas.enums import RunStatus, StopReason, TaskMode
from codepilot.schemas.execution import ExecutionResult, TestBaseline
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.localization import LocalizationResult
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.progress import ProgressSnapshot
from codepilot.schemas.regression import RegressionResult
from codepilot.schemas.reproduction import ReproductionResult
from codepilot.schemas.retrieval import RetrievalResult
from codepilot.schemas.review import ReviewResult
from codepilot.schemas.usage import Usage
from codepilot.schemas.validation import ValidationResult

__all__ = ["Attempt", "RepairState"]


class Attempt(BaseModel):
    """一轮修复的完整记录。

    每轮**追加**到 ``RepairState.attempts``，不覆盖历史（02 §15.1）——
    否则无法回答"第 2 轮比第 1 轮好在哪"。
    """

    iteration: int
    patch_diff: str = ""
    patch_hash: str = ""
    validation: ValidationResult
    execution: ExecutionResult | None = None

    failure_signatures: list[str] = Field(default_factory=list)
    """本轮失败签名排序后的集合，用于重复失败检测。"""

    regression: RegressionResult | None = None
    review: ReviewResult | None = None
    diagnosis: Diagnosis | None = None
    progress: ProgressSnapshot | None = None


class RepairState(BaseModel):
    """一次修复运行的完整状态，也是 checkpointer 保存的对象。

    大字段（完整日志、完整检索结果）只存 artifact 路径，不放这里（02 §15.1）。
    """

    task_id: str
    run_id: str
    repository_id: str
    base_commit: str
    issue: str
    mode: TaskMode = TaskMode.USER

    issue_analysis: IssueAnalysis | None = None
    retrieval: RetrievalResult | None = None
    localization: LocalizationResult | None = None
    impact: ImpactAnalysis | None = None
    plan: RepairPlan | None = None
    reproduction: ReproductionResult | None = None
    baseline: TestBaseline | None = None
    execution: ExecutionResult | None = None

    attempts: list[Attempt] = Field(default_factory=list)
    review_repairs: int = Field(default=0, ge=0, le=1)
    current_patch: str = ""
    regression: RegressionResult | None = None
    review: ReviewResult | None = None

    budget: RepairBudget = Field(default_factory=RepairBudget)
    usage: Usage = Field(default_factory=Usage)
    usage_uncertain: bool = False
    test_runs: int = 0

    started_at: datetime
    status: RunStatus = RunStatus.RUNNING
    stop_reason: StopReason | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)
    report_artifacts: dict[str, str] = Field(default_factory=dict)

    plan_tools: PlannerToolAudit | None = None
    """Planner 的只读工具调用审计。``None`` = 没采集到，**不等于**"模型没调用工具"。"""

    error_class: str | None = None
    """终态归因大类，取值见 :class:`codepilot.errors.FailureClass`。"""

    error_origin: str | None = None
    """终态责任侧，取值见 :class:`codepilot.errors.ErrorOrigin`。

    带默认值的可选字段，历史 checkpoint 照旧可读（``CHECKPOINT_SCHEMA_VERSION``
    不用 +1，因为没有改任何已有字段的语义）。
    """

    plan_corrections: int = Field(default=0, ge=0)
    """Planner 语义/Schema 纠正重试的**已使用**次数，与服务商 HTTP 重试分开计。"""

    @property
    def iteration(self) -> int:
        """已完成的修复轮数。等价于 ``repair_patch`` 已执行次数。"""
        return len(self.attempts)

    @property
    def current_attempt(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None

    def patch_hashes(self) -> list[str]:
        """历史各轮的 patch hash，供 ``repeated_patch`` 判定使用。"""
        return [a.patch_hash for a in self.attempts]

    def signature_sets(self) -> list[frozenset[str]]:
        """历史各轮的失败签名集合，供振荡检测使用（02 §15.4 末段）。"""
        return [frozenset(a.failure_signatures) for a in self.attempts]
