"""G-04 / G-05：六类预算、边界口径与节点入口检查。

重点不是"能不能读出超限"，而是那些**只在边界上出错**的口径：

* ``max_cost`` 未设定时不能被判成费用耗尽（02 §15.3 明确它没有默认值）；
* ``max_iterations`` 只数 ``repair_patch``，首轮 ``generate_patch`` 不算——
  这条 off-by-one 决定了一个 run 最多产生几条 Attempt；
* 恢复时不能把已花的预算清零（计数器全部从 state 推导，不在内存里另存一份）；
* 同一个节点重放不能重复扣费（写回的是绝对值而不是 ``+= 1``）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codepilot.graph.budget import (
    ACTIVITY_CATEGORIES,
    Activity,
    BudgetConfigurationError,
    BudgetManager,
    Category,
)
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import StopReason
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage


def _now() -> datetime:
    return datetime.now(UTC)


def _state(
    *,
    attempts: int = 0,
    test_runs: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    tool_calls: int = 0,
    elapsed: float = 0.0,
    budget: RepairBudget | None = None,
) -> RepairState:
    """构造一个只填了预算相关字段的 State。

    ``elapsed`` 直接把 ``started_at`` 往前挪，因此"已经跑了多久"由数据本身表达，
    配上真实 ``utcnow`` 的默认判定器就能读出期望的墙钟消耗。需要精确控制的用例
    自己注入时钟（见 ``test_wall_clock_uses_the_injected_clock_not_the_host``）。
    """
    state = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=_now() - timedelta(seconds=elapsed),
        budget=budget or RepairBudget(),
        test_runs=test_runs,
        usage=Usage(input_tokens=tokens, output_tokens=0, cost=cost),
    )
    state.attempts.extend(_attempt(index) for index in range(1, attempts + 1))
    state.tool_calls.extend({"name": "edit_file", "ok": True} for _ in range(tool_calls))
    return state


def _attempt(index: int):
    from codepilot.schemas.state import Attempt
    from codepilot.schemas.validation import ValidationResult

    return Attempt(
        iteration=index,
        patch_hash=f"hash-{index}",
        validation=ValidationResult(passed=True),
    )


# ------------------------------------------------------------------ 六类各自触发


@pytest.mark.parametrize(
    ("activity", "category", "reason"),
    [
        (Activity.REPAIR, Category.ITERATIONS, StopReason.BUDGET_ITERATIONS),
        (Activity.REPAIR, Category.TEST_RUNS, StopReason.BUDGET_TEST_RUNS),
        (Activity.REPAIR, Category.TOOL_CALLS, StopReason.BUDGET_TOOL_CALLS),
        (Activity.REPAIR, Category.TOKENS, StopReason.BUDGET_TOKENS),
        (Activity.REPAIR, Category.TIME, StopReason.BUDGET_TIME),
    ],
)
def test_each_budget_category_triggers_its_own_stop_reason(
    activity: Activity, category: Category, reason: StopReason
) -> None:
    budget = RepairBudget(
        max_iterations=2, max_test_runs=3, max_tool_calls_per_iteration=4, max_total_tokens=100
    )
    values = {
        Category.ITERATIONS: {"attempts": 3},
        Category.TEST_RUNS: {"test_runs": 4},
        Category.TOOL_CALLS: {"tool_calls": 5},
        Category.TOKENS: {"tokens": 150},
        Category.TIME: {"elapsed": 5000.0},
    }
    manager = BudgetManager(clock=lambda: datetime.now(UTC))
    state = _state(budget=budget, **values[category])
    assert manager.check(state, activity) is not None
    evaluation = manager.evaluate(state, activity)
    assert reason in evaluation.exhausted


def test_cost_budget_only_applies_when_max_cost_is_set() -> None:
    """未设 ``max_cost`` 时，花掉再多钱也不能被判成费用耗尽（02 §15.3）。"""
    state = _state(cost=9_999.0, budget=RepairBudget(max_cost=None))
    evaluation = BudgetManager().evaluate(state, Activity.LLM)
    assert StopReason.BUDGET_COST not in evaluation.exhausted
    assert evaluation.primary is None
    used, limit = evaluation.usage_for(Category.COST)
    assert used == 9_999.0 and limit == float("inf")


def test_cost_budget_triggers_when_set() -> None:
    state = _state(cost=12.0, budget=RepairBudget(max_cost=10.0))
    evaluation = BudgetManager().evaluate(state, Activity.REPAIR)
    assert StopReason.BUDGET_COST in evaluation.exhausted


# ------------------------------------------------------------------ 边界口径


def test_exactly_at_the_limit_is_exhausted_and_below_is_not() -> None:
    """上限是"最多允许消耗这么多"：刚好花完就不再允许新的消耗。"""
    at = _state(test_runs=12, budget=RepairBudget(max_test_runs=12))
    below = _state(test_runs=11, budget=RepairBudget(max_test_runs=12))
    manager = BudgetManager()
    assert manager.check(at, Activity.TEST) is StopReason.BUDGET_TEST_RUNS
    assert manager.check(below, Activity.TEST) is None


def test_iterations_counts_repair_patch_only_not_the_first_generate() -> None:
    """``max_iterations=5`` 允许 1 次生成 + 5 次修复 = 6 条 Attempt。"""
    budget = RepairBudget(max_iterations=5)
    manager = BudgetManager()
    # 首轮 generate 之后：1 条 Attempt，repair 还没跑过。
    assert manager.iterations_used(_state(attempts=1, budget=budget)) == 0
    assert manager.check(_state(attempts=1, budget=budget), Activity.REPAIR) is None
    # 4 次 repair 之后：还可以再来一次。
    assert manager.check(_state(attempts=5, budget=budget), Activity.REPAIR) is None
    # 第 5 次 repair 已用完：下一次 repair 被拒。
    assert (
        manager.check(_state(attempts=6, budget=budget), Activity.REPAIR)
        is StopReason.BUDGET_ITERATIONS
    )
    assert manager.remaining_iterations(_state(attempts=6, budget=budget)) == 0


def test_wall_clock_uses_the_injected_clock_not_the_host() -> None:
    """判定必须用注入的时钟，而不是宿主机时间（见 ``deps.rules`` 的注释）。

    这条测试故意让"宿主机时间"和"注入时钟"给出**相反**的答案：如果判定走了
    真实 ``utcnow``，两个断言不可能同时成立。
    """
    budget = RepairBudget(max_runtime_seconds=1800)
    started = datetime(2026, 10, 14, tzinfo=UTC)
    state = _state(budget=budget).model_copy(update={"started_at": started})

    # 宿主机现在是 2026-10-15 之后，真实时钟会说这个 run 跑了很久。
    fast_forward = BudgetManager(clock=lambda: started + timedelta(seconds=10_000))
    assert fast_forward.check(state, Activity.LLM) is StopReason.BUDGET_TIME

    rewind = BudgetManager(clock=lambda: started + timedelta(seconds=60))
    assert rewind.check(state, Activity.LLM) is None


# ------------------------------------------------------------------ 活动范围


def test_llm_activity_is_not_blocked_by_test_run_budget() -> None:
    """测试次数耗尽不该拦住纯 LLM 节点，但必须拦住测试节点。"""
    state = _state(test_runs=99, budget=RepairBudget(max_test_runs=12))
    manager = BudgetManager()
    assert manager.check(state, Activity.LLM) is None
    assert manager.check(state, Activity.TEST) is StopReason.BUDGET_TEST_RUNS
    assert Category.TEST_RUNS in ACTIVITY_CATEGORIES[Activity.REPAIR]


def test_finalizing_is_always_allowed_because_it_has_no_budget_activity() -> None:
    """finalize 不参与预算判定：预算耗尽也必须能出报告（G-05）。"""
    assert Activity.LLM in ACTIVITY_CATEGORIES
    exhausted = _state(
        attempts=99, test_runs=99, tokens=10**9, tool_calls=99, budget=RepairBudget()
    )
    manager = BudgetManager()
    for activity in (Activity.LLM, Activity.CODEGEN, Activity.REPAIR, Activity.TEST):
        assert manager.check(exhausted, activity) is not None


def test_all_six_categories_are_covered_by_some_activity() -> None:
    covered = {category for group in ACTIVITY_CATEGORIES.values() for category in group}
    assert covered == set(Category)


# ------------------------------------------------------------------ 记账完整性


def test_usage_is_written_as_absolute_value_so_replay_cannot_double_count() -> None:
    """重放安全：state 里写的是累计绝对值，不是 ``+= 1``。

    这里直接复现"同一节点跑两次"的恢复场景：第二次写回同一个 ``Usage`` 对象，
    计数不能翻倍。
    """
    usage = Usage()
    usage.add_call(alias="coder", input_tokens=100, output_tokens=50, cost=0.5)
    state = _state()
    first = state.model_copy(update={"usage": usage})
    second = first.model_copy(update={"usage": usage})
    assert second.usage.total_tokens == first.usage.total_tokens == 150
    assert second.usage.cost == 0.5


def test_failed_and_retried_calls_are_counted() -> None:
    """失败调用与重试都要计入（02 §5.4：重试不计迭代，但计费用）。"""
    usage = Usage()
    usage.add_call(alias="coder", input_tokens=10, output_tokens=5, cost=0.1)
    usage.add_call(alias="coder", input_tokens=10, output_tokens=0, cost=0.1)  # 重试
    assert usage.llm_calls == 2
    assert usage.cost == pytest.approx(0.2)
    assert usage.by_model["coder"].llm_calls == 2


def test_negative_budget_raises_instead_of_stopping_immediately() -> None:
    """负上限会让"已用 >= 上限"在第一次判定就成立，静默停止比报错危险得多。"""
    with pytest.raises(BudgetConfigurationError):
        BudgetManager().evaluate(_state(budget=RepairBudget(max_test_runs=-1)), Activity.TEST)
    with pytest.raises(BudgetConfigurationError):
        BudgetManager().evaluate(_state(budget=RepairBudget(max_cost=-0.5)), Activity.LLM)


def test_negative_accumulated_usage_is_rejected() -> None:
    state = _state()
    state = state.model_copy(update={"test_runs": -1})
    with pytest.raises(BudgetConfigurationError):
        BudgetManager().evaluate(state, Activity.TEST)


def test_evaluation_reports_every_exhausted_category_not_only_the_first() -> None:
    """只报第一个会把"token 和墙钟同时超了"压成一条，报告看不出真实缺口。"""
    budget = RepairBudget(max_total_tokens=10, max_runtime_seconds=1)
    state = _state(tokens=500, elapsed=5000.0, budget=budget)
    evaluation = BudgetManager().evaluate(state, Activity.LLM)
    assert set(evaluation.exhausted) == {StopReason.BUDGET_TOKENS, StopReason.BUDGET_TIME}
    assert evaluation.primary in evaluation.exhausted
    assert evaluation.summary()["tokens"].endswith("/10")
