"""E-04：Benchmark 指标的单元测试。

每个关键公式都给出**独立手算**的期望值，而不是"跑一遍看它自己等于自己"。
指标层最常见的两类事故——分母被悄悄改掉、以及"没样本"被渲染成 0%/100%——
恰好都是自反式断言看不出来的。

03 §19 与 02 §17.2 的口径在这里逐条落成断言：

* 总体比例的分母是任务总数 N，含环境失败；
* 条件比例（Repair Success Rate、Self-verified Precision）保留自己的子集分母；
* First Attempt Resolved 来自第一轮补丁的**独立评测**，不由最终结果推断。
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from codepilot.eval.metrics import (
    CostMetrics,
    LocalizationSample,
    Ratio,
    TaskOutcome,
    TaskOutcomeSet,
    VerificationMode,
    aggregate_metric_values,
    cost_metrics,
    file_recall_at_k,
    function_recall_at_k,
    iterations_metrics,
    localization_exact_at_k,
    per_seed_metrics,
    reliability_metrics,
    repair_metrics,
    summarize,
)


def _outcome(task_id: str, **kwargs: Any) -> TaskOutcome:
    return TaskOutcome(task_id=task_id, **kwargs)


def _resolved(task_id: str, **kwargs: Any) -> TaskOutcome:
    """默认"成功解决、自称成功、埋点齐全"的任务结论，只覆盖测试关心的差异。

    ``agent_claims_success`` 默认 True 是因为多数用例要的是"自称 vs 评测"两栏
    都有值；不默认的话绝大多数用例会落在 Precision 分母为 0 的无定义分支上，
    测不到真正要测的东西。
    """
    defaults: dict[str, Any] = {
        "resolved": True,
        "first_attempt_resolved": True,
        "agent_claims_success": True,
        "iterations": 1,
        "input_tokens": 100,
        "output_tokens": 50,
        "llm_calls": 2,
        "cost": 1.0,
        "runtime_seconds": 10.0,
        "verification_mode": VerificationMode.FULL,
    }
    defaults.update(kwargs)
    return _outcome(task_id, **defaults)


# ============================================================ 1. 零任务


def test_zero_tasks_leaves_every_ratio_undefined() -> None:
    metrics = repair_metrics([])
    assert metrics.total_tasks == 0
    assert metrics.resolved == Ratio(0, 0)
    assert metrics.resolved.value is None
    assert metrics.repair_gain is None
    assert metrics.repair_success_rate.value is None
    assert metrics.self_verified_precision.value is None

    cost = cost_metrics([])
    assert cost.total_tasks == 0
    assert cost.total_cost is None
    assert cost.mean_tokens_per_task is None
    assert cost.cost_tasks_unknown == 0

    iterations = iterations_metrics([])
    assert iterations.count == 0
    assert (iterations.mean, iterations.median, iterations.p90) == (None, None, None)


def test_summarize_of_empty_batch_is_still_well_formed() -> None:
    suite = summarize([])
    payload = suite.as_dict()
    assert payload["total_tasks"] == 0
    assert payload["repair"]["resolved_rate"]["value"] is None
    assert payload["localization"]["k1"]["file_recall"]["denominator"] == 0


# ============================================================ 2. 全部成功


def test_all_success_gives_one_and_zero_repair_gain() -> None:
    outcomes = [_resolved(f"t{i}") for i in range(4)]
    metrics = repair_metrics(outcomes)
    # 手算：4/4 = 1.0；首轮也是 4/4 = 1.0；gain = 0。
    assert metrics.resolved.numerator == 4
    assert metrics.resolved.denominator == 4
    assert metrics.resolved.value == pytest.approx(1.0)
    assert metrics.first_attempt_resolved.value == pytest.approx(1.0)
    assert metrics.repair_gain == pytest.approx(0.0)
    # 第一轮全过 ⇒ 条件分母为 0，Repair Success Rate 无定义，而不是 0 或 1。
    assert metrics.repair_success_rate.denominator == 0
    assert metrics.repair_success_rate.value is None
    assert metrics.self_verified_precision.value == pytest.approx(1.0)
    assert metrics.undetermined_tasks == 0


# ============================================================ 3. 全部失败


def test_all_failure_keeps_denominator_at_n() -> None:
    outcomes = [
        _resolved(f"t{i}", resolved=False, first_attempt_resolved=False, iterations=3)
        for i in range(5)
    ]
    metrics = repair_metrics(outcomes)
    assert metrics.resolved.value == pytest.approx(0.0)
    assert metrics.resolved.denominator == 5
    assert metrics.repair_gain == pytest.approx(0.0)
    # 条件分母 = 5 个首轮未解决；分子 0 ⇒ 0/5 = 0.0（这里 0 是有定义的）。
    assert metrics.repair_success_rate == Ratio(0, 5)
    assert metrics.repair_success_rate.value == pytest.approx(0.0)


# ============================================================ 4. 全部环境错误


def test_environment_failures_stay_in_the_denominator() -> None:
    """02 §17.2 / 03 §19：分母永远是 N，含环境构建失败的任务。

    把环境失败从分母里剔除，等于只报告"镜像拉得下来的那部分"的成功率。
    """
    outcomes = [
        _resolved(
            f"t{i}",
            resolved=None,
            first_attempt_resolved=None,
            environment_failure=True,
            stop_reason="sandbox_failure",
            verification_mode=VerificationMode.NONE,
        )
        for i in range(3)
    ]
    metrics = repair_metrics(outcomes)
    assert metrics.resolved == Ratio(0, 3)
    assert metrics.undetermined_tasks == 3
    # 首轮结论是 None（没测出来），不是 False，因此不进条件分母。
    assert metrics.repair_success_rate.denominator == 0
    assert metrics.repair_success_rate.value is None

    reliability = reliability_metrics(outcomes)
    assert reliability["environment_failures"]["numerator"] == 3
    assert reliability["environment_failures"]["denominator"] == 3
    assert reliability["stop_reasons"]["counts"] == {"sandbox_failure": 3}


def test_env_failure_is_not_reported_as_a_test_failure() -> None:
    """同一个环境失败任务，Resolved Rate 记 0 但口径必须说明"未判定"。"""
    outcomes = [_resolved("a"), _resolved("b", resolved=None, environment_failure=True)]
    metrics = repair_metrics(outcomes)
    assert metrics.resolved == Ratio(1, 2)
    assert metrics.undetermined_tasks == 1
    # 自检可靠性用三态：b 没有结论，不进 Precision 的分母。
    assert metrics.self_verified_precision.denominator == 1


# ============================================================ 5. 部分成功


def test_partial_success_ratios_are_hand_checkable() -> None:
    outcomes = [
        _resolved("a", resolved=True, first_attempt_resolved=True),
        _resolved("b", resolved=True, first_attempt_resolved=False),
        _resolved("c", resolved=False, first_attempt_resolved=False),
        _resolved("d", resolved=False, first_attempt_resolved=False),
    ]
    metrics = repair_metrics(outcomes)
    # 手算：Resolved 2/4=0.5；First 1/4=0.25；gain=0.25；
    # Repair Success 分母=首轮失败的 3 个，其中最终解决 1 个 ⇒ 1/3。
    assert metrics.resolved == Ratio(2, 4)
    assert metrics.first_attempt_resolved == Ratio(1, 4)
    assert metrics.repair_gain == pytest.approx(0.25)
    assert metrics.repair_success_rate == Ratio(1, 3)
    assert metrics.repair_success_rate.value == pytest.approx(1 / 3)


# ============================================================ 6. 首轮失败、后续成功


def test_repair_gain_is_positive_only_when_iteration_actually_helped() -> None:
    outcomes = [
        _resolved(f"t{i}", resolved=True, first_attempt_resolved=False, iterations=3)
        for i in range(2)
    ] + [_resolved("t3", resolved=True, first_attempt_resolved=True, iterations=1)]
    metrics = repair_metrics(outcomes)
    # 手算：Resolved 3/3=1.0；First 1/3≈0.3333；gain≈0.6667；
    # Repair Success 分母=2，分子=2 ⇒ 1.0。
    assert metrics.resolved == Ratio(3, 3)
    assert metrics.first_attempt_resolved == Ratio(1, 3)
    assert metrics.repair_gain == pytest.approx(2 / 3)
    assert metrics.repair_success_rate == Ratio(2, 2)


# ============================================================ 7. 首轮成功、最终失败


def test_repair_gain_can_be_negative() -> None:
    """迭代把本来通过的补丁改坏了。gain 为负是**合法**结果，不能被截断成 0。

    截断的话，"修复循环有害"这种真实发现就会从报告里消失。
    """
    outcomes = [
        _resolved("a", resolved=False, first_attempt_resolved=True, iterations=4),
        _resolved("b", resolved=True, first_attempt_resolved=True, iterations=1),
    ]
    metrics = repair_metrics(outcomes)
    # 手算：Resolved 1/2=0.5；First 2/2=1.0；gain=-0.5。
    assert metrics.resolved == Ratio(1, 2)
    assert metrics.first_attempt_resolved == Ratio(2, 2)
    assert metrics.repair_gain == pytest.approx(-0.5)
    # a 的首轮是 True，不属"首轮未解决"，因此条件分母为 0。
    assert metrics.repair_success_rate.denominator == 0


def test_first_attempt_is_not_inferred_from_final_result() -> None:
    """第一轮补丁要**单独评测**过才有 First Attempt Resolved。

    如果允许 None 被"最终通过"推断成 True，这个指标就永远等于 Resolved Rate，
    Repair Gain 恒为 0，迭代修复的贡献再也测不出来。
    """
    inferred = _resolved("a", resolved=True, first_attempt_resolved=None)
    metrics = repair_metrics([inferred])
    assert metrics.first_attempt_resolved == Ratio(0, 1)
    assert metrics.undetermined_tasks == 0  # 最终结论仍然是判定出来的
    assert metrics.repair_gain == pytest.approx(1.0)


# ============================================================ 8/9/12. 自检可靠性


def test_agent_claims_success_but_gold_says_no() -> None:
    outcomes = [_resolved("a", resolved=False, agent_claims_success=True)]
    metrics = repair_metrics(outcomes)
    # 分母=1（自认成功且有结论），分子=0 ⇒ Precision 0.0。
    precision = metrics.self_verified_precision
    assert (precision.numerator, precision.denominator, precision.value) == (0, 1, 0.0)
    assert metrics.resolved == Ratio(0, 1)


def test_gold_passes_without_agent_claim_is_counted_but_not_credited() -> None:
    """评测通过但 Agent 没自称成功：不进 Precision 的分子也不进分母。

    它是"低估了自己"，与 Precision 要衡量的"过度自信"是相反方向的事，必须
    单独计数而不是塞进同一个比例。
    """
    outcomes = [_resolved("a", resolved=True, agent_claims_success=False)]
    metrics = repair_metrics(outcomes)
    precision = metrics.self_verified_precision
    assert precision.denominator == 0
    assert precision.value is None
    assert precision.evaluated_without_claim == 1
    assert metrics.resolved == Ratio(1, 1)


def test_claimed_success_without_verdict_is_exposed_not_hidden() -> None:
    outcomes = [
        _resolved("a", resolved=None, agent_claims_success=True, environment_failure=True),
        _resolved("b", resolved=True, agent_claims_success=True),
    ]
    precision = repair_metrics(outcomes).self_verified_precision
    assert (precision.numerator, precision.denominator, precision.value) == (1, 1, 1.0)
    # 100% 背后只有一个可判定样本，另一例无法判定——报告必须能看到这一点。
    assert precision.claimed_without_verdict == 1
    assert precision.total_tasks == 2
    assert "1/1" in precision.describe()


def test_no_self_claims_never_becomes_one_hundred_percent() -> None:
    outcomes = [_resolved(task_id, agent_claims_success=False) for task_id in ("a", "b")]
    precision = repair_metrics(outcomes).self_verified_precision
    assert precision.value is None
    assert not precision.defined
    assert "n/a" in precision.describe()


# ============================================================ 10/11. 补丁形态


def test_empty_and_invalid_patch_rates_use_n_as_denominator() -> None:
    outcomes = [
        _resolved("a", empty_patch=True, resolved=False, first_attempt_resolved=False),
        _resolved("b", invalid_patch=True, resolved=False, first_attempt_resolved=False),
        _resolved("c"),
        _resolved("d"),
    ]
    metrics = repair_metrics(outcomes)
    assert metrics.empty_patch_rate == Ratio(1, 4)
    assert metrics.invalid_patch_rate == Ratio(1, 4)
    # 空补丁与非法补丁是两件事，不能合成一个"坏补丁率"混掉。
    reliability = reliability_metrics(outcomes)
    assert reliability["no_valid_patch"]["numerator"] == 2


def test_empty_and_invalid_are_not_the_same_flag() -> None:
    outcome = _resolved("a", empty_patch=True)
    assert outcome.has_valid_patch is False
    both = _resolved("b", empty_patch=True, invalid_patch=True)
    metrics = repair_metrics([outcome, both])
    assert metrics.empty_patch_rate == Ratio(2, 2)
    assert metrics.invalid_patch_rate == Ratio(1, 2)


# ============================================================ 13. 成本分母


def test_cost_per_resolved_task_undefined_without_resolved_tasks() -> None:
    outcomes = [_resolved("a", resolved=False, first_attempt_resolved=False, cost=2.0)]
    cost = cost_metrics(outcomes)
    assert cost.total_cost == pytest.approx(2.0)
    assert cost.mean_cost_per_task == pytest.approx(2.0)
    # 没有成功任务 ⇒ 分母 0 ⇒ 无定义，不是 0.0（"每成功一个任务花 0 元"）。
    assert cost.mean_cost_per_resolved_task is None
    assert cost.resolved_tasks_with_cost == 0


def test_cost_per_resolved_task_divides_by_resolved_only() -> None:
    outcomes = [
        _resolved("a", resolved=True, cost=3.0),
        _resolved("b", resolved=True, cost=1.0),
        _resolved("c", resolved=False, cost=8.0),
    ]
    cost = cost_metrics(outcomes)
    # 手算：总费用 12；每题平均 4；每个成功任务平均 (3+1)/2 = 2。
    assert cost.total_cost == pytest.approx(12.0)
    assert cost.mean_cost_per_task == pytest.approx(4.0)
    assert cost.mean_cost_per_resolved_task == pytest.approx(2.0)
    assert cost.resolved_tasks_with_cost == 2


# ============================================================ 14. 缺失埋点


def test_missing_token_and_cost_data_never_counted_as_zero() -> None:
    """未知费用不等于 0，缺 token 不等于没用 token。"""
    outcomes = [
        _resolved("a", cost=1.0, input_tokens=100, output_tokens=100),
        _resolved(
            "b",
            cost=None,
            input_tokens=None,
            output_tokens=None,
            llm_calls=None,
            runtime_seconds=None,
        ),
    ]
    cost = cost_metrics(outcomes)
    assert cost.total_cost == pytest.approx(1.0)
    assert cost.cost_complete is False
    assert cost.cost_tasks_unknown == 1
    assert cost.cost_tasks_recorded == 1
    # 平均 token 只对**有埋点的**任务求平均：(200)/1，而不是 200/2=100。
    assert cost.mean_tokens_per_task == pytest.approx(200.0)
    assert cost.tokens_tasks_recorded == 1
    assert cost.total_llm_calls == 2  # b 的 llm_calls 是 None，不进合计
    assert cost.mean_runtime_seconds == pytest.approx(10.0)
    assert cost.runtime_tasks_recorded == 1
    assert cost.as_dict()["total_cost_is_lower_bound"] is True


def test_partial_cost_total_is_declared_a_lower_bound() -> None:
    cost = cost_metrics([_resolved("a", cost=1.0), _resolved("b", cost=None)])
    assert cost.cost_complete is False
    payload = cost.as_dict()
    assert payload["total_cost_is_lower_bound"] is True
    assert payload["cost_tasks_unknown"] == 1


def test_mixed_currencies_are_rejected_rather_than_summed() -> None:
    """把 CNY 和 USD 加起来是一个看起来很像数字的错误。"""
    outcomes = [
        _resolved("a", cost=1.0, cost_currency="CNY"),
        _resolved("b", cost=1.0, cost_currency="USD"),
    ]
    with pytest.raises(ValueError, match="币种"):
        cost_metrics(outcomes)


# ============================================================ 15. P90 边界


@pytest.mark.parametrize(
    "values,expected_mean,expected_median,expected_p90",
    [
        ([5.0], 5.0, 5.0, 5.0),  # n=1：三个统计量都是它自己
        ([1.0, 2.0], 1.5, 1.5, 2.0),  # n=2：ceil(1.8)=2 ⇒ 第 2 个
        ([1.0, 2.0, 3.0], 2.0, 2.0, 3.0),  # n=3：ceil(2.7)=3 ⇒ 最大值
        ([1.0, 2.0, 3.0, 4.0, 5.0], 3.0, 3.0, 5.0),  # n=5：ceil(4.5)=5
        ([float(i) for i in range(1, 11)], 5.5, 5.5, 9.0),  # n=10：ceil(9)=9 ⇒ 第 9 个
    ],
)
def test_p90_uses_nearest_rank(
    values: list[float], expected_mean: float, expected_median: float, expected_p90: float
) -> None:
    outcomes = [_resolved(f"t{i}", iterations=int(value)) for i, value in enumerate(values)]
    stats = iterations_metrics(outcomes)
    assert stats.count == len(values)
    assert stats.mean == pytest.approx(expected_mean)
    assert stats.median == pytest.approx(expected_median)
    assert stats.p90 == pytest.approx(expected_p90)


def test_unordered_input_does_not_change_the_distribution() -> None:
    """分位数必须在排序后取，否则"输入顺序影响 P90"这种 bug 会静默存在。"""
    ascending = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    shuffled = [7, 3, 10, 1, 8, 5, 2, 9, 4, 6]
    first = iterations_metrics([_resolved(f"a{i}", iterations=v) for i, v in enumerate(ascending)])
    second = iterations_metrics([_resolved(f"b{i}", iterations=v) for i, v in enumerate(shuffled)])
    assert first.p90 == second.p90 == pytest.approx(math.ceil(0.9 * 10) * 1.0 - 1 + 1)
    assert first.mean == second.mean == pytest.approx(5.5)


def test_tasks_without_iteration_record_are_excluded_from_the_count_not_zeroed() -> None:
    outcomes = [_resolved("a", iterations=2), _resolved("b", iterations=None)]
    stats = iterations_metrics(outcomes)
    assert stats.count == 1
    assert stats.mean == pytest.approx(2.0)


# ============================================================ 16. 重复身份


def test_duplicate_task_and_seed_is_rejected() -> None:
    """同一 (task_id, seed) 出现两次会把分母悄悄撑大。"""
    outcomes = [_resolved("a"), _resolved("a")]
    with pytest.raises(ValueError, match="重复的"):
        repair_metrics(outcomes)
    with pytest.raises(ValueError, match="重复的"):
        cost_metrics(outcomes)
    with pytest.raises(ValueError, match="重复的"):
        summarize(outcomes)


def test_same_task_across_seeds_is_legitimate() -> None:
    """不同 seed 里重复 task_id 是设计要的东西，不能被去重规则误伤。"""
    outcomes = [_resolved("a", seed=s) for s in (0, 1, 2)]
    metrics = repair_metrics(outcomes)
    assert metrics.resolved == Ratio(3, 3)
    assert summarize(outcomes).seeds == (0, 1, 2)


def test_task_outcome_validates_negative_measurements() -> None:
    for kwargs in ({"iterations": -1}, {"cost": -0.5}, {"runtime_seconds": -1.0}, {"seed": -1}):
        with pytest.raises(ValueError):
            _outcome("a", **kwargs)


# ============================================================ 17. 多 seed 聚合


def test_per_seed_metrics_keep_independent_denominators() -> None:
    outcomes = [
        _resolved("a", seed=0, resolved=True, first_attempt_resolved=True),
        _resolved("b", seed=0, resolved=False, first_attempt_resolved=False),
        _resolved("a", seed=1, resolved=True, first_attempt_resolved=True),
        _resolved("b", seed=1, resolved=True, first_attempt_resolved=False),
    ]
    by_seed = per_seed_metrics(outcomes)
    assert sorted(by_seed) == [0, 1]
    # seed 0：1/2；seed 1：2/2。摊平成 4 个观测会得到 3/4——那是另一个数，
    # 而 02 §17.5 要的是"每个 seed 一个数，再报均值 ± 标准差"。
    assert by_seed[0].repair.resolved == Ratio(1, 2)
    assert by_seed[1].repair.resolved == Ratio(2, 2)
    assert by_seed[0].total_tasks == 2


def test_aggregate_reports_mean_and_sample_stddev() -> None:
    by_seed = list(
        per_seed_metrics(
            [
                _resolved("a", seed=0, resolved=True, first_attempt_resolved=True),
                _resolved("b", seed=0, resolved=False, first_attempt_resolved=False),
                _resolved("a", seed=1, resolved=True, first_attempt_resolved=True),
                _resolved("b", seed=1, resolved=True, first_attempt_resolved=False),
                _resolved("a", seed=2, resolved=True, first_attempt_resolved=True),
                _resolved("b", seed=2, resolved=True, first_attempt_resolved=True),
            ]
        ).values()
    )
    aggregate = aggregate_metric_values(by_seed)
    # 手算：三个 seed 的 Resolved Rate = 0.5, 1.0, 1.0 ⇒ mean=5/6≈0.8333；
    # 样本标准差 = sqrt(((0.5-0.8333)^2 + 2*(1-0.8333)^2)/2) ≈ 0.2887。
    assert aggregate["resolved_rate"]["mean"] == pytest.approx(5 / 6)
    assert aggregate["resolved_rate"]["stddev"] == pytest.approx(0.28867513, rel=1e-5)
    assert aggregate["resolved_rate"]["seeds"] == 3


def test_single_seed_has_no_defined_stddev() -> None:
    """一个 seed 时标准差**无定义**。报 0 会被读成"多次运行完全一致"。"""
    aggregate = aggregate_metric_values(list(per_seed_metrics([_resolved("a", seed=0)]).values()))
    assert aggregate["resolved_rate"]["stddev"] is None
    assert aggregate["resolved_rate"]["mean"] == pytest.approx(1.0)


def test_undefined_metric_is_skipped_rather_than_treated_as_zero() -> None:
    """某个 seed 上 Repair Success Rate 无定义（分母 0）时不进均值。

    把它当 0 计入会让"这一批没有首轮失败任务"看起来像"迭代修复全都没救回来"。
    """
    outcomes = [
        _resolved("a", seed=0, first_attempt_resolved=True),
        _resolved("b", seed=0, first_attempt_resolved=True),
        _resolved("a", seed=1, first_attempt_resolved=True),
        _resolved("b", seed=1, resolved=True, first_attempt_resolved=False),
    ]
    aggregate = aggregate_metric_values(list(per_seed_metrics(outcomes).values()))
    # seed 0 上没有任何"首轮失败"的任务 ⇒ 无定义；seed 1 ⇒ 1/1。
    # 均值必须只建立在有定义的那一个 seed 上，而不是把无定义当成 0 拉低到 0.5。
    assert aggregate["repair_success_rate"]["seeds"] == 1
    assert aggregate["repair_success_rate"]["mean"] == pytest.approx(1.0)
    assert aggregate["repair_success_rate"]["stddev"] is None


# ============================================================ 18. Full vs Sampled


def test_sampled_verification_is_visible_in_the_distribution() -> None:
    outcomes = [
        _resolved("a", verification_mode=VerificationMode.FULL),
        _resolved("b", verification_mode=VerificationMode.SAMPLED),
        _resolved("c", verification_mode=VerificationMode.SAMPLED),
        _resolved("d", verification_mode=VerificationMode.NONE),
    ]
    modes = reliability_metrics(outcomes)["verification_modes"]
    assert modes["counts"] == {"full": 1, "sampled": 2, "none": 1}
    assert modes["shares"]["sampled"] == pytest.approx(0.5)
    # 抽样验证与全量验证解决率不可混读，所以两者必须同时出现在一张表里。
    assert modes["total_tasks"] == 4


def test_limited_and_none_modes_are_distinct() -> None:
    outcomes = [
        _resolved("a", verification_mode=VerificationMode.LIMITED),
        _resolved("b", verification_mode=VerificationMode.NONE),
    ]
    counts = reliability_metrics(outcomes)["verification_modes"]["counts"]
    assert counts == {"limited": 1, "none": 1}


# ============================================================ 定位指标


def _loc(task_id: str, **kwargs: Any) -> LocalizationSample:
    return LocalizationSample(task_id=task_id, **kwargs)


def test_file_recall_counts_at_least_one_gold_file_in_top_k() -> None:
    samples = [
        _loc("a", predicted_files=("x.py", "y.py"), gold_files=frozenset({"y.py"})),
        _loc("b", predicted_files=("p.py", "q.py"), gold_files=frozenset({"z.py"})),
    ]
    # 手算：a 命中（y.py 在 Top-2 内），b 未命中 ⇒ 1/2。
    assert file_recall_at_k(samples, 2) == Ratio(1, 2)
    assert file_recall_at_k(samples, 1) == Ratio(0, 2)


def test_no_localization_result_counts_as_a_miss_not_an_absence() -> None:
    """定位为空是分母里的一次失败，不能被"没数据"抹掉。"""
    samples = [
        _loc("a", predicted_files=(), gold_files=frozenset({"y.py"})),
        _loc("b", predicted_files=("y.py",), gold_files=frozenset({"y.py"})),
    ]
    assert file_recall_at_k(samples, 5) == Ratio(1, 2)


def test_tasks_without_gold_are_not_in_the_denominator() -> None:
    """没有定位目标的任务，Recall 无从定义；塞进分母只会制造假失败。"""
    samples = [
        _loc("a", predicted_files=("x.py",), gold_files=frozenset()),
        _loc("b", predicted_files=("y.py",), gold_files=frozenset({"y.py"})),
    ]
    assert file_recall_at_k(samples, 3) == Ratio(1, 1)


def test_duplicate_candidates_do_not_consume_top_k_slots() -> None:
    """去重的作用是"同一个文件不占多个名额"，它让真实命中的候选挤进 Top-K。

    不去重时 ``("x.py", "x.py", "gold.py")`` 的 Top-2 是 ``x.py`` 两次，gold 被挤到
    第三；去重后 Top-2 是 ``x.py`` + ``gold.py``，命中。两种算法给出不同的 Recall@K，
    而不去重那个是在奖励重复候选。
    """
    samples = [
        _loc("a", predicted_files=("x.py", "x.py", "gold.py"), gold_files=frozenset({"gold.py"}))
    ]
    assert file_recall_at_k(samples, 2) == Ratio(1, 1)
    assert file_recall_at_k(samples, 1) == Ratio(0, 1)
    # 排名必须被保留：去重不能顺手把顺序按字典序重排。
    ranked = [_loc("b", predicted_files=("z.py", "gold.py"), gold_files=frozenset({"gold.py"}))]
    assert file_recall_at_k(ranked, 1) == Ratio(0, 1)
    assert file_recall_at_k(ranked, 2) == Ratio(1, 1)


def test_multi_file_gold_separates_recall_from_exact() -> None:
    samples = [
        _loc(
            "a",
            predicted_files=("one.py", "two.py"),
            gold_files=frozenset({"one.py", "missing.py"}),
        )
    ]
    # 至少一个命中 ⇒ Recall 1/1；全部命中不成立 ⇒ Exact 0/1。
    assert file_recall_at_k(samples, 2) == Ratio(1, 1)
    assert localization_exact_at_k(samples, 2) == Ratio(0, 1)
    complete = [
        _loc(
            "b",
            predicted_files=("one.py", "missing.py"),
            gold_files=frozenset({"one.py", "missing.py"}),
        )
    ]
    assert localization_exact_at_k(complete, 2) == Ratio(1, 1)


def test_function_recall_treats_module_level_gold_as_the_module() -> None:
    """02 §17.2：gold 只改模块级代码时，以模块 symbol 计。"""
    module_level = [
        _loc("a", predicted_symbols=("pkg.core.normalize",), gold_symbols=frozenset({"pkg.core"}))
    ]
    assert function_recall_at_k(module_level, 1) == Ratio(1, 1)
    # 但反过来不成立：命中同模块的另一个函数，不能算命中一个函数级 gold。
    specific = [
        _loc(
            "b",
            predicted_symbols=("pkg.core.other",),
            gold_symbols=frozenset({"pkg.core.normalize"}),
        )
    ]
    assert function_recall_at_k(specific, 1) == Ratio(0, 1)


def test_localization_rejects_non_positive_k() -> None:
    samples = [_loc("a", predicted_files=("x.py",), gold_files=frozenset({"x.py"}))]
    for bad in (0, -1):
        with pytest.raises(ValueError, match="正整数"):
            file_recall_at_k(samples, bad)
        with pytest.raises(ValueError, match="正整数"):
            function_recall_at_k(samples, bad)
        with pytest.raises(ValueError, match="正整数"):
            localization_exact_at_k(samples, bad)


def test_summarize_reports_all_three_k_values() -> None:
    samples = [
        _resolved(
            "a", localization=_loc("a", predicted_files=("x.py",), gold_files=frozenset({"x.py"}))
        ),
        _resolved(
            "b", localization=_loc("b", predicted_files=("y.py",), gold_files=frozenset({"z.py"}))
        ),
    ]
    payload = summarize(samples, localization_k=(1, 3)).as_dict()
    assert payload["localization"]["k1"]["file_recall"] == {
        "numerator": 1,
        "denominator": 2,
        "value": 0.5,
    }
    assert payload["localization"]["k3"]["localization_exact"]["numerator"] == 1


# ============================================================ 辅助类型


def test_ratio_rejects_impossible_values() -> None:
    with pytest.raises(ValueError, match="分子"):
        Ratio(3, 2)
    with pytest.raises(ValueError, match="分母不能为负"):
        Ratio(0, -1)
    with pytest.raises(ValueError, match="分子不能为负"):
        Ratio(-1, 2)


def test_ratio_renders_undefined_as_na_not_as_a_percentage() -> None:
    assert Ratio(0, 0).percentage() == "n/a"
    assert str(Ratio(1, 4)) == "1/4 = 25.00%"


def test_task_outcome_set_declares_its_own_completeness() -> None:
    bundle = TaskOutcomeSet(
        outcomes=(_resolved("a"), _resolved("b")),
        declared_tasks=3,
        seeds_requested=(0, 1, 2),
        completed=2,
        not_executed=4,
        budget_blocked=0,
        evaluation_errors=0,
    )
    assert bundle.expected_observations == 9
    assert bundle.complete is False
    assert bundle.integrity == "partial"
    assert bundle.as_dict()["recorded_observations"] == 2


def test_task_outcome_set_coerces_lists_to_tuple() -> None:
    bundle = TaskOutcomeSet(outcomes=[_resolved("a")], declared_tasks=1, seeds_requested=(0,))  # type: ignore[list-item]
    assert isinstance(bundle.outcomes, tuple)
    assert bundle.complete is True
    assert bundle.integrity == "full"
    assert bundle.metrics().total_tasks == 1


# ============================================================ 纯度


def test_metrics_do_not_read_the_report_markdown() -> None:
    """指标必须由结果对象算出，不能从 summary.md 的自然语言里反向解析。

    那会让报告成为指标的**来源**而不是产物：手改一个数字就能"提高"成功率。
    """
    import inspect

    from codepilot.eval import metrics as metrics_module

    source = inspect.getsource(metrics_module)
    for forbidden in ("read_text", "open(", "subprocess", "requests", "docker", "markdown"):
        assert forbidden not in source, f"metrics 引用了 {forbidden}"


def test_every_metric_is_a_pure_function_of_its_input() -> None:
    outcomes = [
        _resolved("a", resolved=True, first_attempt_resolved=False, iterations=2, cost=1.5),
        _resolved("b", resolved=None, environment_failure=True, iterations=5, cost=None),
    ]
    first = summarize(outcomes).as_dict()
    second = summarize(list(reversed(outcomes))).as_dict()
    # 顺序无关（除了排序后必然一致的字段）；两次独立计算结果必须完全相同。
    assert first["repair"] == second["repair"]
    assert first["cost"]["total_cost"] == second["cost"]["total_cost"]
    assert first["reliability"]["stop_reasons"] == second["reliability"]["stop_reasons"]


def test_cost_metrics_type_shape_is_stable_for_serialisation() -> None:
    payload: dict[str, Any] = cost_metrics([_resolved("a")]).as_dict()
    assert isinstance(payload["total_cost"], float | int)
    assert CostMetrics.__name__ == "CostMetrics"


def test_metrics_accept_plain_sequences() -> None:
    """指标接受普通序列并按 N 计数，不依赖任何回调或惰性求值。"""
    outcomes = [_resolved("a"), _resolved("b")]
    assert len(outcomes) == 2
    assert repair_metrics(outcomes).total_tasks == 2
