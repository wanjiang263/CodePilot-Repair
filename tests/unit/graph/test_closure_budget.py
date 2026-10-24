"""Phase 11 §三 + Phase 12 §二：预算分层、闭环预留、专属额度与闭环准入的判定口径。

这里要守住的不是"能不能算出超限"，而是五句话：

* **局部额度不等于全局终止** —— ``tool_calls`` 封顶只拦住编码，审查照旧有自己的额度；
* **预留是天花板，不是账本** —— 预留从不写入 ``state.usage``，所以不可能重复计费；
  它只把消耗型活动的可用上限压低；
* **未知费用不得记成零** —— 估算不出来时，设了硬费用上限的运行必须在派发前就被拦住；
* **预留只能收紧，不能放松硬上限**；
* **测试次数按"还要跨几次容器"计价**（DA-024）—— 剩余撑不起闭环时不启动那一轮，
  而不是让编码阶段把最后的额度借去自己跑测试。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codepilot.graph.budget import (
    CLOSURE_FLAKY_RETRY_RESERVE,
    CLOSURE_PATCH_VALIDATION_RUNS,
    CLOSURE_REGRESSION_LEVEL_RUNS,
    CLOSURE_TARGET_TEST_RUNS,
    REVIEW_CONTEXT_TOKENS,
    REVIEW_OUTPUT_TOKENS,
    REVIEW_TIME_RESERVE_SECONDS,
    Activity,
    BudgetConfigurationError,
    BudgetManager,
    Category,
    ClosureReservation,
    ClosureTestPlan,
    outstanding_in_budget_currency,
)
from codepilot.graph.stop_rules import StopRules
from codepilot.llm.config import KNOWN_ROLES, LLMConfig
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus, StopReason
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage

PER_CALL = REVIEW_CONTEXT_TOKENS + REVIEW_OUTPUT_TOKENS


def _state(
    *,
    budget: RepairBudget | None = None,
    tokens: int = 0,
    cost: float = 0.0,
    llm_calls: int = 1,
    cost_unknown: int = 0,
    tool_calls: int = 0,
    review_calls: int = 0,
    attempts: int = 0,
    review_repairs: int = 0,
) -> RepairState:
    from codepilot.schemas.state import Attempt
    from codepilot.schemas.validation import ValidationResult

    usage = Usage(input_tokens=tokens, output_tokens=0, cost=cost, llm_calls=llm_calls)
    usage.cost_unknown_calls = cost_unknown
    state = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime.now(UTC) - timedelta(seconds=1),
        budget=budget or RepairBudget(),
        usage=usage,
        review_calls=review_calls,
        review_repairs=review_repairs,
    )
    state.tool_calls.extend({"name": "edit_file", "ok": True} for _ in range(tool_calls))
    if attempts:
        state.attempts.extend(
            Attempt(
                iteration=index, patch_hash=f"h{index}", validation=ValidationResult(passed=True)
            )
            for index in range(1, attempts + 1)
        )
    return state


def _reserve(
    state: RepairState,
    *,
    reviewer: bool = True,
    pending: int = 2,
    config=None,
    simulated: bool = False,
):
    return BudgetManager().closure_reservation(
        state,
        reviewer_available=reviewer,
        pending_review_calls=pending,
        reviewer_config=config,
        simulated_provider=simulated,
    )


def test_a_simulated_provider_with_an_empty_ledger_is_known_zero_not_unknown() -> None:
    """预留作用域扩到 ``Activity.LLM`` 之后必须守住的边界。

    第一个 LLM 节点（``understand_issue``）时账本必然是空的。真实付费 provider 没有
    价格依据时要 fail closed；但脚本化 / FakeLLM 批次**没有货币费用**，把"还没调用过"
    读成"估不出来"会让每一个模拟运行在开局就被拦住（实测形态：
    ``SMOKE_COMPLETED_NO_PATCH``，¥1 硬上限、0 花费）。
    """
    budget = RepairBudget(max_cost=1.0)
    state = _state(budget=budget, tokens=0, cost=0.0, llm_calls=0)
    simulated = _reserve(state, simulated=True)
    assert simulated.estimated is True
    assert simulated.cost == 0.0
    assert simulated.basis == "simulated-zero-cost"
    assert simulated.tokens > 0, "token 维度照常预留：模拟批次也烧上下文"
    assert BudgetManager().check(state, Activity.LLM, reservation=simulated) is None

    # 同一个现场换成真付费 provider：没有任何价格依据，必须继续 fail closed。
    real = _reserve(state, simulated=False)
    assert real.estimated is False and real.cost == float("inf")
    assert BudgetManager().check(state, Activity.LLM, reservation=real) is StopReason.BUDGET_COST

    # 未知费用计数存在时，"模拟"也不许把费用读成 0（下界就是下界）。
    unknown = _state(budget=budget, tokens=0, cost=0.0, llm_calls=0, cost_unknown=1)
    assert _reserve(unknown, simulated=True).estimated is False


# ------------------------------------------------------------------ 预留的量


def test_reservation_is_zero_when_no_reviewer_is_wired() -> None:
    """没有 Reviewer 时审查会如实记成未执行，不该为它空占额度。"""
    reserve = _reserve(_state(), reviewer=False)
    assert reserve.review_calls == 0
    assert reserve.tokens == 0
    assert reserve.estimated is True


def test_reservation_covers_the_review_and_the_one_allowed_re_review() -> None:
    """初审 1 次；Phase 9 的高危修复政策允许再复审时预留 2 次（次数由状态机给出）。"""
    once = _reserve(_state(), pending=1)
    twice = _reserve(_state(), pending=2)
    assert once.review_calls == 1 and once.tokens == PER_CALL
    assert twice.review_calls == 2 and twice.tokens == 2 * PER_CALL


def test_reservation_is_zero_when_no_review_is_pending() -> None:
    """§三.6：审查已经完成或不再可能执行时，预留必须归零，不得永久占用预算。

    ``pending_review_calls`` 由状态机算出（:func:`pending_review_calls`），这里验证
    预留对它的响应：0 次欠账 = 0 token、0 费用、``estimated=True``（是"算得出 0"，
    不是"估不出来"），而且**不**放松任何硬上限。
    """
    state = _state(budget=RepairBudget(max_total_tokens=100_000, max_cost=10.0), tokens=10_000)
    reserve = _reserve(state, pending=0)
    assert reserve.tokens == 0 and reserve.cost == 0.0 and reserve.review_calls == 0
    assert reserve.estimated is True and reserve.basis == "review-not-pending"
    evaluation = BudgetManager().evaluate(state, Activity.CODEGEN, reservation=reserve)
    assert not evaluation.reserved
    assert evaluation.usage_for(Category.COST) == (0.0, 10.0)
    assert evaluation.usage_for(Category.TOKENS) == (10_000.0, 100_000.0)


def test_reservation_takes_the_worse_of_the_reviewer_budget_and_observed_average() -> None:
    """预留取较坏者：预留不足正是本轮要修的缺陷，估算偏乐观就是没修。"""
    fat = _state(tokens=100_000, llm_calls=2)  # 平均一次 50_000
    reserve = _reserve(fat)
    assert reserve.tokens == 2 * 50_000
    assert reserve.tokens > 2 * PER_CALL


def test_reservation_estimates_cost_from_the_observed_unit_price() -> None:
    state = _state(tokens=10_000, cost=1.0, llm_calls=1)
    reserve = _reserve(state)
    assert reserve.estimated is True
    assert reserve.cost == pytest.approx(reserve.tokens * 1.0 / 10_000)


def test_unknown_cost_is_not_recorded_as_zero() -> None:
    """``cost_unknown_calls`` 存在时费用是下界，预留必须承认自己估算不出来。"""
    reserve = _reserve(_state(tokens=10_000, cost=1.0, cost_unknown=1))
    assert reserve.estimated is False
    assert reserve.cost == float("inf")


def test_observed_zero_cost_is_a_real_reading_not_an_estimation_gap() -> None:
    """负控制：0 token / 0 费用是**合法**读数（FakeLLM 与免费额度就这么记账）。

    把它当成"估不出来"会让每一个零成本运行在编码派发前就被误阻断 —— 首轮真实
    smoke 就是这样失败的，所以这条边界必须钉住。真正该 fail closed 的只有
    "有未知费用调用"或"一次调用都还没有"。
    """
    free = _state(budget=RepairBudget(max_cost=10.0), tokens=0, cost=0.0, llm_calls=3)
    reserve = _reserve(free)
    assert reserve.estimated is True
    assert reserve.cost == 0.0
    # 设了硬费用上限也照常派发：预留估得出值，只是这个值真的是 0。
    assert BudgetManager().check(free, Activity.CODEGEN, reservation=reserve) is None
    never = _state(llm_calls=0)
    assert _reserve(never).estimated is False


def test_reservation_is_idempotent_and_never_bills() -> None:
    """预留是天花板计算，不是记账：反复求值不改变账本，也不改变结果。"""
    state = _state(tokens=40_000, cost=0.4, llm_calls=4)
    before = state.usage.model_dump(mode="json")
    first = _reserve(state)
    second = _reserve(state)
    assert first == second
    BudgetManager().evaluate(state, Activity.CODEGEN, reservation=first)
    assert state.usage.model_dump(mode="json") == before


# ---------------------------------------------------- 预留的费用口径（Reviewer 单价）


def _config(
    *,
    reviewer_price: dict[str, object] | None,
    coder_price: dict[str, object] | None = None,
    exchange_rates: dict[str, float] | None = None,
    budget_currency: str = "CNY",
) -> LLMConfig:
    """Coder 与 Reviewer 用**不同别名、不同单价**的配置。

    两个别名是这条测试的全部意义：只用一个模型时"账本平均单价"恰好等于"审查单价"，
    预留算错也看不出来。这里让 coder 便宜、reviewer 贵，平均单价就会系统性低估预留。
    """
    coder = coder_price or {"currency": "CNY", "input_per_mtok": 1.0, "output_per_mtok": 1.0}
    roles = dict.fromkeys(KNOWN_ROLES, "cheap")
    roles["reviewer"] = "pricey"
    models: dict[str, dict[str, object]] = {
        "cheap": {"alias": "cheap", "provider": "p", "model": "coder-model", "price": coder},
        "pricey": {"alias": "pricey", "provider": "p", "model": "review-model"},
    }
    if reviewer_price is not None:
        models["pricey"]["price"] = reviewer_price
    return LLMConfig.model_validate(
        {
            "providers": {
                "p": {
                    "name": "p",
                    "base_url": "https://.invalid/v1",
                    "api_key_env": "TEST_KEY_ONLY",
                }
            },
            "models": models,
            "roles": roles,
            "exchange_rates": exchange_rates or {},
            "budget_currency": budget_currency,
        }
    )


#: Reviewer 声明的上下文与输出预算（与 budget.py 同源，这里显式重述算式以便断言）。
_IN, _OUT = REVIEW_CONTEXT_TOKENS, REVIEW_OUTPUT_TOKENS


def test_reservation_uses_the_reviewers_model_price_not_the_ledger_average() -> None:
    """§三.1：Coder 便宜、Reviewer 贵时，预留必须按 Reviewer 的单价。"""
    # 账本：100_000 token 花了 0.1 → 平均单价 1e-6/token（= 1 元/百万 token）。
    state = _state(tokens=100_000, cost=0.1, llm_calls=10)
    config = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 4.0, "output_per_mtok": 16.0}
    )
    reserve = _reserve(state, config=config)
    expected = 2 * (4.0 * _IN + 16.0 * _OUT) / 1_000_000
    assert reserve.estimated is True
    assert reserve.basis == "reviewer-model:pricey"
    assert reserve.cost == pytest.approx(expected)
    # 平均单价口径（0.056176）明显低于按审查模型估的值：预留取保守者。
    assert reserve.cost > 2 * PER_CALL * 1e-6


def test_cached_input_price_is_deliberately_not_credited() -> None:
    """§三.2：缓存价只说明"实际大概更便宜"，预留按最坏情况全额计输入价。"""
    flat = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 4.0, "output_per_mtok": 16.0}
    )
    cached = _config(
        reviewer_price={
            "currency": "CNY",
            "input_per_mtok": 4.0,
            "output_per_mtok": 16.0,
            "cached_input_per_mtok": 0.4,
        }
    )
    state = _state(tokens=50_000, cost=0.05, llm_calls=5)
    assert _reserve(state, config=cached).cost == pytest.approx(_reserve(state, config=flat).cost)


def test_input_and_output_prices_are_priced_separately() -> None:
    """§三.2：输入价与输出价各自生效——平均单价口径下这两个数字无法区分。"""
    state = _state(tokens=50_000, cost=0.05, llm_calls=5)
    cheap_out = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 2.0, "output_per_mtok": 1.0}
    )
    dear_out = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 2.0, "output_per_mtok": 8.0}
    )
    a = _reserve(state, config=cheap_out).cost
    b = _reserve(state, config=dear_out).cost
    assert a == pytest.approx(2 * (2.0 * _IN + 1.0 * _OUT) / 1_000_000)
    assert b == pytest.approx(2 * (2.0 * _IN + 8.0 * _OUT) / 1_000_000)
    assert b > a


def test_reservation_priced_by_the_reviewer_still_never_bills() -> None:
    """预留是天花板，不是账本：按单价估算同样不得写入 ``state.usage``。"""
    config = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 4.0, "output_per_mtok": 16.0}
    )
    state = _state(budget=RepairBudget(max_cost=10.0), tokens=50_000, cost=0.05, llm_calls=5)
    before = state.usage.model_dump(mode="json")
    reserve = _reserve(state, config=config)
    BudgetManager().evaluate(state, Activity.CODEGEN, reservation=reserve)
    BudgetManager().evaluate(state, Activity.REPAIR, reservation=reserve)
    assert state.usage.model_dump(mode="json") == before
    assert _reserve(state, config=config) == reserve


def test_observed_average_larger_than_the_review_context_is_priced_at_the_steeper_unit() -> None:
    """观测到的单次调用规模超过审查声明的预算时，超出部分按**较贵**的单价计。"""
    config = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 1.0, "output_per_mtok": 9.0}
    )
    state = _state(tokens=200_000, cost=0.2, llm_calls=2)  # 平均一次 100_000 token
    reserve = _reserve(state, config=config)
    extra_per_call = 100_000 - (_IN + _OUT)
    expected = 2 * ((1.0 * _IN + 9.0 * _OUT) / 1_000_000) + 2 * extra_per_call * 9.0 / 1_000_000
    assert reserve.tokens == 2 * 100_000
    assert reserve.cost == pytest.approx(expected)


def test_foreign_reviewer_price_needs_a_rate_before_it_is_trusted() -> None:
    """§三.3：币种与预算不一致又缺汇率时，宁可当作估不出来（缺汇率按 1.0 会低估七倍）。"""
    state = _state(budget=RepairBudget(max_cost=10.0), tokens=1_000, cost=1.0, cost_unknown=1)
    usd = {"currency": "USD", "input_per_mtok": 1.0, "output_per_mtok": 4.0}
    untrusted = _reserve(state, config=_config(reviewer_price=usd))
    assert untrusted.basis == "unknown"
    assert untrusted.estimated is False
    assert untrusted.cost == float("inf")
    blocked = BudgetManager().evaluate(state, Activity.CODEGEN, reservation=untrusted)
    assert blocked.primary is StopReason.BUDGET_COST


def test_foreign_reviewer_price_is_converted_when_a_rate_exists() -> None:
    state = _state(budget=RepairBudget(max_cost=100.0), tokens=50_000, cost=0.05, llm_calls=5)
    config = _config(
        reviewer_price={"currency": "USD", "input_per_mtok": 1.0, "output_per_mtok": 2.0},
        exchange_rates={"USD": 7.0},
    )
    reserve = _reserve(state, config=config)
    assert reserve.basis == "reviewer-model:pricey"
    assert reserve.cost == pytest.approx(7.0 * 2 * (1.0 * _IN + 2.0 * _OUT) / 1_000_000)


def test_reservation_follows_the_declared_budget_currency() -> None:
    """预算币种换成 USD 且单价就是 USD 时，无需汇率也可直接采信。"""
    budget = RepairBudget(max_cost=10.0, budget_currency="USD")
    state = _state(budget=budget, tokens=50_000, cost=0.05, llm_calls=5)
    config = _config(
        reviewer_price={"currency": "USD", "input_per_mtok": 1.0, "output_per_mtok": 2.0},
        budget_currency="CNY",
    )
    reserve = _reserve(state, config=config)
    assert reserve.basis == "reviewer-model:pricey"
    assert reserve.cost == pytest.approx(2 * (1.0 * _IN + 2.0 * _OUT) / 1_000_000)


def test_missing_reviewer_price_falls_back_to_the_ledger_unit() -> None:
    """单价缺失（模型没配 price）时退回实测单价，并在 basis 里说清是哪一口。"""
    state = _state(tokens=10_000, cost=1.0, llm_calls=1)
    reserve = _reserve(state, config=_config(reviewer_price=None))
    assert reserve.basis == "observed-unit"
    assert reserve.cost == pytest.approx(reserve.tokens * 1.0 / 10_000)
    assert _reserve(state, config=None) == reserve


def test_role_not_mapped_to_a_model_falls_back_instead_of_guessing() -> None:
    """roles 里没有 reviewer 时不猜模型：退回账本单价，而不是拿最便宜的那个顶上。"""
    config = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 4.0, "output_per_mtok": 4.0}
    )
    config = config.model_copy(
        update={"roles": {k: v for k, v in config.roles.items() if k != "reviewer"}}
    )
    state = _state(tokens=10_000, cost=1.0, llm_calls=1)
    assert _reserve(state, config=config).basis == "observed-unit"


def test_free_reviewer_model_is_a_real_zero_not_an_estimation_gap() -> None:
    """负控制：单价 0（免费额度）是合法读数，设了硬费用上限也照常派发。"""
    budget = RepairBudget(max_cost=10.0)
    state = _state(budget=budget, tokens=0, cost=0.0, llm_calls=0)
    config = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 0.0, "output_per_mtok": 0.0}
    )
    reserve = _reserve(state, config=config)
    assert reserve.estimated is True
    assert reserve.cost == 0.0
    assert BudgetManager().check(state, Activity.CODEGEN, reservation=reserve) is None


def test_expensive_reviewer_blocks_the_coding_dispatch_but_not_the_review() -> None:
    """§三.8：剩余费用不足以支撑按审查单价估的预留时，派发前阻断；审查照旧可用。"""
    budget = RepairBudget(max_cost=0.3)
    state = _state(budget=budget, tokens=50_000, cost=0.2, llm_calls=5)
    config = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 4.0, "output_per_mtok": 16.0}
    )
    manager = BudgetManager()
    reserve = _reserve(state, config=config)
    assert reserve.cost > 0.1  # 剩下的钱确实付不起两次审查
    assert manager.check(state, Activity.CODEGEN, reservation=reserve) is StopReason.BUDGET_COST
    assert manager.check(state, Activity.REVIEW) is None
    assert manager.coding_cost_ceiling(state, reserve) is not None
    assert manager.coding_cost_ceiling(state, reserve) <= budget.max_cost


def test_priced_reservation_is_identical_after_a_cross_process_resume() -> None:
    """恢复路径不重复计费也不放松预留：单价口径只依赖配置与 checkpoint 账本。"""
    budget = RepairBudget(max_total_tokens=1_000_000, max_cost=100.0)
    config = _config(
        reviewer_price={"currency": "CNY", "input_per_mtok": 4.0, "output_per_mtok": 16.0}
    )
    original = _state(budget=budget, tokens=40_000, cost=0.4, llm_calls=4)
    revived = _state(budget=budget)
    revived.usage.reset_to(original.usage)
    assert _reserve(revived, config=config) == _reserve(original, config=config)
    manager = BudgetManager()
    assert manager.coding_cost_ceiling(revived, _reserve(revived, config=config)) == (
        manager.coding_cost_ceiling(original, _reserve(original, config=config))
    )


# ------------------------------------------------------------------ 预留的作用域


def test_reservation_tightens_every_pre_review_spend_but_never_the_beneficiaries() -> None:
    """§三.2：预留削减**所有排在审查之前的模型调用**；验证与审查用真实硬上限。

    清单只有一份（``RESERVATION_BEARING_ACTIVITIES``）：LLM/CODEGEN/REPAIR 收紧，
    TEST/REVIEW 原样。TEST 与 REVIEW 是预留要保护的对象，把它们也收紧就会"为了保护
    闭环而把闭环跳过"，那是同一个 bug 的反方向。
    """
    budget = RepairBudget(max_total_tokens=100_000)
    state = _state(budget=budget, tokens=10_000)
    reserve = _reserve(state)
    manager = BudgetManager()

    for activity in (Activity.LLM, Activity.CODEGEN, Activity.REPAIR):
        spend = manager.evaluate(state, activity, reservation=reserve)
        assert spend.reserved, activity
        if Category.TOKENS in spend.categories:
            assert spend.usage_for(Category.TOKENS) == (10_000.0, 100_000 - reserve.tokens)

    for activity in (Activity.TEST, Activity.REVIEW):
        untouched = manager.evaluate(state, activity, reservation=reserve)
        assert not untouched.reserved, activity
        if Category.TOKENS in untouched.categories:
            assert untouched.usage_for(Category.TOKENS)[1] == 100_000
    # 收紧之后的派发判定仍然可复现：同一状态两次判定给出同一个停止原因。
    # （只比原因，不比墙钟读数 —— 那是真实时钟，本来就该往前走。）
    tight = _state(budget=RepairBudget(max_total_tokens=2 * reserve.tokens), tokens=reserve.tokens)
    first = manager.evaluate(tight, Activity.LLM, reservation=reserve)
    second = manager.evaluate(tight, Activity.LLM, reservation=reserve)
    assert first.primary is StopReason.BUDGET_TOKENS
    assert first.exhausted == second.exhausted
    assert first.usage[:2] == second.usage[:2]


def test_a_diagnose_that_spends_past_the_reservation_is_refused_before_the_call() -> None:
    """§三.2：Diagnose 连续烧钱烧到剩余额度撑不起预留时，请求根本不发出。

    ``Activity.LLM`` 现在也吃预留，所以这条不再是概念修正：上限钉在"已花 + 预留"
    之间 —— 不扣预留付得起、扣了预留付不起，阻断只能来自预留本身。
    """
    reserve = _reserve(_state())
    budget = RepairBudget(max_total_tokens=2 * reserve.tokens)
    manager = BudgetManager()
    spent = _state(budget=budget, tokens=reserve.tokens, llm_calls=2)
    assert manager.check(spent, Activity.LLM) is None, "不设预留时照常放行"
    assert manager.check(spent, Activity.LLM, reservation=reserve) is StopReason.BUDGET_TOKENS
    # 边界口径"已用 >= 上限即耗尽"：剩余恰好等于预留时不再允许新的消耗。
    assert (
        manager.check(
            _state(budget=budget, tokens=budget.max_total_tokens - reserve.tokens),
            Activity.LLM,
            reservation=reserve,
        )
        is StopReason.BUDGET_TOKENS
    )
    roomy = _state(budget=RepairBudget(max_total_tokens=3 * reserve.tokens), tokens=reserve.tokens)
    assert manager.check(roomy, Activity.LLM, reservation=reserve) is None


def test_the_reviewer_spends_the_reservation_it_was_given_and_bills_once() -> None:
    """§三.5：Reviewer 用的是那份预留，钱只记一次、预留只扣一次。

    两个方向都要成立：``Activity.REVIEW`` 看不到预留（受益方，硬上限原样），审查不会
    被自己那份预留拦下；审查真正落账之后账本里只多一份审查费用 —— 预留从不写入
    ``state.usage``，所以不存在"扣了预留又扣一次实际费用"。审查结论一旦绑定当前候选
    补丁，欠账清零，预留不再占用预算。
    """
    from codepilot.graph.routing import pending_review_calls
    from codepilot.graph.stop_rules import patch_hash
    from codepilot.schemas.review import ReviewResult

    reserve = _reserve(_state())
    budget = RepairBudget(max_total_tokens=100_000, max_cost=1.0)
    state = _state(budget=budget, tokens=10_000, cost=0.1, llm_calls=1)
    manager = BudgetManager()
    before = state.usage.model_dump(mode="json")

    assert manager.check(state, Activity.REVIEW, reservation=reserve) is None
    assert manager.evaluate(state, Activity.REVIEW, reservation=reserve).usage_for(
        Category.COST
    ) == (0.1, 1.0)

    # 审查落账：一次真实调用的费用进账本，账本只多出这一笔。
    settled = state.model_copy(deep=True)
    settled.usage.add_call(
        alias="review-model",
        input_tokens=REVIEW_CONTEXT_TOKENS,
        output_tokens=reserve.tokens // 2,
        cost=reserve.cost / 2,
        role="reviewer",
    )
    settled.review_calls = 1
    assert settled.usage.llm_calls == 2
    assert settled.usage.cost == pytest.approx(0.1 + reserve.cost / 2)
    assert state.usage.model_dump(mode="json") == before, "求值不得动账本"

    # 结论绑定当前候选补丁之后：初审已经挣到了，预留立刻归零（同一份预留不会被扣两次）。
    candidate = "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    digest = patch_hash(candidate)
    reviewed = settled.model_copy(
        update={
            "current_patch": candidate,
            "review": ReviewResult(approved=True, summary="ok", reviewed_patch_hash=digest),
        }
    )
    assert pending_review_calls(reviewed, candidate_digest=digest) == 0
    assert (
        manager.closure_reservation(
            reviewed,
            reviewer_available=True,
            pending_review_calls=pending_review_calls(reviewed, candidate_digest=digest),
        ).review_calls
        == 0
    )


def test_reviewer_keeps_wall_clock_headroom_from_pre_review_nodes() -> None:
    """Token/cost reservation alone is not enough: tests can eat the wall clock.

    A review has no separate container, but it still needs time for its model
    call. The reservation therefore also shrinks the TIME ceiling of LLM /
    codegen / repair nodes, while the review activity itself uses the real
    hard limit (the reservation is for the reviewer, not against it).
    """
    state = _state(budget=RepairBudget(max_runtime_seconds=100))
    reserve = _reserve(state)
    assert reserve.review_calls == 2
    assert reserve.seconds == 2 * REVIEW_TIME_RESERVE_SECONDS
    manager = BudgetManager()
    blocked = manager.evaluate(state, Activity.LLM, reservation=reserve)
    assert StopReason.BUDGET_TIME in blocked.exhausted
    assert manager.evaluate(state, Activity.REVIEW).primary is None


def test_outstanding_precharge_is_counted_once_and_never_double_billed() -> None:
    """§三.4：已结算 + 已预扣 + 预留三段各自只算一次，相加才是可用 headroom。

    预扣是"已经承诺出去、``state.usage`` 还没看见"的钱，预留是"还不许承诺出去"的钱。
    判定必须把两者都让出来；同时预留依旧不落账本，重复求值不会产生第二笔费用。
    """
    state = _state(budget=RepairBudget(max_cost=1.0), tokens=20_000, cost=0.02, llm_calls=2)
    reserve = _reserve(state)
    manager = BudgetManager()
    evaluation = manager.evaluate(state, Activity.LLM, reservation=reserve, outstanding=0.3)
    used, limit = evaluation.usage_for(Category.COST)
    assert used == pytest.approx(0.32), "已结算 0.02 + 预扣 0.30，各只加一次"
    assert limit == pytest.approx(1.0 - reserve.cost), "预留从上限里扣，同样只扣一次"
    assert evaluation.reserved == (
        (Category.TOKENS, float(reserve.tokens)),
        (Category.COST, reserve.cost),
        (Category.TIME, reserve.seconds),
    ), "预留对 token、费用与墙钟各扣一次，不多不少"
    assert (
        manager.evaluate(state, Activity.LLM, reservation=reserve, outstanding=0.3).usage[:2]
        == evaluation.usage[:2]
    ), "求值无副作用：同一状态两次判定必须给出同样的读数"
    # 预扣把剩余吃光时，阻断原因是费用，而不是凭空多出来的额度。
    assert manager.check(state, Activity.LLM, reservation=reserve) is None, "没有预扣时照常放行"
    assert (
        manager.check(state, Activity.LLM, reservation=reserve, outstanding=0.95)
        is StopReason.BUDGET_COST
    )


@pytest.mark.parametrize(
    ("outstanding", "target", "rates", "expected"),
    [
        ((0.5, "CNY"), "CNY", {}, 0.5),
        ((0.5, "USD"), "USD", {}, 0.5),
        ((0.5, "USD"), "CNY", {"USD": 7.0}, 3.5),
        ((0.0, "USD"), "CNY", {}, 0.0),
        (None, "CNY", {}, 0.0),
        ((0.5, "USD"), "CNY", {}, None),
        ((0.5, "USD"), "CNY", {"EUR": 8.0}, None),
    ],
)
def test_outstanding_conversion_and_a_missing_rate_is_unknown(
    outstanding: tuple[float, str] | None,
    target: str,
    rates: dict[str, float],
    expected: float | None,
) -> None:
    """§三.6 币种边界：缺汇率时当作**未知**，绝不按 1.0 硬套。

    :func:`codepilot.llm.cost.convert` 缺汇率按 1.0 处理：同币种无害，跨币种会把七倍
    差额读成"差不多"。预算准入宁可认为这笔钱位置不明（由调用方 fail closed），也不
    能拿一个方向可能反了的数字继续派发。
    """
    assert (
        outstanding_in_budget_currency(outstanding, target=target, exchange_rates=rates) == expected
    )


def test_a_negative_outstanding_is_a_configuration_error_not_a_free_allowance() -> None:
    with pytest.raises(BudgetConfigurationError):
        BudgetManager().evaluate(_state(), Activity.LLM, outstanding=-1.0)
    with pytest.raises(BudgetConfigurationError):
        outstanding_in_budget_currency((-1.0, "CNY"), target="CNY")


def test_reservation_below_zero_is_clamped_and_still_blocks() -> None:
    """预留大于剩余时可用上限归零：判定必须落在耗尽上，而不是给出负数天花板。"""
    budget = RepairBudget(max_total_tokens=100_000)
    state = _state(budget=budget, tokens=60_000)
    evaluation = BudgetManager().evaluate(
        state,
        Activity.CODEGEN,
        reservation=_reserve(state),  # 120_000 > 剩余额度
    )
    assert evaluation.usage_for(Category.TOKENS) == (60_000.0, 0.0)
    assert evaluation.primary is StopReason.BUDGET_TOKENS


def test_reservation_never_creates_headroom_above_the_hard_cap() -> None:
    """硬上限始终有效：已经花完时，预留不会凭空变出可用额度。"""
    budget = RepairBudget(max_total_tokens=100_000)
    state = _state(budget=budget, tokens=100_000)
    evaluation = BudgetManager().evaluate(state, Activity.CODEGEN, reservation=_reserve(state))
    assert evaluation.primary is StopReason.BUDGET_TOKENS
    assert evaluation.usage_for(Category.TOKENS)[1] <= 100_000


def test_insufficient_remaining_budget_blocks_the_dispatch_before_spending() -> None:
    """§七.10：剩余费用/token 不足以支撑闭环时，派发前就阻断。"""
    budget = RepairBudget(max_total_tokens=60_000)
    state = _state(budget=budget, tokens=40_000, cost=0.4, llm_calls=4)
    manager = BudgetManager()
    reserve = _reserve(state)
    assert reserve.tokens > 60_000 - 40_000  # 前提：剩下的确实不够
    evaluation = manager.evaluate(state, Activity.CODEGEN, reservation=reserve)
    assert evaluation.primary is StopReason.BUDGET_TOKENS
    assert manager.coding_token_ceiling(state, reserve) <= 20_000
    # 验证与审查自己不受这条阻断影响——它们正是被保护的一方。
    assert manager.check(state, Activity.REVIEW) is None


def test_unknown_cost_with_a_hard_cap_blocks_codegen_fail_closed() -> None:
    """未知费用 + 设了 ``max_cost``：无法证明闭环付得起，就不许派发编码。"""
    budget = RepairBudget(max_cost=10.0)
    state = _state(budget=budget, tokens=1_000, cost=1.0, cost_unknown=1)
    manager = BudgetManager()
    evaluation = manager.evaluate(state, Activity.CODEGEN, reservation=_reserve(state))
    assert evaluation.primary is StopReason.BUDGET_COST
    assert evaluation.summary()["reserved"]


def test_unknown_cost_without_a_cap_does_not_invent_a_block() -> None:
    """负控制：未设 ``max_cost`` 时费用本就不封顶（02 §15.3），不得凭空拦停。"""
    state = _state(budget=RepairBudget(max_cost=None), tokens=1_000, cost_unknown=1)
    evaluation = BudgetManager().evaluate(state, Activity.CODEGEN, reservation=_reserve(state))
    assert StopReason.BUDGET_COST not in evaluation.exhausted


def test_cost_ceiling_handed_to_the_coding_agent_is_netted() -> None:
    budget = RepairBudget(max_cost=10.0)
    state = _state(budget=budget, tokens=10_000, cost=1.0, llm_calls=1)
    manager = BudgetManager()
    reserve = _reserve(state)
    ceiling = manager.coding_cost_ceiling(state, reserve)
    assert ceiling is not None
    assert 0.0 < ceiling < 10.0
    assert manager.coding_cost_ceiling(_state(budget=RepairBudget(max_cost=None)), reserve) is None


# ------------------------------------------------------------------ 专属额度


def test_review_activity_ignores_the_coding_tool_budget() -> None:
    """§三.3：编码的工具调用消耗不得占用审查专属额度。"""
    budget = RepairBudget(max_tool_calls_per_iteration=30, max_review_calls=2)
    state = _state(budget=budget, tool_calls=30)
    manager = BudgetManager()
    assert manager.check(state, Activity.REVIEW) is None
    assert manager.check(state, Activity.CODEGEN) is StopReason.BUDGET_TOOL_CALLS


def test_review_allowance_stops_its_own_loop() -> None:
    """审查轮次用完之后，``review`` 与再来一轮修复都会被自己的专属额度拦下。"""
    budget = RepairBudget(max_review_calls=2)
    spent = _state(budget=budget, review_calls=2)
    manager = BudgetManager()
    assert manager.check(spent, Activity.REVIEW) is StopReason.BUDGET_REVIEW_CALLS
    assert manager.check(spent, Activity.REPAIR) is StopReason.BUDGET_REVIEW_CALLS
    assert manager.check(_state(budget=budget, review_calls=1), Activity.REVIEW) is None


def test_review_call_stop_maps_to_a_budget_run_status() -> None:
    """``budget_review_calls`` 必须是预算类终态，而不是 ``stopped``/``error``。"""
    from codepilot.graph.stop_rules import StopDecision

    decision = StopDecision(
        primary=StopReason.BUDGET_REVIEW_CALLS,
        all_reasons=(StopReason.BUDGET_REVIEW_CALLS,),
        explanations=("review allowance spent",),
    )
    assert StopRules().status_for(decision) is RunStatus.BUDGET_EXHAUSTED


def test_negative_review_allowance_is_rejected() -> None:
    with pytest.raises(BudgetConfigurationError):
        BudgetManager().evaluate(_state(budget=RepairBudget(max_review_calls=-1)), Activity.REVIEW)


def test_reservation_type_is_frozen_and_summary_readable() -> None:
    from dataclasses import FrozenInstanceError

    reserve = ClosureReservation(tokens=10, cost=0.5, review_calls=1, estimated=True)
    assert reserve.summary()["tokens"] == "10"
    with pytest.raises(FrozenInstanceError):
        reserve.tokens = 11  # type: ignore[misc]


def test_reservation_without_a_reservation_argument_is_unchanged() -> None:
    """旧调用方（停止规则）不传预留时，行为必须与 Phase 10 一致。"""
    budget = RepairBudget(max_total_tokens=100_000)
    state = _state(budget=budget, tokens=99_000)
    assert BudgetManager().check(state, Activity.REPAIR) is None


def test_cross_process_resume_nets_the_ceiling_from_the_restored_ledger() -> None:
    """§七.11：跨进程续跑时预留按 checkpoint 里的账本重算，既不重复计费也不重复派发。

    新进程的内存计数器一律为 0，唯一可信的是 state 里的累计量（:meth:`Usage.reset_to`
    正是 ``Deps.bind`` 做的事）。判定必须与旧进程**逐字节相同**：偏松就会把已经花掉的
    预留当成还没花，偏紧就会白丢一次派发。
    """
    budget = RepairBudget(max_total_tokens=100_000, max_cost=1.0)
    original = _state(budget=budget, tokens=40_000, cost=0.4, llm_calls=4)
    manager = BudgetManager()
    before = (
        manager.coding_token_ceiling(original, _reserve(original)),
        manager.coding_cost_ceiling(original, _reserve(original)),
        _reserve(original),
    )

    # 新进程：state 从 checkpoint 读回，内存账本从零开始。
    revived = _state(budget=budget)
    revived.usage.reset_to(original.usage)
    after = (
        manager.coding_token_ceiling(revived, _reserve(revived)),
        manager.coding_cost_ceiling(revived, _reserve(revived)),
        _reserve(revived),
    )
    assert after == before
    # 复用而不是重新派发：审查次数由 state 推导，重放不会自己加一次。
    assert manager.review_calls_used(revived) == manager.review_calls_used(original) == 0


# --------------------------------------------------------------------- 闭环准入（DA-024）


def _plan(
    *,
    spent: int,
    allowance: int = 12,
    target: bool = True,
    impact: bool = True,
    full: bool = True,
) -> ClosureTestPlan:
    return BudgetManager().closure_test_plan(
        _state(budget=RepairBudget(max_test_runs=allowance)),
        target_tests_planned=target,
        impact_level_expected=impact,
        full_level_expected=full,
        runs_spent=spent,
    )


def test_the_closure_plan_counts_container_calls_not_test_cases() -> None:
    """``required`` 数的是"还要跨几次容器"，与每次圈进多少用例无关。

    口径必须与 :class:`~codepilot.graph.deps.AccountingSandbox` 一致：真实基线里
    L3 一次跑 779 条也只算一次。写成"用例数"会让准入在任何真实仓库上都永远不足。
    """
    plan = _plan(spent=3)
    assert (
        plan.required
        == (
            CLOSURE_PATCH_VALIDATION_RUNS
            + CLOSURE_TARGET_TEST_RUNS
            + 2 * CLOSURE_REGRESSION_LEVEL_RUNS
        )
        == 3
    )
    assert plan.remaining == 9
    assert plan.feasible is True
    assert CLOSURE_PATCH_VALIDATION_RUNS == 0, "PatchValidator 是静态校验，不花容器"


def test_the_admission_boundary_is_one_counting_unit() -> None:
    """**恰好够用**放行，**少一次**拒绝：判定读的是"还要几次"，不是"还剩多少余量"。"""
    assert _plan(spent=9).remaining == 3
    assert _plan(spent=9).feasible is True
    assert _plan(spent=10).remaining == 2
    assert _plan(spent=10).feasible is False


def test_the_flaky_retry_reserve_comes_out_of_the_coders_share_not_out_of_the_closure() -> None:
    """复跑预留从"模型自己能花的份额"里扣，永不从闭环那几次里扣。

    方向很重要：闭环不足时唯一正确的动作是**不启动这一轮**；把预留算进 ``required``
    则会反过来把"本来能验完的一轮"判成不可行，白丢一次修复机会。
    """
    assert CLOSURE_FLAKY_RETRY_RESERVE == 2
    plan = _plan(spent=6)  # 剩 6，闭环要 3
    assert plan.required == 3
    assert plan.coder_share == 6 - 3 - CLOSURE_FLAKY_RETRY_RESERVE == 1
    exactly = _plan(spent=9)
    assert exactly.coder_share == 0, "刚够时模型一次都不能自己跑，闭环那三次不动"
    # 预留不会被负数吞掉。
    assert _plan(spent=11).coder_share == 0


def test_the_only_way_l3_costs_less_is_the_runner_declining_to_run_it() -> None:
    """回归层级一律按**一次容器执行**计价；抽样不打折。

    ``full_level_expected=False`` 唯一的来源是 :class:`~codepilot.graph.regression.
    RegressionRunner` 自己的 L3 全量次数上限（跑过两次就不再跑），**不是**"改成抽样
    就便宜一点"。把降级当成省额度的手段会同时毁掉覆盖范围和这条判据的意义。
    """
    assert CLOSURE_REGRESSION_LEVEL_RUNS == 1, "L2 与 L3 各算一次调用，与圈进多少用例无关"
    with_l3 = _plan(spent=4, full=True)
    without_l3 = _plan(spent=4, full=False)
    assert with_l3.required == 3
    assert without_l3.required == 2
    # 没有目标测试时（复现失败且规划没选测试）不该为空跑的一次执行留额度。
    assert _plan(spent=4, target=False).required == 2


def test_the_plan_never_lends_out_more_than_the_allowance_holds() -> None:
    """不变量：可行时"份额 + 闭环所需 ≤ 剩余"；不可行时绝不派发份额。

    上限只被读、从不被放松：``allowance`` 永远等于 ``max_test_runs``，准入判定既不会
    偷偷提高它，也不会因为"额度紧"就少算闭环要的那几次。
    """
    for spent in range(0, 14):
        plan = _plan(spent=spent, allowance=12)
        assert plan.allowance == 12
        assert plan.coder_share >= 0
        assert plan.remaining == max(12 - spent, 0)
        assert plan.feasible == (plan.remaining >= plan.required)
        if plan.feasible:
            assert plan.coder_share + plan.required <= plan.remaining
        else:
            assert plan.coder_share == 0, "买不起闭环的一轮不该再有任何自跑测试的份额"


def test_an_overdrawn_allowance_reads_as_zero_left_not_as_negative() -> None:
    """已经花超了（历史/恢复都可能）时，剩余是 0 而不是负数。"""
    plan = _plan(spent=15, allowance=12)
    assert plan.remaining == 0
    assert plan.feasible is False
    assert plan.coder_share == 0


def test_negative_counts_are_a_configuration_error() -> None:
    """负计数会让判定"恰好够用"，静默放行一个无法闭环的轮次。"""
    with pytest.raises(BudgetConfigurationError):
        BudgetManager().closure_test_plan(
            _state(),
            target_tests_planned=True,
            impact_level_expected=True,
            full_level_expected=True,
            runs_spent=-1,
        )


def test_the_record_and_the_detail_are_the_same_arithmetic() -> None:
    """报告、事件与失败原因必须共用一句算式，否则三处读数会漂。"""
    plan = _plan(spent=5, allowance=7)
    record = plan.record(trigger="review")
    assert (record.remaining, record.required, record.feasible) == (2, 3, False)
    assert record.trigger == "review"
    assert record.coder_share == 0
    assert record.summary()["runs_spent"] == "5"
    assert record.summary()["allowance"] == "7"
    assert record.detail == plan.detail()
    assert "5 spent of 7" in record.detail
    assert "closure requires 3" in record.detail
    assert "refused before any spend" in record.detail


def test_a_feasible_record_names_the_share_it_left_the_coding_stage() -> None:
    plan = _plan(spent=2, allowance=9)
    record = plan.record(trigger="generation")
    assert record.feasible is True
    assert record.coder_share == 2
    assert "coding stage capped at 2 self-triggered run(s)" in record.detail


def test_the_plan_is_a_reading_that_never_touches_the_ledger() -> None:
    """准入判定只读不写：连判两次，账本与计数的变化都必须是零。

    它与闭环预留同一条纪律（预留不入账本，所以不可能重复计费）。这一条不是洁癖：
    恢复时同一条边可能被重走，读数若是记账就会多扣一份额度、把后面的轮次饿死。
    """
    state = _state(budget=RepairBudget(max_test_runs=12), tokens=1_000, cost=0.5)
    manager = BudgetManager()
    before = (state.test_runs, state.usage.llm_calls, state.usage.cost)
    for _ in range(2):
        manager.closure_test_plan(
            state,
            target_tests_planned=True,
            impact_level_expected=True,
            full_level_expected=True,
            runs_spent=5,
        )
    assert (state.test_runs, state.usage.llm_calls, state.usage.cost) == before
