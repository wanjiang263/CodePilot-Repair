"""预算检查（02 §15.3、G-04）。

六类预算全部从 **state 里已有的累计量**推导，而不是在本模块另存一份计数器：
``iterations`` 来自 ``attempts``、``test_runs`` 来自 ``state.test_runs``、
token/费用来自 ``state.usage``、墙钟来自 ``state.started_at``。

这样做的理由很具体：中断恢复时唯一可信的账本只有 checkpoint 里的 state。如果
预算计数器活在内存里，进程重启后它就归零了，恢复出来的 run 可以无限烧钱——
这是 G-04 明确禁止的行为（"跨进程恢复时不能重新清零累计预算"）。

``max_iterations`` 的口径（02 §15.3 注释："``repair_patch`` 最多执行次数"）：
**首轮 ``generate_patch`` 不计入迭代次数**。``attempts`` 有 N 条时，已执行的
``repair_patch`` 次数是 ``max(N - 1, 0)``，因此 ``max_iterations=5`` 允许
1 次生成 + 5 次修复 = 最多 6 条 Attempt。这一条有专门的 off-by-one 测试。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from codepilot.clock import utcnow
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import StopReason
from codepilot.schemas.state import RepairState

__all__ = [
    "ACTIVITY_CATEGORIES",
    "Activity",
    "BudgetConfigurationError",
    "BudgetEvaluation",
    "BudgetManager",
    "Category",
]

Clock = Callable[[], datetime]


class Category(StrEnum):
    """六类预算的执行身份。"""

    ITERATIONS = "iterations"
    TEST_RUNS = "test_runs"
    TOOL_CALLS = "tool_calls"
    TOKENS = "tokens"
    COST = "cost"
    TIME = "time"


class Activity(StrEnum):
    """节点准备做的事；不同活动受不同类别约束。"""

    #: 一次 LLM 调用（理解 / 定位 / 规划 / 诊断）。
    LLM = "llm"
    #: 带工具循环的代码生成（既烧 token，也受每轮工具调用上限约束）。
    CODEGEN = "codegen"
    #: 又一轮修复（受 ``max_iterations`` 约束的唯一活动）。
    REPAIR = "repair"
    #: Sandbox 测试执行（含复现验证与回归）。
    TEST = "test"


#: 判定优先级：先结构性上限（还能不能再修一轮），再外部副作用上限（测试次数），
#: 然后是单轮消耗（工具调用），最后才是累计消耗（token / 费用 / 墙钟）。
#: 固定顺序的意义在于 ``stop_reason`` 必须可复现：同一状态两次判定给出同一个
#: 原因，否则路径测试无法断言。
_PRIORITY: tuple[Category, ...] = (
    Category.ITERATIONS,
    Category.TEST_RUNS,
    Category.TOOL_CALLS,
    Category.TOKENS,
    Category.COST,
    Category.TIME,
)

_STOP_REASONS: dict[Category, StopReason] = {
    Category.ITERATIONS: StopReason.BUDGET_ITERATIONS,
    Category.TEST_RUNS: StopReason.BUDGET_TEST_RUNS,
    Category.TOOL_CALLS: StopReason.BUDGET_TOOL_CALLS,
    Category.TOKENS: StopReason.BUDGET_TOKENS,
    Category.COST: StopReason.BUDGET_COST,
    Category.TIME: StopReason.BUDGET_TIME,
}

#: 每类活动受哪些类别约束。回归/目标测试同样受墙钟约束：一个已经超时的 run
#: 不该再花 5 分钟跑全量。
ACTIVITY_CATEGORIES: dict[Activity, tuple[Category, ...]] = {
    Activity.LLM: (Category.TOKENS, Category.COST, Category.TIME),
    Activity.CODEGEN: (Category.TOKENS, Category.COST, Category.TIME, Category.TOOL_CALLS),
    Activity.REPAIR: _PRIORITY,
    Activity.TEST: (Category.TEST_RUNS, Category.COST, Category.TIME),
}


@dataclass(frozen=True, slots=True)
class BudgetEvaluation:
    """一次预算判定的完整结果。

    ``exhausted`` 是**全部**超限类别（按优先级），不是只有第一个：只报第一个
    会把"token 和墙钟同时超了"压成一个原因，报告里就看不出真实缺口了。
    """

    primary: StopReason | None
    exhausted: tuple[StopReason, ...]
    categories: tuple[Category, ...]
    usage: tuple[tuple[Category, float, float], ...]

    @property
    def is_exhausted(self) -> bool:
        return self.primary is not None

    def usage_for(self, category: Category) -> tuple[float, float]:
        for name, used, limit in self.usage:
            if name is category:
                return used, limit
        raise KeyError(category)

    def summary(self) -> dict[str, str]:
        """人类可读的 ``已用/上限``，用于事件与报告。``inf`` 表示该项未封顶。"""
        out: dict[str, str] = {}
        for category, used, limit in self.usage:
            out[category.value] = (
                f"{used:g}/unlimited" if limit == float("inf") else f"{used:g}/{limit:g}"
            )
        return out


def _limit_or_inf(value: float | None) -> float:
    return float("inf") if value is None else float(value)


class BudgetConfigurationError(ValueError):
    """预算上限为负或计数为负：这类配置会让第一次判定就"恰好超限"，静默停止。"""


def _validate(budget: RepairBudget, state: RepairState) -> None:
    limits: dict[str, float] = {
        "max_iterations": budget.max_iterations,
        "max_test_runs": budget.max_test_runs,
        "max_tool_calls_per_iteration": budget.max_tool_calls_per_iteration,
        "max_total_tokens": budget.max_total_tokens,
        "max_runtime_seconds": budget.max_runtime_seconds,
    }
    if budget.max_cost is not None:
        limits["max_cost"] = budget.max_cost
    negative_limits = [name for name, value in limits.items() if value < 0]
    if negative_limits:
        raise BudgetConfigurationError(f"预算上限不能为负：{negative_limits}")
    negative_usage = [
        name
        for name, value in {
            "test_runs": state.test_runs,
            "attempts": len(state.attempts),
            "input_tokens": state.usage.input_tokens,
            "output_tokens": state.usage.output_tokens,
        }.items()
        if value < 0
    ]
    if negative_usage:
        raise BudgetConfigurationError(f"累计用量不能为负：{negative_usage}")


class BudgetManager:
    """按 state 推导的六类预算判定器；本身无可变计数。"""

    def __init__(self, *, clock: Clock = utcnow) -> None:
        self._clock = clock

    # ------------------------------------------------------------------ 读数

    def elapsed_seconds(self, state: RepairState) -> float:
        """墙钟耗时。``started_at`` 缺失时区时按 UTC 处理，不抛给运行期。"""
        started = state.started_at
        now = self._clock()
        if started.tzinfo is None:
            started = started.replace(tzinfo=now.tzinfo)
        return max((now - started).total_seconds(), 0.0)

    def iterations_used(self, state: RepairState) -> int:
        """已执行的 ``repair_patch`` 次数 = Attempt 条数 - 1（首轮是 generate）。"""
        return max(len(state.attempts) - 1, 0)

    def tool_calls_this_iteration(self, state: RepairState) -> int:
        """当前这一轮的 tool 调用数。

        以"最后一条已执行完测试的 Attempt"为分界：``tool_calls`` 由生成节点整体
        覆盖，所以恢复或重跑同一轮不会把上一轮的工具数叠加进来。
        """
        return len(state.tool_calls)

    def readings(
        self, budget: RepairBudget, state: RepairState
    ) -> dict[Category, tuple[float, float]]:
        """每类预算的 ``(已用, 上限)``。上限 ``inf`` 表示未设定（不触发超限）。"""
        return {
            Category.ITERATIONS: (
                float(self.iterations_used(state)),
                float(budget.max_iterations),
            ),
            Category.TEST_RUNS: (float(state.test_runs), float(budget.max_test_runs)),
            Category.TOOL_CALLS: (
                float(self.tool_calls_this_iteration(state)),
                float(budget.max_tool_calls_per_iteration),
            ),
            Category.TOKENS: (
                float(state.usage.total_tokens),
                _limit_or_inf(budget.max_total_tokens),
            ),
            # 未设 max_cost 时费用不封顶（02 §15.3），但仍然继续累计与上报。
            Category.COST: (state.usage.cost, _limit_or_inf(budget.max_cost)),
            Category.TIME: (
                self.elapsed_seconds(state),
                _limit_or_inf(budget.max_runtime_seconds),
            ),
        }

    # ------------------------------------------------------------------ 判定

    def evaluate(self, state: RepairState, activity: Activity) -> BudgetEvaluation:
        """按固定优先级判定该活动是否已无预算。

        边界口径：**已用 >= 上限 即视为耗尽**。上限是"最多允许消耗这么多"，
        所以刚好花完时不再允许新的消耗，而不是允许第 limit+1 次。
        """
        budget = state.budget
        _validate(budget, state)
        readings = self.readings(budget, state)
        relevant = ACTIVITY_CATEGORIES[activity]
        ordered = [c for c in _PRIORITY if c in relevant]
        exhausted = tuple(_STOP_REASONS[c] for c in ordered if readings[c][0] >= readings[c][1])
        usage = tuple((c, readings[c][0], readings[c][1]) for c in ordered)
        return BudgetEvaluation(
            primary=exhausted[0] if exhausted else None,
            exhausted=exhausted,
            categories=tuple(ordered),
            usage=usage,
        )

    def check(self, state: RepairState, activity: Activity) -> StopReason | None:
        return self.evaluate(state, activity).primary

    def remaining_iterations(self, state: RepairState) -> int:
        used, limit = self.readings(state.budget, state)[Category.ITERATIONS]
        return max(int(limit - used), 0)
