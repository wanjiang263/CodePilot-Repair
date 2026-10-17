"""离线评测指标（02 §17.2、03 §19，docs/05 D-09 / E-03）。

这里的每个函数都必须是纯函数：不碰网络、不起容器、不读写文件。指标一旦需要
副作用才能算出来，它就再也无法在单元测试里被手算核对，也无法在评测脚本和图
节点之间复用。Phase 10/11 的 Benchmark 直接把这些函数当作接口使用，所以入参
是"任务级结论"而不是任何运行期对象。

## 分母口径（本模块最容易写错、后果也最大的部分）

02 §17.2 同时规定了两件事，两者不能混：

* **总体比例**的分母是任务总数 N，**包含**环境失败、没产出补丁、超预算的任务。
  把"没跑成"的任务从分母里剔掉，等于只在你成功的那部分上打分。
* **条件比例**的分母是它自己定义的那个子集——Repair Success Rate 只看"第一轮
  没解决"的任务，Self-verified Precision 只看"Agent 自称成功"的任务。把它们也
  摊到 N 上就不再是这两个指标了。

统一分母不该改变指标的数学定义，所以这里每个比例都带回自己的 ``numerator`` /
``denominator``。分母为 0 时值是 ``None`` 而**不是** 0.0 或 1.0：把"这一批里没有
样本"渲染成一个百分比，就是在编造结论。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from codepilot.eval.attribution import Attribution, EvaluationState
from codepilot.schemas.enums import RunStatus

__all__ = [
    "CostMetrics",
    "LocalizationSample",
    "Ratio",
    "RepairMetrics",
    "SelfVerifiedPrecision",
    "SelfVerifiedSample",
    "SuiteMetrics",
    "SummaryStatistics",
    "TaskOutcome",
    "TaskOutcomeSet",
    "VerificationMode",
    "agent_claims_success",
    "aggregate_metric_values",
    "attribution_metrics",
    "cost_metrics",
    "file_recall_at_k",
    "function_recall_at_k",
    "iterations_metrics",
    "localization_exact_at_k",
    "per_seed_metrics",
    "reliability_metrics",
    "repair_metrics",
    "self_verified_precision",
    "summarize",
]

#: Agent 会对外宣称"这一步成功了"的状态。``provisionally_verified`` 的边界见
#: ``RunReport``：它只表示一个通过校验的补丁跑过了所选授权测试，不表示最终解决。
_SELF_SUCCESS_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.PROVISIONALLY_VERIFIED}
)


@dataclass(frozen=True, slots=True)
class SelfVerifiedSample:
    """一个任务的两侧结论：Agent 自己怎么说，评测怎么说。"""

    task_id: str
    agent_claims_success: bool
    evaluation_passed: bool | None
    """``None`` = 评测没能给出结论（环境失败、镜像坏了）。

    它**不是** ``False``：把"没测出来"当成"测出来是错的"会系统性地压低 Precision，
    而那种下降与模型能力无关。
    """

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空")


def agent_claims_success(status: RunStatus | str) -> bool:
    """把一次运行的终态翻译成"Agent 是否自认成功"。"""
    normalized = status if isinstance(status, RunStatus) else RunStatus(status)
    return normalized in _SELF_SUCCESS_STATUSES


@dataclass(frozen=True, slots=True)
class SelfVerifiedPrecision:
    """Self-verified Precision 及其**分母口径**。

    分母不含证据的部分必须可见，否则一个"100%"会掩盖"其实只有 0 个样本"。
    """

    numerator: int
    denominator: int
    value: float | None
    claimed_without_verdict: int
    evaluated_without_claim: int
    total_tasks: int

    @property
    def defined(self) -> bool:
        return self.value is not None

    def describe(self) -> str:
        if self.value is None:
            return (
                f"Self-verified Precision: n/a（0/{self.total_tasks} 个任务自认成功且评测可判定；"
                "无证据时不计为 100%）"
            )
        return (
            f"Self-verified Precision: {self.numerator}/{self.denominator} = {self.value:.4f}"
            f"（分母=Agent 自认成功且评测给出结论的任务数；"
            f"另有 {self.claimed_without_verdict} 个自认成功的任务评测无法判定，"
            f"{self.evaluated_without_claim} 个评测通过但 Agent 未宣称成功）"
        )


def self_verified_precision(samples: Iterable[SelfVerifiedSample]) -> SelfVerifiedPrecision:
    """评测确认通过且 Agent 自认成功的任务数 / Agent 自认成功的任务数。

    口径（每一条都有对应的单测）：

    * 分母 = ``agent_claims_success`` 且 ``evaluation_passed is not None`` 的任务；
    * 分子 = 其中 ``evaluation_passed is True`` 的任务；
    * Agent 自认成功但评测失败 → 分母不变、分子不变 ⇒ Precision 下降；
    * 评测成功但 Agent 没有宣称成功 → **不**进分子也**不**进分母（单独计数）；
    * 没有任何任务自认成功 → ``value is None``，绝不返回 1.0；
    * 评测无法判定（环境失败）的自认成功任务被排除在分子分母之外，但必须被
      报告出来——它们决定了这个指标的可信样本量。

    这个指标衡量的是**自检的可靠性**，不是 Resolved Rate；两者混用会高估系统。
    """
    items = list(samples)
    ids = [item.task_id for item in items]
    if len(set(ids)) != len(ids):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        raise ValueError(f"重复的 task_id: {duplicates[:5]}")

    numerator = 0
    denominator = 0
    without_verdict = 0
    evaluated_without_claim = 0
    for item in items:
        if item.evaluation_passed is None:
            if item.agent_claims_success:
                without_verdict += 1
            continue
        if item.agent_claims_success:
            denominator += 1
            if item.evaluation_passed:
                numerator += 1
        elif item.evaluation_passed:
            evaluated_without_claim += 1

    value = (numerator / denominator) if denominator else None
    return SelfVerifiedPrecision(
        numerator=numerator,
        denominator=denominator,
        value=value,
        claimed_without_verdict=without_verdict,
        evaluated_without_claim=evaluated_without_claim,
        total_tasks=len(items),
    )


# ================================================================= E-03 指标全集


class VerificationMode(StrEnum):
    """评测结论的证据强度。混在一起报告就等于把抽样说成全量。"""

    __test__: ClassVar[bool] = False

    FULL = "full"
    SAMPLED = "sampled"
    LIMITED = "limited"
    NONE = "none"
    """没有产生任何评测结论（环境失败、补丁没应用上）。"""


@dataclass(frozen=True, slots=True)
class Ratio:
    """一个比例连同它的分子分母。

    ``value`` 为 ``None`` 表示"分母为 0，这个指标在本批任务上无定义"。调用方
    必须把 ``None`` 原样带到报告里；把它渲染成 0% 或 100% 都是编造。
    """

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.denominator < 0:
            raise ValueError("分母不能为负")
        if self.numerator < 0:
            raise ValueError("分子不能为负")
        if self.numerator > self.denominator:
            raise ValueError(f"分子 {self.numerator} 不能大于分母 {self.denominator}")

    @property
    def value(self) -> float | None:
        return (self.numerator / self.denominator) if self.denominator else None

    @property
    def defined(self) -> bool:
        return self.value is not None

    def percentage(self) -> str:
        return "n/a" if self.value is None else f"{self.value * 100:.2f}%"

    def __str__(self) -> str:
        return f"{self.numerator}/{self.denominator} = {self.percentage()}"

    def as_dict(self) -> dict[str, Any]:
        return {"numerator": self.numerator, "denominator": self.denominator, "value": self.value}


def _ratio(flags: Iterable[bool]) -> Ratio:
    items = list(flags)
    return Ratio(numerator=sum(1 for item in items if item), denominator=len(items))


@dataclass(frozen=True, slots=True)
class LocalizationSample:
    """一个任务的定位对比：模型排序后的候选 vs gold。

    gold 侧刻意不在这里从补丁重算——那是评测器的职责。把已经算好的结果传进来，
    本模块才能保持纯函数，也才能保证 gold 永不回流到推理侧。
    """

    task_id: str
    predicted_files: tuple[str, ...] = ()
    predicted_symbols: tuple[str, ...] = ()
    gold_files: frozenset[str] = frozenset()
    gold_symbols: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空")

    @staticmethod
    def dedupe(order: Sequence[str]) -> tuple[str, ...]:
        """去重但保留排名。

        重复候选如果不折叠，"Top-5" 里出现三次同一个文件会被算成覆盖了三格，
        Recall@K 于是凭空变好——那是对排序质量的一次凭空奖励。
        """
        seen: set[str] = set()
        out: list[str] = []
        for item in order:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return tuple(out)


def _symbol_covers(gold: str, predicted: str) -> bool:
    """gold 是模块级改动时，该模块内的任何 symbol 都算命中。

    02 §17.2："gold patch 只改模块级代码时，以模块 symbol 计"。模块级 gold 记成
    ``pkg.core``、函数级 gold 记成 ``pkg.core.normalize``，于是前缀关系就是那条
    规则本身，而不是一个特例分支。
    """
    return predicted == gold or predicted.startswith(f"{gold}.")


def _require_positive_k(k: int) -> None:
    if k < 1:
        raise ValueError(f"K 必须是正整数，收到 {k}")


def _localization_ratio(
    samples: Sequence[LocalizationSample],
    *,
    select: Callable[[LocalizationSample], tuple[str, ...]],
    golds: Callable[[LocalizationSample], tuple[str, ...]],
    matches: Callable[[str, str], bool],
    k: int,
    require_all: bool = False,
) -> Ratio:
    """没有 gold 定位信息的任务**不进分母**。

    把它们算进去会制造一种假失败：一个没有任何定位目标的任务，Recall 永远是 0，
    而那与模型的定位能力无关。分母因此是"有 gold 定位信息的任务数"，且可见。
    """
    flags: list[bool] = []
    for sample in samples:
        gold_set = golds(sample)
        if not gold_set:
            continue
        top = select(sample)[:k]
        if require_all:
            flags.append(all(any(matches(gold, cand) for cand in top) for gold in gold_set))
        else:
            flags.append(any(matches(gold, cand) for gold in gold_set for cand in top))
    return _ratio(flags)


def file_recall_at_k(samples: Sequence[LocalizationSample], k: int) -> Ratio:
    """gold 修改的文件中**至少一个**出现在 Top-K 文件中的任务比例。"""
    _require_positive_k(k)
    return _localization_ratio(
        samples,
        select=lambda item: LocalizationSample.dedupe(item.predicted_files),
        golds=lambda item: tuple(sorted(item.gold_files)),
        matches=lambda gold, candidate: gold == candidate,
        k=k,
    )


def function_recall_at_k(samples: Sequence[LocalizationSample], k: int) -> Ratio:
    """同 File Recall，但按函数；模块级 gold 以该模块的 symbol 计。"""
    _require_positive_k(k)
    return _localization_ratio(
        samples,
        select=lambda item: LocalizationSample.dedupe(item.predicted_symbols),
        golds=lambda item: tuple(sorted(item.gold_symbols)),
        matches=_symbol_covers,
        k=k,
    )


def localization_exact_at_k(samples: Sequence[LocalizationSample], k: int) -> Ratio:
    """gold 修改的**全部**文件都出现在 Top-K 中的比例。

    多文件任务在这里与单文件任务自然分开：只报对了其中一个文件的任务，Recall
    记命中、Exact 不记。把两者混为一谈会高估定位能力。
    """
    _require_positive_k(k)
    return _localization_ratio(
        samples,
        select=lambda item: LocalizationSample.dedupe(item.predicted_files),
        golds=lambda item: tuple(sorted(item.gold_files)),
        matches=lambda gold, candidate: gold == candidate,
        k=k,
        require_all=True,
    )


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """一个 (task, seed) 运行的全部结论。指标层唯一的输入形态。"""

    task_id: str
    seed: int = 0
    category: str = "uncategorized"

    # ---- 评测结论（三态；None = 没能判定）----
    resolved: bool | None = None
    first_attempt_resolved: bool | None = None
    agent_claims_success: bool = False

    # ---- 补丁形态 ----
    empty_patch: bool = False
    invalid_patch: bool = False

    # ---- 运行形态 ----
    iterations: int | None = None
    stop_reason: str | None = None
    environment_failure: bool = False
    harness_failure: bool = False
    """Harness 自己出错导致这次观测根本没有评到模型。**不**等于环境失败。"""

    verification_mode: VerificationMode = VerificationMode.NONE

    # ---- 归因（Harness Hardening）----
    attribution: Attribution = Attribution.EVALUATION_NOT_RUN
    evaluation_state: EvaluationState = EvaluationState.NOT_RUN
    error_class: str | None = None
    error_origin: str | None = None

    # ---- 成本 ----
    input_tokens: int | None = None
    output_tokens: int | None = None
    llm_calls: int | None = None
    cost: float | None = None
    """``None`` = 费用未知。未知**不等于 0**：把未知当 0，会让"其实没测到成本"
    伪装成"这一批很便宜"。"""

    runtime_seconds: float | None = None
    cost_currency: str = "CNY"

    # ---- 定位（评测侧算出后传入）----
    localization: LocalizationSample | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id 不能为空")
        if self.seed < 0:
            raise ValueError("seed 不能为负")
        for name in ("iterations", "input_tokens", "output_tokens", "llm_calls"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} 不能为负")
        if self.cost is not None and self.cost < 0:
            raise ValueError("cost 不能为负")
        if self.runtime_seconds is not None and self.runtime_seconds < 0:
            raise ValueError("runtime_seconds 不能为负")

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @property
    def has_valid_patch(self) -> bool:
        """产出过可评测的非空合法补丁。"""
        return not self.empty_patch and not self.invalid_patch

    @property
    def over_budget(self) -> bool:
        return self.stop_reason is not None and self.stop_reason.startswith("budget_")


def _check_unique_identity(outcomes: Sequence[TaskOutcome]) -> None:
    """同一 (task_id, seed) 只能有一条结论。

    重复一份不会报错，只会把分母悄悄变大——那同时污染总体比例和条件比例。
    不同 seed 里同一个 task_id 出现多次是**合法**的，所以身份是二元组。
    """
    keys = [(item.task_id, item.seed) for item in outcomes]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"重复的 (task_id, seed): {duplicates[:5]}")


@dataclass(frozen=True, slots=True)
class RepairMetrics:
    """修复类指标。总体比例与条件比例分列，不共用分母。"""

    total_tasks: int
    resolved: Ratio
    first_attempt_resolved: Ratio
    repair_success_rate: Ratio
    self_verified_precision: SelfVerifiedPrecision
    empty_patch_rate: Ratio
    invalid_patch_rate: Ratio
    undetermined_tasks: int
    """评测未能判定的任务数（环境失败等）。它们**仍在**总体分母里。"""

    @property
    def repair_gain(self) -> float | None:
        """Resolved Rate − First Attempt Resolved（02 §17.2）。

        刻意由本对象自己算：调用方拿两个不同口径的数字相减，得到的是一个没有定义
        的量。
        """
        first = self.first_attempt_resolved.value
        final = self.resolved.value
        if first is None or final is None:
            return None
        return final - first

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "resolved_rate": self.resolved.as_dict(),
            "first_attempt_resolved": self.first_attempt_resolved.as_dict(),
            "repair_gain": self.repair_gain,
            "repair_success_rate": self.repair_success_rate.as_dict(),
            "self_verified_precision": {
                "numerator": self.self_verified_precision.numerator,
                "denominator": self.self_verified_precision.denominator,
                "value": self.self_verified_precision.value,
                "claimed_without_verdict": self.self_verified_precision.claimed_without_verdict,
                "evaluated_without_claim": self.self_verified_precision.evaluated_without_claim,
            },
            "empty_patch_rate": self.empty_patch_rate.as_dict(),
            "invalid_patch_rate": self.invalid_patch_rate.as_dict(),
            "undetermined_tasks": self.undetermined_tasks,
        }


def repair_metrics(outcomes: Sequence[TaskOutcome]) -> RepairMetrics:
    """修复指标全集。

    三条最容易写错、且每条都有对应单测的口径：

    * **First Attempt Resolved 用第一轮补丁的独立评测结论**，不是"最终通过就推断
      第一轮也通过"。推断出来的那个数永远是 Resolved Rate 的上界，等于把迭代修复
      的贡献整个抹掉。
    * **Repair Success Rate 是条件比例**：分母 = 第一轮明确未解决的任务。
    * **总体比例的分母含环境失败任务**。
    """
    _check_unique_identity(outcomes)
    first_failed = [item for item in outcomes if item.first_attempt_resolved is False]
    samples = [
        SelfVerifiedSample(
            # 复合身份：一个任务在 3 个 seed 里跑 3 次，就是 3 次独立的"自称 vs
            # 评测"配对。只用 task_id 会让 :func:`self_verified_precision` 的唯一性
            # 检查把这批评成"重复任务"而直接拒绝——那正是它该拒绝的事，只是这批不是。
            task_id=f"{item.task_id}#seed={item.seed}",
            agent_claims_success=item.agent_claims_success,
            evaluation_passed=item.resolved,
        )
        for item in outcomes
    ]
    return RepairMetrics(
        total_tasks=len(outcomes),
        resolved=_ratio([item.resolved is True for item in outcomes]),
        first_attempt_resolved=_ratio([item.first_attempt_resolved is True for item in outcomes]),
        repair_success_rate=Ratio(
            numerator=sum(1 for item in first_failed if item.resolved is True),
            denominator=len(first_failed),
        ),
        self_verified_precision=self_verified_precision(samples),
        empty_patch_rate=_ratio([item.empty_patch for item in outcomes]),
        invalid_patch_rate=_ratio([item.invalid_patch for item in outcomes]),
        undetermined_tasks=sum(1 for item in outcomes if item.resolved is None),
    )


@dataclass(frozen=True, slots=True)
class DistributionMetrics:
    """一组标签的计数及其占比。"""

    total_tasks: int
    counts: dict[str, int]

    @property
    def shares(self) -> dict[str, float]:
        if not self.total_tasks:
            return {}
        return {key: value / self.total_tasks for key, value in sorted(self.counts.items())}

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "counts": dict(sorted(self.counts.items())),
            "shares": {key: round(value, 6) for key, value in self.shares.items()},
        }


def _distribution(
    values: Iterable[str | None], *, missing_label: str, total: int
) -> DistributionMetrics:
    """把一组可空标签聚成计数。``None`` 归入 ``missing_label`` 而不是被丢掉。

    "没有 stop_reason" 本身是一条信息（跑完了、没触发任何停止规则），丢掉它会让
    分布表看起来像凭空少了一批任务。
    """
    counts: dict[str, int] = {}
    for value in values:
        key = value if value is not None else missing_label
        counts[key] = counts.get(key, 0) + 1
    return DistributionMetrics(total_tasks=total, counts=counts)


def reliability_metrics(outcomes: Sequence[TaskOutcome]) -> dict[str, Any]:
    """可靠性：停止原因、环境失败、超预算、无有效补丁、验证模式。"""
    _check_unique_identity(outcomes)
    total = len(outcomes)
    environment = _ratio([item.environment_failure for item in outcomes])
    return {
        "total_tasks": total,
        "stop_reasons": _distribution(
            (item.stop_reason for item in outcomes), missing_label="none", total=total
        ).as_dict(),
        "environment_failures": environment.as_dict(),
        "environment_failure_causes": _distribution(
            (item.stop_reason for item in outcomes if item.environment_failure),
            missing_label="unattributed",
            total=environment.numerator,
        ).as_dict(),
        "over_budget": _ratio([item.over_budget for item in outcomes]).as_dict(),
        "no_valid_patch": _ratio([not item.has_valid_patch for item in outcomes]).as_dict(),
        "verification_modes": _distribution(
            (item.verification_mode.value for item in outcomes),
            missing_label="none",
            total=total,
        ).as_dict(),
    }


@dataclass(frozen=True, slots=True)
class AttributionMetrics:
    """归因分布 + 两套解决率 + 有效评测覆盖率。

    两个比例**必须同时**看，只看一个就会得出错的结论：

    * **端到端解决率**分母 = 全部派发（含 Harness 崩溃、超预算、没评成的）。
      这是"这批任务里有多少真的被修好了"。
    * **条件解决率**分母 = 实际拿到独立评测判定的任务。
      这是"在评测真的发生过的那些任务里，模型修好了多少"——它是唯一可以直接
      当作模型能力读数的一项，且必须带着覆盖率一起报（覆盖率 30% 时的 80%
      条件解决率完全可能是幸存者偏差）。

    ``environment_failures`` 与 ``harness_failures`` 分开：一次 Docker 镜像坏了
    与一次我们自己的代码抛了异常，是两类不同的责任，也不该混进同一个错误率。
    """

    total_tasks: int
    counts: dict[str, int]
    end_to_end_resolved: Ratio
    conditional_resolved: Ratio
    evaluation_coverage: Ratio
    harness_error_rate: Ratio
    environment_error_rate: Ratio
    sandbox_error_rate: Ratio
    plan_rejection_rate: Ratio
    model_output_error_rate: Ratio
    unevaluated_tasks: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "counts": dict(sorted(self.counts.items())),
            "end_to_end_resolved": self.end_to_end_resolved.as_dict(),
            "conditional_resolved": self.conditional_resolved.as_dict(),
            "evaluation_coverage": self.evaluation_coverage.as_dict(),
            "harness_error_rate": self.harness_error_rate.as_dict(),
            "environment_error_rate": self.environment_error_rate.as_dict(),
            "sandbox_error_rate": self.sandbox_error_rate.as_dict(),
            "plan_rejection_rate": self.plan_rejection_rate.as_dict(),
            "model_output_error_rate": self.model_output_error_rate.as_dict(),
            "unevaluated_tasks": self.unevaluated_tasks,
        }


def attribution_metrics(outcomes: Sequence[TaskOutcome]) -> AttributionMetrics:
    """按归因把一次批次切开。纯计数，不做任何"排除后更好看"的处理。"""
    _check_unique_identity(outcomes)
    total = len(outcomes)
    counts: dict[str, int] = {}
    for item in outcomes:
        counts[item.attribution.value] = counts.get(item.attribution.value, 0) + 1

    def _share(*attributions: Attribution) -> Ratio:
        wanted = {item.value for item in attributions}
        return _ratio([item.attribution.value in wanted for item in outcomes])

    evaluated_flags = [item.evaluation_state is EvaluationState.EVALUATED for item in outcomes]
    conditional = [
        item.resolved is True for item, flag in zip(outcomes, evaluated_flags, strict=True) if flag
    ]
    return AttributionMetrics(
        total_tasks=total,
        counts=counts,
        end_to_end_resolved=_ratio([item.resolved is True for item in outcomes]),
        conditional_resolved=_ratio(conditional),
        evaluation_coverage=_ratio(evaluated_flags),
        harness_error_rate=_share(Attribution.HARNESS_ERROR),
        environment_error_rate=_share(Attribution.ENV_ERROR),
        sandbox_error_rate=_share(Attribution.SANDBOX_ERROR),
        plan_rejection_rate=_share(Attribution.MODEL_INVALID_PLAN),
        model_output_error_rate=_share(Attribution.MODEL_OUTPUT_ERROR),
        unevaluated_tasks=sum(1 for flag in evaluated_flags if not flag),
    )


@dataclass(frozen=True, slots=True)
class SummaryStatistics:
    """一组数值分布的三个位置统计量。"""

    count: int
    mean: float | None
    median: float | None
    p90: float | None

    def as_dict(self) -> dict[str, Any]:
        return {"count": self.count, "mean": self.mean, "median": self.median, "p90": self.p90}


def _statistics(values: Sequence[float]) -> SummaryStatistics:
    """mean / median / P90。

    P90 用 **nearest-rank**（取第 ``ceil(0.9n)`` 个观测值），不做插值：迭代次数是
    离散计数，插值会报出"2.7 轮"这种从没人测到的数字，而读者会以为真有过 2.7 轮
    的运行。空输入返回 None，不返回 0。
    """
    if not values:
        return SummaryStatistics(count=0, mean=None, median=None, p90=None)
    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    rank = max(1, min(n, math.ceil(0.9 * n)))
    return SummaryStatistics(count=n, mean=mean, median=float(median), p90=float(ordered[rank - 1]))


def iterations_metrics(outcomes: Sequence[TaskOutcome]) -> SummaryStatistics:
    """迭代次数分布。只统计**记录了**迭代次数的任务，``count`` 即其数量。"""
    _check_unique_identity(outcomes)
    return _statistics([float(item.iterations) for item in outcomes if item.iterations is not None])


@dataclass(frozen=True, slots=True)
class CostMetrics:
    """成本与耗时。每个均值都带着它**实际**的分母。

    分母不是 N 的地方都有原因：缺埋点的任务不能被当成"0 token"计入平均，否则
    平均值会随埋点覆盖率漂移，看起来却像模型变便宜了。
    """

    total_tasks: int
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_tokens: int | None
    total_llm_calls: int | None
    mean_tokens_per_task: float | None
    tokens_tasks_recorded: int
    total_cost: float | None
    mean_cost_per_task: float | None
    mean_cost_per_resolved_task: float | None
    cost_tasks_recorded: int
    cost_tasks_unknown: int
    resolved_tasks_with_cost: int
    cost_currency: str
    mean_runtime_seconds: float | None
    runtime_tasks_recorded: int
    cost_complete: bool
    """``False`` = 至少一个任务的费用没测到，``total_cost`` 因此只是**下界**。"""

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "total_llm_calls": self.total_llm_calls,
            "mean_tokens_per_task": self.mean_tokens_per_task,
            "tokens_tasks_recorded": self.tokens_tasks_recorded,
            "total_cost": self.total_cost,
            "total_cost_is_lower_bound": not self.cost_complete,
            "mean_cost_per_task": self.mean_cost_per_task,
            "mean_cost_per_resolved_task": self.mean_cost_per_resolved_task,
            "cost_tasks_recorded": self.cost_tasks_recorded,
            "cost_tasks_unknown": self.cost_tasks_unknown,
            "resolved_tasks_with_cost": self.resolved_tasks_with_cost,
            "cost_currency": self.cost_currency,
            "mean_runtime_seconds": self.mean_runtime_seconds,
            "runtime_tasks_recorded": self.runtime_tasks_recorded,
            "cost_complete": self.cost_complete,
        }


def _mean(values: Sequence[float]) -> tuple[float | None, int]:
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def cost_metrics(outcomes: Sequence[TaskOutcome]) -> CostMetrics:
    """Token / 费用 / 墙钟。未知数据只从对应均值里剔除，绝不从总体分母里剔除。"""
    _check_unique_identity(outcomes)
    total = len(outcomes)
    currencies = {item.cost_currency for item in outcomes if item.cost is not None}
    if len(currencies) > 1:
        raise ValueError(f"同一批结果里混用了币种: {sorted(currencies)}")

    token_pairs = [
        (item.input_tokens, item.output_tokens)
        for item in outcomes
        if item.input_tokens is not None or item.output_tokens is not None
    ]
    input_total = sum(pair[0] or 0 for pair in token_pairs) if token_pairs else None
    output_total = sum(pair[1] or 0 for pair in token_pairs) if token_pairs else None
    tokens_total = None if input_total is None else input_total + (output_total or 0)
    mean_tokens, tokens_recorded = _mean(
        [float((pair[0] or 0) + (pair[1] or 0)) for pair in token_pairs]
    )

    costs = [item.cost for item in outcomes if item.cost is not None]
    total_cost = sum(costs) if costs else None
    mean_cost, cost_recorded = _mean(costs)
    resolved_costs = [
        item.cost for item in outcomes if item.cost is not None and item.resolved is True
    ]
    mean_cost_resolved, _ = _mean(resolved_costs)

    call_counts = [item.llm_calls for item in outcomes if item.llm_calls is not None]
    runtimes = [item.runtime_seconds for item in outcomes if item.runtime_seconds is not None]
    mean_runtime, runtime_recorded = _mean(runtimes)

    return CostMetrics(
        total_tasks=total,
        total_input_tokens=input_total,
        total_output_tokens=output_total,
        total_tokens=tokens_total,
        total_llm_calls=sum(call_counts) if call_counts else None,
        mean_tokens_per_task=mean_tokens,
        tokens_tasks_recorded=tokens_recorded,
        total_cost=total_cost,
        mean_cost_per_task=mean_cost,
        mean_cost_per_resolved_task=mean_cost_resolved,
        cost_tasks_recorded=cost_recorded,
        cost_tasks_unknown=total - cost_recorded,
        resolved_tasks_with_cost=len(resolved_costs),
        cost_currency=next(iter(currencies), "CNY"),
        mean_runtime_seconds=mean_runtime,
        runtime_tasks_recorded=runtime_recorded,
        cost_complete=cost_recorded == total,
    )


#: 03 §19 要求的三个 K 值。
DEFAULT_K: tuple[int, ...] = (1, 5, 10)


@dataclass(frozen=True, slots=True)
class SuiteMetrics:
    """一批 (task, seed) 结果的完整指标。"""

    total_tasks: int
    categories: dict[str, int]
    seeds: tuple[int, ...]
    repair: RepairMetrics
    localization: dict[str, dict[str, Any]]
    iterations: SummaryStatistics
    cost: CostMetrics
    reliability: dict[str, Any]
    attribution: AttributionMetrics

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "categories": dict(sorted(self.categories.items())),
            "seeds": list(self.seeds),
            "repair": self.repair.as_dict(),
            "localization": self.localization,
            "iterations": self.iterations.as_dict(),
            "cost": self.cost.as_dict(),
            "reliability": self.reliability,
            "attribution": self.attribution.as_dict(),
        }


def summarize(
    outcomes: Sequence[TaskOutcome], *, localization_k: Sequence[int] = DEFAULT_K
) -> SuiteMetrics:
    """一次 benchmark 的全套指标。纯函数，不碰任何外部状态。"""
    _check_unique_identity(outcomes)
    samples = [item.localization for item in outcomes if item.localization is not None]
    localization: dict[str, dict[str, Any]] = {}
    for k in localization_k:
        localization[f"k{k}"] = {
            "file_recall": file_recall_at_k(samples, k).as_dict(),
            "function_recall": function_recall_at_k(samples, k).as_dict(),
            "localization_exact": localization_exact_at_k(samples, k).as_dict(),
        }
    categories: dict[str, int] = {}
    for item in outcomes:
        categories[item.category] = categories.get(item.category, 0) + 1
    return SuiteMetrics(
        total_tasks=len(outcomes),
        categories=categories,
        seeds=tuple(sorted({item.seed for item in outcomes})),
        repair=repair_metrics(outcomes),
        localization=localization,
        iterations=iterations_metrics(outcomes),
        cost=cost_metrics(outcomes),
        reliability=reliability_metrics(outcomes),
        attribution=attribution_metrics(outcomes),
    )


def per_seed_metrics(
    outcomes: Sequence[TaskOutcome], *, localization_k: Sequence[int] = DEFAULT_K
) -> dict[int, SuiteMetrics]:
    """按 seed 分别计算。

    02 §17.5 要的是"每组配置 3 个 seed，报告均值 ± 标准差"，那必须先有每个 seed
    自己的数字。把 3 个 seed 摊平成 3N 个任务，得到的是**一次大运行**的比例，不是
    三次独立重复实验——标准差就没有定义良好的对象了。
    """
    grouped: dict[int, list[TaskOutcome]] = {}
    for item in outcomes:
        grouped.setdefault(item.seed, []).append(item)
    return {
        seed: summarize(items, localization_k=localization_k)
        for seed, items in sorted(grouped.items())
    }


def aggregate_metric_values(
    metrics: Sequence[SuiteMetrics],
) -> dict[str, dict[str, float | int | None]]:
    """跨 seed 的均值与标准差（02 §17.5）。

    只对**同分母口径**的比值取均值：每个 seed 的任务集相同，所以每个 seed 的
    Resolved Rate 是独立重复，它们的均值才是"均值 ± 标准差"里的均值。
    用样本标准差（``ddof=1``）；只有 1 个 seed 时标准差无定义，报 ``None`` 而不是
    0——报 0 会被读成"多次运行结果完全一致"。
    """
    selectors: dict[str, Callable[[SuiteMetrics], float | None]] = {
        "resolved_rate": lambda item: item.repair.resolved.value,
        "first_attempt_resolved": lambda item: item.repair.first_attempt_resolved.value,
        "repair_gain": lambda item: item.repair.repair_gain,
        "repair_success_rate": lambda item: item.repair.repair_success_rate.value,
        "self_verified_precision": lambda item: item.repair.self_verified_precision.value,
        "empty_patch_rate": lambda item: item.repair.empty_patch_rate.value,
        "invalid_patch_rate": lambda item: item.repair.invalid_patch_rate.value,
        "mean_iterations": lambda item: item.iterations.mean,
        "mean_tokens_per_task": lambda item: item.cost.mean_tokens_per_task,
        "mean_cost_per_task": lambda item: item.cost.mean_cost_per_task,
    }
    out: dict[str, dict[str, float | int | None]] = {}
    for name, select in selectors.items():
        values = [value for value in (select(item) for item in metrics) if value is not None]
        if not values:
            out[name] = {"mean": None, "stddev": None, "seeds": 0}
            continue
        mean = sum(values) / len(values)
        stddev: float | None = None
        if len(values) > 1:
            variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
            stddev = math.sqrt(variance)
        out[name] = {"mean": mean, "stddev": stddev, "seeds": len(values)}
    return out


@dataclass(frozen=True, slots=True)
class TaskOutcomeSet:
    """一批结果连同它的**完整性声明**。

    ``declared_tasks × seeds`` 是应当产生的观测数，``len(outcomes)`` 是实际产生的。
    差值里装着预算截停、崩溃、被跳过的任务；不写出来，一份"9/9 通过"的报告可能
    其实只跑了三分之一。
    """

    outcomes: tuple[TaskOutcome, ...] = ()
    declared_tasks: int = 0
    seeds_requested: tuple[int, ...] = ()
    completed: int = 0
    not_executed: int = 0
    budget_blocked: int = 0
    evaluation_errors: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", tuple(self.outcomes))

    @property
    def expected_observations(self) -> int:
        return self.declared_tasks * len(self.seeds_requested)

    @property
    def complete(self) -> bool:
        return (
            self.not_executed == 0
            and self.budget_blocked == 0
            and len(self.outcomes) == self.expected_observations
        )

    @property
    def integrity(self) -> str:
        """``full`` / ``partial``——报告的标题级别就取决于它。"""
        return "full" if self.complete else "partial"

    def metrics(self) -> SuiteMetrics:
        return summarize(self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared_tasks": self.declared_tasks,
            "seeds_requested": list(self.seeds_requested),
            "expected_observations": self.expected_observations,
            "recorded_observations": len(self.outcomes),
            "completed": self.completed,
            "not_executed": self.not_executed,
            "budget_blocked": self.budget_blocked,
            "evaluation_errors": self.evaluation_errors,
            "complete": self.complete,
            "integrity": self.integrity,
        }
