"""修复预算（02 §15.3）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["DEFAULT_MAX_ITERATIONS", "ClosureAdmission", "RepairBudget"]

DEFAULT_MAX_ITERATIONS = 5


class ClosureAdmission(BaseModel):
    """一次编码派发前对"测试额度够不够走完闭环"的判定记录（DA-024）。

    存在的理由：闭环准入是一个**算式**，不是一个布尔。只报告"额度不足"，事后无法
    复核是哪一项要的额度、当时还剩多少、以及编码阶段被允许花掉几轮之外的额度；
    而这些数字恰恰是决定"该提高 ``max_test_runs`` 还是该收短回归范围"的唯一依据。

    它是**读数**而不是记账：这些数只描述剩余测试额度如何分配，从不写入任何用量账本，
    因此重放或恢复同一次判定不会多消耗一次测试执行。
    """

    feasible: bool
    runs_spent: int = Field(ge=0)
    """派发时已经计费的全部测试执行次数（基线两次、复现验证、历史各轮与工具层 ``run_tests``）。"""

    allowance: int = Field(ge=0)
    """``max_test_runs`` 的硬上限读数。"""

    required: int = Field(ge=0)
    """这一版候选补丁**最少**要几次测试执行才能走完闭环（校验 0 次 + 目标 + 回归层级）。"""

    retry_reserve: int = Field(ge=0)
    """回归里"新失败复跑两次"这一档预留的额度；从编码阶段的可用份额里扣，不参与准入判定。"""

    target_runs: int = Field(default=0, ge=0)
    impact_runs: int = Field(default=0, ge=0)
    full_runs: int = Field(default=0, ge=0)
    coder_share: int = Field(default=0, ge=0)
    """编码阶段本轮**最多**可以自己跑的测试次数 = ``剩余 - required - retry_reserve``（不为负）。"""

    trigger: str = ""
    """这一轮修复是被谁要求的：``review`` / ``diagnosis`` / ``validation`` / ``generation``。"""

    detail: str = ""
    """同一判定的人类可读算式，必须与事件流和报告里的是同一句话。"""

    @property
    def remaining(self) -> int:
        """判定时还剩几次测试执行可用。"""
        return max(self.allowance - self.runs_spent, 0)

    def summary(self) -> dict[str, str]:
        """事件流里的读数（字符串，便于直接与 ``summary`` 字段对齐）。"""
        return {
            "feasible": str(self.feasible),
            "runs_spent": str(self.runs_spent),
            "allowance": str(self.allowance),
            "required": str(self.required),
            "target": str(self.target_runs),
            "impact": str(self.impact_runs),
            "full": str(self.full_runs),
            "retry_reserve": str(self.retry_reserve),
            "coder_share": str(self.coder_share),
            "trigger": self.trigger,
        }


class RepairBudget(BaseModel):
    """单任务的全部预算上限。

    字段名与默认值逐项对照 02 §15.3，改动前请先改文档。唯一的例外是
    ``max_review_calls``：它是 Phase 11 为"局部预算独立计数"新增的专属额度，
    口径写在 docs/notes/phase11-closure-budget.md。
    """

    max_iterations: int = DEFAULT_MAX_ITERATIONS
    """``repair_patch`` 最多执行次数。"""

    max_test_runs: int = 12
    """含目标测试、回归、复现验证。

    v1.0 是 8，v1.1 提高到 12，因为一次完整流程至少包括：复现验证 1 次、
    目标测试每轮 1 次、回归 1–2 次。Phase 10 会根据实际分布再调整。
    """

    max_files_changed: int = 5
    max_lines_changed: int = 150
    max_tool_calls_per_iteration: int = 30
    max_review_calls: int = 2
    """``review`` 节点自己的专属额度，与编码阶段的工具额度互不相干。

    独立计数是修正"Coder 局部额度耗尽把审查一起拖死"的那一半：编码阶段烧光
    ``max_tool_calls_per_iteration`` 之后，审查阶段仍然必须有额度可花。默认 2 =
    初审 + Phase 9 高危修复政策允许的那一次复审，多出来的第三次审查调用才是这里要拦的。
    """

    max_runtime_seconds: int = 1800
    max_total_tokens: int = 1_000_000

    max_cost: float | None = None
    """单任务费用上限，单位为 ``budget_currency``。

    **没有固定默认值**：不同模型单价差别很大，同一个数字换个模型含义就完全
    不同。未设定时仍受 ``max_total_tokens`` 与 ``max_runtime_seconds`` 约束，
    不会无限运行。用户在配置文件或 ``--max-cost`` 中自行设定，
    benchmark 运行前会打印推算的费用上界供确认。
    """

    budget_currency: str = "CNY"

    def is_cost_limited(self) -> bool:
        return self.max_cost is not None
