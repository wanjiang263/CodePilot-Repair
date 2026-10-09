"""修复预算（02 §15.3）。"""

from __future__ import annotations

from pydantic import BaseModel

__all__ = ["DEFAULT_MAX_ITERATIONS", "RepairBudget"]

DEFAULT_MAX_ITERATIONS = 5


class RepairBudget(BaseModel):
    """单任务的全部预算上限。

    字段名与默认值逐项对照 02 §15.3，改动前请先改文档。
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
