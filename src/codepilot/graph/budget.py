"""预算检查（02 §15.3、G-04、Phase 11 闭环预算）。

六类预算 + 审查专属额度全部从 **state 里已有的累计量**推导，而不是在本模块另存
一份计数器：``iterations`` 来自 ``attempts``、``test_runs`` 来自
``state.test_runs``、token/费用来自 ``state.usage``、墙钟来自 ``state.started_at``、
审查次数来自 ``state.review_calls``。

这样做的理由很具体：中断恢复时唯一可信的账本只有 checkpoint 里的 state。如果
预算计数器活在内存里，进程重启后它就归零了，恢复出来的 run 可以无限烧钱——
这是 G-04 明确禁止的行为（"跨进程恢复时不能重新清零累计预算"）。

``max_iterations`` 的口径（02 §15.3 注释："``repair_patch`` 最多执行次数"）：
**首轮 ``generate_patch`` 不计入迭代次数**。``attempts`` 有 N 条时，已执行的
``repair_patch`` 次数是 ``max(N - 1, 0)``，因此 ``max_iterations=5`` 允许
1 次生成 + 5 次修复 = 最多 6 条 Attempt。这一条有专门的 off-by-one 测试。

## 四种预算的分工（Phase 11）

| 预算 | 上限 | 计数口径 | 谁会被它拦 |
| --- | --- | --- | --- |
| 工具调用 | ``max_tool_calls_per_iteration`` | ``state.tool_calls``（**只由编码阶段整体覆盖**） | 当前这一轮的编码循环 |
| 修复迭代 | ``max_iterations`` | ``len(attempts) - 1`` | ``repair_patch`` 派发 |
| 模型/费用 | ``max_total_tokens`` / ``max_cost`` | ``state.usage`` + 派发层预扣 | 每个节点入口 + 闭环预留 |
| 验证/审查 | ``max_test_runs`` / ``max_review_calls`` | ``state.test_runs`` / ``state.review_calls`` | 测试与 ``review`` 节点 |

前两类是**局部额度**：它们只说明"这一轮编码/修复不能再做了"，**不**等于整个任务
结束。后两类里的费用与审查额度是**全局或专属**的，不能被局部的编码额度顶掉。
首次真实 GLM 运行暴露的缺陷正是把这两层写成了一层：Coder 撞到
``max_tool_calls_per_iteration`` 后 ``_run_coder`` 直接把 run 写成终态，路由于是
绕过 PatchValidator、目标测试与 PatchReviewer 直奔 finalize。

## 预留的作用范围：所有排在审查之前的模型调用

:data:`RESERVATION_BEARING_ACTIVITIES` 是 ``{LLM, CODEGEN, REPAIR}``。第一轮只收了
``CODEGEN``/``REPAIR``，但真实闭环里排在审查之前的还有 Diagnose（以及任何
``Activity.LLM`` 节点）：诊断把 headroom 花光之后，``review`` 入口只能如实记
"未执行"——现象与"审查被编码烧光"完全一样。所以预留在**统一预算准入**
（节点入口的 :meth:`BudgetManager.evaluate`）上生效，而不是给每个节点各配一套算法。
``TEST`` 与 ``REVIEW`` 是预留的**受益方**，用真实硬上限，绝不因为预留而少花一分钱。

## 预留为什么按 Reviewer 的单价，而不是按账本平均单价

账本平均单价 = ``已花费用 / 已花 token``，它的口径是**已经跑过的那些调用**。而
Coder、Planner 与 Reviewer 常常是不同模型（不同输入价、输出价、缓存价），平均单价
既不代表 Reviewer，也不代表"最贵的那个"。只换掉闭环的走向、不换掉这个口径，症状
会原样复发：预留按便宜的编码模型估，审查按贵的审查模型花。

所以预留**优先**用 Reviewer 实际绑定模型的单价
（``LLMConfig.resolve_role("reviewer")`` → :func:`codepilot.llm.cost.compute_cost`），
复用同一套单价、缓存价、币种换算与计量单位，不另建第二套账。只有拿不到单价时
（脚本化 provider、FakeLLM、角色未映射）才退回账本平均单价；两种口径都给不出数字
且设了硬费用上限时 fail closed —— 未知费用不得当成零。

## 第五道闸门：测试额度的闭环准入（DA-024）

token/费用预留管不住第三次真实单题运行（glm14）暴露的那一类缺口：那一次 ¥0.196 远未触顶
¥10，把运行判死的是 ``max_test_runs=12`` —— 两轮"目标测试 + L2 + L3"加上基线两次与编码
阶段自己调用的 ``run_tests`` 就把它花光了，于是第二轮候选补丁的回归只剩半截、审查从未发生。

测试次数不能像 token 那样"预留"：它是离散次数，而编码阶段的 ``run_tests`` 与闭环的目标测试、
回归**共用同一个计数器**，谁先花掉就是谁的。所以这里做两件事
（:meth:`BudgetManager.closure_test_plan`）：

* **准入**：剩余次数撑不起这一版候选的闭环（补丁校验 0 次 + 目标测试 + 当前配置要求的回归
  层级）时，修复轮**在启动之前**被拒，``stop_reason=budget_closure_test_runs``。
  已经审过的补丁、审查留下的 high 问题与回归结论原样保留——半途而废的新候选比没有新候选
  更糟，因为它会产出一次注定无法闭环的验证。
* **份额**：可行时把编码阶段本轮可自己调用的 ``run_tests`` 压到
  ``剩余 - 闭环所需 - flaky 复跑预留``，让闭环那几次物理上还在。只收紧，绝不放松
  ``max_test_runs``，也绝不为了省额度把 FULL 回归降级成 SAMPLED（抽样与否是覆盖范围的
  决定，不是额度的决定）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from codepilot.clock import utcnow
from codepilot.llm.config import LLMConfig, ModelPrice
from codepilot.llm.cost import compute_cost
from codepilot.schemas.budget import ClosureAdmission, RepairBudget
from codepilot.schemas.enums import StopReason
from codepilot.schemas.state import RepairState

__all__ = [
    "ACTIVITY_CATEGORIES",
    "CLOSURE_FLAKY_RETRY_RESERVE",
    "CLOSURE_PATCH_VALIDATION_RUNS",
    "CLOSURE_REGRESSION_LEVEL_RUNS",
    "CLOSURE_TARGET_TEST_RUNS",
    "RESERVATION_BEARING_ACTIVITIES",
    "REVIEWER_ROLE",
    "REVIEW_CONTEXT_TOKENS",
    "REVIEW_OUTPUT_TOKENS",
    "Activity",
    "BudgetConfigurationError",
    "BudgetEvaluation",
    "BudgetManager",
    "Category",
    "ClosureReservation",
    "ClosureTestPlan",
    "outstanding_in_budget_currency",
]

Clock = Callable[[], datetime]

#: :class:`codepilot.agents.reviewer.PatchReviewer` 的上下文预算与输出上限。
#: 预留量必须用**同一个数字**，否则"给审查留了多少"与审查真实会花多少无关。
REVIEW_CONTEXT_TOKENS = 24_000
REVIEW_OUTPUT_TOKENS = 4_096

#: 闭环准入的算术常数（DA-024）。口径全部对齐 :class:`codepilot.graph.regression.RegressionRunner`
#: 与 :func:`codepilot.graph.nodes.run_target_tests` 里**实际会发生**的 ``run_tests`` 调用次数，
#: 而不是"大概三四次"：判定要的是可复算的数，不是印象。
CLOSURE_PATCH_VALIDATION_RUNS = 0
"""PatchValidator 是纯静态校验，一次容器执行都不花。"""

CLOSURE_TARGET_TEST_RUNS = 1
"""目标测试（L1）一次 ``run_tests``。范围多大都只算一次：计数口径是**调用次数**。"""

CLOSURE_REGRESSION_LEVEL_RUNS = 1
"""每个回归层级（L2 影响面、L3 全量/抽样）各一次 ``run_tests``。抽样不省次数。"""

CLOSURE_FLAKY_RETRY_RESERVE = 2
"""回归里每出现一条疑似新失败，判定"是不是 flaky"要复跑两次。"""

#: 预留定价所依据的 Agent 角色名。它必须与装配层构造 Reviewer 时
#: ``registry.for_role(...)`` 用的角色同名，否则"按审查模型的单价预留"会变成
#: "按别的模型的单价预留"。
REVIEWER_ROLE = "reviewer"


class Category(StrEnum):
    """预算的执行身份：七类，各自独立计数。"""

    ITERATIONS = "iterations"
    TEST_RUNS = "test_runs"
    TOOL_CALLS = "tool_calls"
    REVIEW_CALLS = "review_calls"
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
    #: 独立审查（专属额度，**不**受编码阶段的工具调用计数影响）。
    REVIEW = "review"


#: 会被"闭环预留"削减上限的活动：**一切可能先于 Reviewer 花钱的模型调用**。
#: 首轮修复只收紧 ``CODEGEN``/``REPAIR``，但真实闭环里 Diagnose（以及任何
#: ``Activity.LLM`` 节点）同样排在审查之前烧钱：诊断把 headroom 花光之后，
#: ``review`` 入口只能如实记"未执行"。预留必须落在**派发之前**拦住它们，否则
#: "给审查留钱"这句话在最常见的一条失败路径上不成立。
#: 验证与审查是预留的**受益方**：它们用的是真实硬上限，审查正是来花这笔预留的。
RESERVATION_BEARING_ACTIVITIES: frozenset[Activity] = frozenset(
    {Activity.LLM, Activity.CODEGEN, Activity.REPAIR}
)

#: 判定优先级：先结构性上限（还能不能再修一轮），再外部副作用上限（测试次数），
#: 然后是单轮消耗（工具/审查调用），最后才是累计消耗（token / 费用 / 墙钟）。
#: 固定顺序的意义在于 ``stop_reason`` 必须可复现：同一状态两次判定给出同一个
#: 原因，否则路径测试无法断言。
_PRIORITY: tuple[Category, ...] = (
    Category.ITERATIONS,
    Category.TEST_RUNS,
    Category.TOOL_CALLS,
    Category.REVIEW_CALLS,
    Category.TOKENS,
    Category.COST,
    Category.TIME,
)

_STOP_REASONS: dict[Category, StopReason] = {
    Category.ITERATIONS: StopReason.BUDGET_ITERATIONS,
    Category.TEST_RUNS: StopReason.BUDGET_TEST_RUNS,
    Category.TOOL_CALLS: StopReason.BUDGET_TOOL_CALLS,
    Category.REVIEW_CALLS: StopReason.BUDGET_REVIEW_CALLS,
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
    Activity.REVIEW: (Category.REVIEW_CALLS, Category.TOKENS, Category.COST, Category.TIME),
}


@dataclass(frozen=True, slots=True)
class ClosureReservation:
    """编码派发前必须留给"校验 → 测试 → 审查（→ 有界修复 → 复审）"的额度。

    它**不是**记账：预留从不写入 ``state.usage``，因此不可能重复计费。它只做一件事
    ——把先于审查花钱的模型调用（:data:`RESERVATION_BEARING_ACTIVITIES`）的可用上限
    压低，让编码与诊断物理上花不掉审查要用的那部分。

    ``estimated=False`` 表示现有费用模型给不出可信数字：既没有可用的模型单价
    （缺失或币种无法换算），账本里也没有可参考的实测单价。此时消耗型活动**必须**
    被拦下：把未知费用当成零，等于让闭环在最贵的那种运行里最先饿死。

    ``basis`` 记录这个数字是**怎么来的**（``reviewer-model:<别名>`` /
    ``observed-unit`` / ``unknown`` / ``no-reviewer``）。预留口径决定一次真实运行
    会不会在派发前被拦住，事后只看一个金额看不出该去核对哪份单价配置。
    """

    tokens: int
    cost: float
    review_calls: int
    estimated: bool
    basis: str = ""

    def summary(self) -> dict[str, str]:
        return {
            "tokens": f"{self.tokens}",
            "cost": f"{self.cost:.6f}",
            "review_calls": str(self.review_calls),
            "estimated": str(self.estimated),
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class ClosureTestPlan:
    """一版候选补丁的验证闭环**至少**要几次测试执行、当时还剩几次（DA-024）。

    计数口径与 :class:`codepilot.graph.deps.AccountingSandbox` 完全一致：一次
    ``run_tests`` **调用**算一次，不管这次调用圈进了一条还是七百七十九条测试。
    所以 ``required`` 说的是"闭环还要跨几次容器"，不是"还要跑几个用例"。

    为什么要单独算而不是交给 ``Activity.TEST`` 的入口检查：入口检查只在额度**已经
    花完**之后拦，而一次修复轮的消耗顺序是"编码自己可能跑几次 → 目标测试 → 回归
    两层"。真实运行（glm14）实测的形状正是编码把最后的余额花在 ``run_tests`` 工具上，
    于是第二轮候选的 L3 被拦在门外、审查从未发生——补丁已经改了，验证却半身不遂。
    准入判定的作用点因此有两个：**剩余不够 required 时整轮不启动**；够的时候把编码
    阶段自己能花的份额先扣出来（:attr:`coder_share`），让 required 这几次物理上还在。

    ``retry_reserve`` 刻意**不**进 ``required``：复跑只在真的出现疑似新失败时才发生，
    把它算进准入门槛会把"本来能走完闭环的一轮"判成不可行，而那种情况下回归不完整
    本来就已被 :func:`codepilot.graph.routing.review_preconditions` 如实拦在审查之外。
    它进的是编码阶段的可用份额——也就是"能省的额度先从模型的手里省，不从闭环里省"。
    """

    runs_spent: int
    allowance: int
    target_runs: int
    impact_runs: int
    full_runs: int
    retry_reserve: int = CLOSURE_FLAKY_RETRY_RESERVE

    @property
    def required(self) -> int:
        """闭环最少需要的测试执行次数（含补丁校验那一档，取 0）。"""
        return CLOSURE_PATCH_VALIDATION_RUNS + self.target_runs + self.impact_runs + self.full_runs

    @property
    def remaining(self) -> int:
        return max(self.allowance - self.runs_spent, 0)

    @property
    def feasible(self) -> bool:
        """剩余是否撑得起 ``required``。**恰好等于**时可行：判定用的是"还要几次"。"""
        return self.remaining >= self.required

    @property
    def coder_share(self) -> int:
        """编码阶段本轮最多可以自己跑几次测试；不足时为 0，绝不侵占闭环那几次。"""
        return max(self.remaining - self.required - self.retry_reserve, 0)

    def detail(self) -> str:
        parts = [
            f"patch validation needs {CLOSURE_PATCH_VALIDATION_RUNS} container run(s)",
            f"target tests need {self.target_runs}",
            f"regression L2 needs {self.impact_runs}",
            f"regression L3 needs {self.full_runs}",
        ]
        verdict = (
            f"a repair round can complete its closure (coding stage capped at "
            f"{self.coder_share} self-triggered run(s), "
            f"{self.retry_reserve} kept for flaky re-runs)"
            if self.feasible
            else (
                "the repair round is refused before any spend: it would leave a new candidate "
                "patch without the verification its review and success claim require"
            )
        )
        return (
            f"test runs: {self.runs_spent} spent of {self.allowance}, {self.remaining} left; "
            f"closure requires {self.required} ({', '.join(parts)}) → {verdict}"
        )

    def record(self, *, trigger: str) -> ClosureAdmission:
        """落成可审计读数：与 :meth:`detail` 同一句话，报告与事件不再各算一遍。"""
        return ClosureAdmission(
            feasible=self.feasible,
            runs_spent=self.runs_spent,
            allowance=self.allowance,
            required=self.required,
            retry_reserve=self.retry_reserve,
            target_runs=self.target_runs,
            impact_runs=self.impact_runs,
            full_runs=self.full_runs,
            coder_share=self.coder_share,
            trigger=trigger,
            detail=self.detail(),
        )


@dataclass(frozen=True, slots=True)
class BudgetEvaluation:
    """一次预算判定的完整结果。

    ``exhausted`` 是**全部**超限类别（按优先级），不是只有第一个：只报第一个
    会把"token 和墙钟同时超了"压成一个原因，报告里就看不出真实缺口了。

    ``reserved`` 是本次判定里被闭环预留削减掉的那部分上限；``limit`` 已经是
    **净去预留之后**的数字，所以 ``usage`` 里的对比可以直接读，不必再减一次。
    """

    primary: StopReason | None
    exhausted: tuple[StopReason, ...]
    categories: tuple[Category, ...]
    usage: tuple[tuple[Category, float, float], ...]
    reserved: tuple[tuple[Category, float], ...] = ()

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
        if self.reserved:
            out["reserved"] = ",".join(
                f"{category.value}={value:g}" for category, value in self.reserved
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
        "max_review_calls": budget.max_review_calls,
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


def _reviewer_price(config: LLMConfig | None) -> tuple[ModelPrice, str, dict[str, float]] | None:
    """取 Reviewer **实际绑定模型**的单价，返回 ``(单价, 别名, 汇率)``。

    拿不到就返回 ``None``：角色未映射、别名不存在、单价缺失或不完备（缺输入价或
    输出价）。这里刻意不做任何"用一个差不多的模型价格代替"的兜底——预留估错方向
    比预留估不出来更难发现，因为前者会让闭环在需要钱的时候安静地没钱。
    """
    if config is None:
        return None
    try:
        spec = config.resolve_role(REVIEWER_ROLE)
    except KeyError:
        return None
    price = spec.price
    if price is None or not price.is_complete:
        return None
    return price, spec.alias, config.exchange_rates


def _priced_reservation(
    budget: RepairBudget,
    config: LLMConfig | None,
    *,
    tokens: int,
    calls: int,
) -> tuple[float, str] | None:
    """按审查模型的单价给出预留费用；单价或币种不可靠时返回 ``None``。

    三条口径都写在代码里而不是靠读者记住：

    * **币种**：预算上限（``budget.max_cost``）按 ``budget_currency`` 读，所以预留
      必须先换算到同一个币种。缺汇率时 :func:`codepilot.llm.cost.convert` 按 1.0
      处理，会把 7 倍差额算成"价格差不多"——这种数字宁可当作估不出来。
    * **缓存价不折价**：审查上下文命中缓存是**最好情况**，预留要担保的是最坏情况。
      因此全额按输入价计，``cached_input_per_mtok`` 只用于说明"实际大概会花更少"。
    * **超出部分按较贵的单价**：观测到的单次调用规模大于审查声明的预算时，多出来的
      token 可能是输入也可能是输出，取两者单价的较大者，不去猜是哪一种。
    """
    priced = _reviewer_price(config)
    if priced is None:
        return None
    price, alias, rates = priced
    assert price.input_per_mtok is not None  # is_complete 已保证
    assert price.output_per_mtok is not None
    target = budget.budget_currency
    if price.currency != target and price.currency not in rates:
        return None
    per_call = compute_cost(
        price=price,
        input_tokens=REVIEW_CONTEXT_TOKENS,
        output_tokens=REVIEW_OUTPUT_TOKENS,
        cached_input_tokens=0,
        target_currency=target,
        exchange_rates=rates,
    )
    declared = calls * (REVIEW_CONTEXT_TOKENS + REVIEW_OUTPUT_TOKENS)
    extra = max(tokens - declared, 0)
    steeper = max(price.input_per_mtok, price.output_per_mtok)
    extra_cost = (
        compute_cost(
            price=ModelPrice(
                currency=price.currency, input_per_mtok=steeper, output_per_mtok=steeper
            ),
            input_tokens=extra,
            output_tokens=0,
            target_currency=target,
            exchange_rates=rates,
        ).amount
        if extra
        else 0.0
    )
    return calls * per_call.amount + extra_cost, f"reviewer-model:{alias}"


def outstanding_in_budget_currency(
    outstanding: tuple[float, str] | None,
    *,
    target: str,
    exchange_rates: Mapping[str, float] | None = None,
) -> float | None:
    """把派发层**已预扣但尚未结算**的费用换算到预算币种。

    返回 ``None`` 表示"这笔钱还估不出预算币种里的数值"：币种与预算不同又缺汇率。
    :func:`codepilot.llm.cost.convert` 缺汇率时按 1.0 处理，会把 USD/CNY 的七倍差额
    读成"差不多"，所以这里把它当作**未知**交回调用方，由调用方按硬预算的
    fail-closed 口径处理，而不是拿一个错方向的数字继续派发。

    ``outstanding`` 为 ``None`` 时返回 ``0.0``：没有活动账本（未启用逐请求预留）就是
    没有预扣，这与"有一笔预扣但换算不了"是两件事，必须区分开。
    """
    if outstanding is None:
        return 0.0
    amount, currency = outstanding
    if amount < 0:
        raise BudgetConfigurationError(f"预扣费用不能为负：{amount}")
    if amount == 0 or currency == target:
        return amount
    rate = (exchange_rates or {}).get(currency)
    return None if rate is None else amount * rate


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
        """当前这一轮**编码阶段**的 tool 调用数。

        以"最后一条已执行完测试的 Attempt"为分界：``tool_calls`` 由生成节点整体
        覆盖，所以恢复或重跑同一轮不会把上一轮的工具数叠加进来。

        它是**局部额度**：Planner 与 Reviewer 都不写这个字段，因此编码阶段的工具
        消耗不会占用审查的专属额度（``review_calls`` / ``max_review_calls``）。
        """
        return len(state.tool_calls)

    def review_calls_used(self, state: RepairState) -> int:
        """已经真正执行过的独立审查次数（重放复用不计数，见 ``nodes.review``）。"""
        return state.review_calls

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
            Category.REVIEW_CALLS: (
                float(self.review_calls_used(state)),
                float(budget.max_review_calls),
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

    # ---------------------------------------------------------- 闭环预留

    def closure_reservation(
        self,
        state: RepairState,
        *,
        reviewer_available: bool,
        pending_review_calls: int,
        reviewer_config: LLMConfig | None = None,
        simulated_provider: bool = False,
    ) -> ClosureReservation:
        """先于审查花钱的模型调用必须留给"校验 → 测试 → 审查（→ 有界修复 → 复审）"的额度。

        * token：每次审查按 :data:`REVIEW_CONTEXT_TOKENS` + :data:`REVIEW_OUTPUT_TOKENS`
          计（这是 Reviewer 自己声明的上下文预算，见 :mod:`codepilot.agents.reviewer`）；
          已经观测到的"平均每调用 token"更大时按更大者计——预留取**较坏的那个**，
          因为预留不足正是本次要修的缺陷。
        * 费用：**优先**按 Reviewer 实际绑定模型的输入/输出单价折算到预算币种
          （:func:`_priced_reservation`，复用 :func:`codepilot.llm.cost.compute_cost`
          与配置的汇率，不另建计费体系）；拿不到单价时退回账本实测单价
          ``state.usage.cost / state.usage.total_tokens``；两者都不成立时
          ``estimated=False``（未知费用不得记成零）。账本平均单价只代表"已经跑过的
          调用"，在编码与审查用不同模型时**不是**一个保守数字，所以只作回退。
        * 审查次数：``pending_review_calls`` 由 :func:`codepilot.graph.routing.pending_review_calls`
          按状态机算出——**审查已经做完、审查专属额度已经花完、run 已进终态**时它是
          0，预留随之归零，不会永久占用预算。没有 Reviewer 时预留 0 次：审查会如实
          记录为未执行，不该为它空占额度。
        * ``simulated_provider``：脚本化 / FakeLLM 批次**没有货币费用**。账本为空时这笔
          费用是"已知的 0"，不是"估不出来"；不区分就会让每个模拟批次在第一个 LLM 节点
          被 fail closed 拦住（见下面的边界注释）。真付费 provider 一律传 ``False``，
          没有任何价格依据时照旧 fail closed。

        迭代预算与测试次数**不**在这里预留：``max_iterations`` 已经由 ``check_budget``
        在每次修复派发前判定，测试次数在每个测试节点入口判定（那是既有的安全降级，
        补丁会留在"未验证"状态并被如实报告）。费用与 token 是不可回收的支出，
        只有它们必须在派发前就留出 headroom。
        """
        if not reviewer_available:
            return ClosureReservation(
                tokens=0, cost=0.0, review_calls=0, estimated=True, basis="no-reviewer"
            )
        if pending_review_calls <= 0:
            return ClosureReservation(
                tokens=0, cost=0.0, review_calls=0, estimated=True, basis="review-not-pending"
            )
        calls = pending_review_calls
        per_call = float(REVIEW_CONTEXT_TOKENS + REVIEW_OUTPUT_TOKENS)
        usage = state.usage
        if usage.llm_calls > 0 and usage.total_tokens > 0:
            per_call = max(per_call, usage.total_tokens / usage.llm_calls)
        tokens = int(calls * per_call)
        priced = _priced_reservation(state.budget, reviewer_config, tokens=tokens, calls=calls)
        if priced is not None:
            cost, basis = priced
            return ClosureReservation(
                tokens=tokens, cost=cost, review_calls=calls, estimated=True, basis=basis
            )
        # 回退到账本实测单价。能估算的前提是"账本里没有未知费用调用，且要么已经有过
        # 一次调用可参考、要么这个 provider 本来就是**模拟**的"。
        #
        # 观测到 0 token / 0 费用是**合法**读数（FakeLLM 与免费额度就这么记账），把它
        # 当成"估不出来"会让所有零成本运行在派发前被误阻断 —— 首轮真实 smoke 就是这样
        # 失败的。预留作用范围扩到 ``Activity.LLM`` 之后，同一条边界会以另一种形态复现：
        # "**一次调用都还没有**"发生在 run 的第一个 LLM 节点上，于是每个模拟批次在
        # ``understand_issue`` 就被拦住（实测 ``SMOKE_COMPLETED_NO_PATCH``）。
        #
        # 判据因此按**费用的来源**收口：
        # * ``cost_unknown_calls > 0`` → 费用真的是下界，必须 fail closed；
        # * 账本为空但 provider 是模拟的 → 费用**已知的就是 0**，不是"估不出来"；
        # * 账本为空且是真付费 provider → 没有任何价格依据，继续 fail closed。
        estimated = usage.cost_unknown_calls == 0 and (usage.llm_calls > 0 or simulated_provider)
        if simulated_provider and usage.llm_calls == 0 and usage.cost_unknown_calls == 0:
            return ClosureReservation(
                tokens=tokens,
                cost=0.0,
                review_calls=calls,
                estimated=True,
                basis="simulated-zero-cost",
            )
        unit = usage.cost / usage.total_tokens if estimated and usage.total_tokens else 0.0
        cost = tokens * unit if estimated else float("inf")
        return ClosureReservation(
            tokens=tokens,
            cost=cost,
            review_calls=calls,
            estimated=estimated,
            basis="observed-unit" if estimated else "unknown",
        )

    def closure_test_plan(
        self,
        state: RepairState,
        *,
        target_tests_planned: bool,
        impact_level_expected: bool,
        full_level_expected: bool,
        runs_spent: int | None = None,
    ) -> ClosureTestPlan:
        """这一版候选补丁的闭环还要几次测试执行，当时还剩几次（DA-024 的准入算式）。

        三个 ``*_expected`` 由调用方按**当前配置要求的验证范围**传入，而不是由本函数
        去猜：目标测试是否存在取自 :func:`codepilot.graph.routing.selected_target_tests`，
        回归层级取自 :class:`~codepilot.graph.regression.RegressionRunner` 自己的判定条件
        （有没有可信基线、L3 全量次数是否已到上限）。把这套条件抄第二份进预算层，
        迟早会和回归实现跑出两个答案——那正是"准入以为要跑、实际不跑"的漂移。

        ``runs_spent`` 默认取 ``state.test_runs``；节点里应传**实时**计数
        （``deps.test_runs``），因为 ``state.test_runs`` 只在节点返回时才落账。

        抽样（``regression_mode=sampled``）**不省次数**：L3 仍是一次 ``run_tests``。
        所以准入不足从来不是"降级成抽样就行"的理由（那是覆盖范围的决定，不是额度的决定）。
        """
        spent = state.test_runs if runs_spent is None else runs_spent
        if spent < 0:
            raise BudgetConfigurationError(f"累计测试执行次数不能为负：{spent}")
        return ClosureTestPlan(
            runs_spent=spent,
            allowance=state.budget.max_test_runs,
            target_runs=CLOSURE_TARGET_TEST_RUNS if target_tests_planned else 0,
            impact_runs=CLOSURE_REGRESSION_LEVEL_RUNS if impact_level_expected else 0,
            full_runs=CLOSURE_REGRESSION_LEVEL_RUNS if full_level_expected else 0,
        )

    # ------------------------------------------------------------------ 判定

    def evaluate(
        self,
        state: RepairState,
        activity: Activity,
        *,
        reservation: ClosureReservation | None = None,
        outstanding: float = 0.0,
    ) -> BudgetEvaluation:
        """按固定优先级判定该活动是否已无预算。

        边界口径：**已用 >= 上限 即视为耗尽**。上限是"最多允许消耗这么多"，
        所以刚好花完时不再允许新的消耗，而不是允许第 limit+1 次。

        ``reservation`` 把"验证与审查必须留下的额度"从可用上限里扣掉；作用范围是
        :data:`RESERVATION_BEARING_ACTIVITIES`（先于审查花钱的模型调用）。停止规则
        判定不传预留——它判的是"还要不要再修一轮"，而闭环额度要在真正派发时才起作用。
        预留只会收紧派发，绝不放松硬上限。

        ``outstanding`` 是派发层**已预扣但尚未结算**的费用（预算币种）。它先加进
        费用读数，再扣预留，于是同一段钱不会被算两遍：预扣是"已经承诺出去的钱"，
        预留是"还不许承诺出去的钱"，两者相加才是真正可用的 headroom。
        """
        budget = state.budget
        _validate(budget, state)
        if outstanding < 0:
            raise BudgetConfigurationError(f"预扣费用不能为负：{outstanding}")
        readings = self.readings(budget, state)
        if outstanding:
            used, limit = readings[Category.COST]
            readings[Category.COST] = (used + outstanding, limit)
        relevant = ACTIVITY_CATEGORIES[activity]
        ordered = [c for c in _PRIORITY if c in relevant]
        reserved = self._apply_reservation(
            readings, activity=activity, reservation=reservation, ordered=ordered
        )
        exhausted = tuple(_STOP_REASONS[c] for c in ordered if readings[c][0] >= readings[c][1])
        usage = tuple((c, readings[c][0], readings[c][1]) for c in ordered)
        return BudgetEvaluation(
            primary=exhausted[0] if exhausted else None,
            exhausted=exhausted,
            categories=tuple(ordered),
            usage=usage,
            reserved=tuple(reserved),
        )

    @staticmethod
    def _apply_reservation(
        readings: dict[Category, tuple[float, float]],
        *,
        activity: Activity,
        reservation: ClosureReservation | None,
        ordered: list[Category],
    ) -> list[tuple[Category, float]]:
        """把闭环预留从"先于审查花钱"的活动可用上限里扣掉，返回被扣掉的量。"""
        if reservation is None or activity not in RESERVATION_BEARING_ACTIVITIES:
            return []
        spent: list[tuple[Category, float]] = []
        for category, amount in (
            (Category.TOKENS, float(reservation.tokens)),
            (Category.COST, reservation.cost),
        ):
            if category not in ordered:
                continue
            used, limit = readings[category]
            if limit == float("inf"):
                # 未封顶的项目没有"留给闭环"可言：真实消耗仍由节点入口与派发层
                # 记账判定，这里不做假扣减。
                continue
            if not reservation.estimated and category is Category.COST:
                # 未知费用 + 设了硬费用上限：无法证明闭环还付得起，把剩余额度全部
                # 视作预留（fail closed，判定自然给出 budget_cost）。
                amount = max(limit - used, 0.0)
            if amount <= 0:
                continue
            spent.append((category, amount))
            readings[category] = (used, max(limit - amount, 0.0))
        return spent

    def check(
        self,
        state: RepairState,
        activity: Activity,
        *,
        reservation: ClosureReservation | None = None,
        outstanding: float = 0.0,
    ) -> StopReason | None:
        return self.evaluate(
            state, activity, reservation=reservation, outstanding=outstanding
        ).primary

    def remaining_iterations(self, state: RepairState) -> int:
        used, limit = self.readings(state.budget, state)[Category.ITERATIONS]
        return max(int(limit - used), 0)

    def coding_token_ceiling(self, state: RepairState, reservation: ClosureReservation) -> int:
        """本轮编码允许达到的**累计** token 上限（已扣闭环预留）。

        节点入口只检查一次，而编码循环内部有几十次模型调用，所以真正管得住闭环
        的是这个数：它被传进 :class:`~codepilot.agents.coder.CodingAgent`，在每一圈
        循环里判定。``max_total_tokens`` 是硬上限，本函数只会给出**更小**的值。

        预扣（``outstanding``）**不**进这里：那两个天花板是给 Coding Agent 与
        ``state.usage`` 比的，而那本账只含**已结算**的调用。预扣在派发入口
        （:meth:`evaluate`）就把整次派发拦住，不会漏到循环里。
        """
        evaluation = self.evaluate(state, Activity.CODEGEN, reservation=reservation)
        try:
            _, limit = evaluation.usage_for(Category.TOKENS)
        except KeyError:
            return state.budget.max_total_tokens
        return int(limit)

    def coding_cost_ceiling(
        self, state: RepairState, reservation: ClosureReservation
    ) -> float | None:
        """本轮编码允许达到的**累计**费用上限；未设 ``max_cost`` 时返回 ``None``。"""
        budget = state.budget
        if budget.max_cost is None:
            return None
        evaluation = self.evaluate(state, Activity.CODEGEN, reservation=reservation)
        try:
            _, limit = evaluation.usage_for(Category.COST)
        except KeyError:
            return budget.max_cost
        return float(limit)
