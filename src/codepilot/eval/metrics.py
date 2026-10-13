"""离线评测指标（02 §19，docs/05 D-09）。

这里的每个函数都必须是纯函数：不碰网络、不起容器、不读写文件。指标一旦需要
副作用才能算出来，它就再也无法在单元测试里被手算核对，也无法在评测脚本和图
节点之间复用。Phase 10/11 的 Benchmark 直接把这些函数当作接口使用，所以入参
是"任务级结论"而不是任何运行期对象。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from codepilot.schemas.enums import RunStatus

__all__ = [
    "SelfVerifiedPrecision",
    "SelfVerifiedSample",
    "agent_claims_success",
    "self_verified_precision",
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
