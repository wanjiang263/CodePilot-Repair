"""强制修复策略切换（02 §15.6、G-10）。

规则原文：``is_patch_caused=True`` 且连续两轮 ``refine`` 无进展时，下一轮**强制**
改为 ``revert_and_retry``。

这条规则存在的原因是 refine 的一个系统性失效模式：补丁本身引入了新错误时，
"在当前补丁上继续改"会把同一个错误越描越大，而模型的自报 confidence 越高，
它越倾向于继续 refine。所以这里刻意：

* **不看 confidence**（02 §14.6：自报置信度没有校准，用来分支会得到一个不可调试
  的系统）；
* **不允许模型否决**：即使这一轮模型提议 ``refine`` / ``expand_scope``，只要命中
  条件就改写成 ``revert_and_retry``。模型提议的原文与改写原因写进
  ``state.events``，可审计。

计数的口径写在 :meth:`RepairPolicy.rounds` 里：第 i 轮（i≥2）**实际执行**的策略
是第 i-1 轮诊断里那条**生效**策略——节点在 ``diagnose`` 阶段已经把生效策略回写进
``Attempt.diagnosis.repair_strategy``，模型的原始提议保存在事件里。这样"连续两轮
refine"数的是真正执行过的两轮，不是模型想过什么。
"""

from __future__ import annotations

from dataclasses import dataclass

from codepilot.schemas.diagnosis import Diagnosis
from codepilot.schemas.enums import RepairStrategy
from codepilot.schemas.progress import made_progress
from codepilot.schemas.state import RepairState

__all__ = ["FORCED_NO_PROGRESS_ROUNDS", "PolicyDecision", "RepairPolicy"]

#: 触发强制切换所需的"连续无进展 refine 轮数"（02 §15.6 的"连续两轮"）。
FORCED_NO_PROGRESS_ROUNDS = 2


@dataclass(frozen=True, slots=True)
class RoundStrategy:
    """一轮修复实际执行的策略，以及该轮是否有进展。"""

    iteration: int
    strategy: RepairStrategy
    progressed: bool

    @property
    def stalled_refine(self) -> bool:
        return self.strategy is RepairStrategy.REFINE and not self.progressed


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """策略判定结果。

    ``proposed`` 与 ``effective`` 分开保存：报告要能回答"模型想干什么、系统实际
    让它干了什么、为什么"。只有 ``forced`` 为 True 时两者才可能不同。
    """

    proposed: RepairStrategy
    effective: RepairStrategy
    forced: bool
    refine_stall_streak: int
    reason: str

    @property
    def overridden(self) -> bool:
        return self.proposed is not self.effective


class RepairPolicy:
    """把 state 历史 + 本轮诊断变成"下一轮该执行哪个策略"。"""

    def rounds(self, state: RepairState) -> list[RoundStrategy]:
        """逐轮的生效策略与进展。

        第 1 轮没有前置诊断，它是 ``generate_patch`` 的首轮产物，因此**不记为
        refine**：把首轮算进"连续两轮 refine"会让任何两轮无进展的 run 都提前
        触发强制回退，那是 off-by-one，不是保守。

        进展按"上一条带 progress 快照的 Attempt"配对计算，而不是按列表下标：
        校验失败的轮次没有执行结果、也就没有 progress 快照，用下标会整体错位。
        """
        rounds: list[RoundStrategy] = []
        previous_snapshot = None
        previous_diagnosis = None
        for attempt in state.attempts:
            diagnosis = attempt.diagnosis
            snapshot = attempt.progress
            if (
                previous_diagnosis is not None
                and snapshot is not None
                and previous_snapshot is not None
            ):
                rounds.append(
                    RoundStrategy(
                        iteration=attempt.iteration,
                        strategy=previous_diagnosis.repair_strategy,
                        progressed=made_progress(previous_snapshot, snapshot),
                    )
                )
            if snapshot is not None:
                previous_snapshot = snapshot
            if diagnosis is not None:
                previous_diagnosis = diagnosis
        return rounds

    def stall_streak(self, state: RepairState) -> int:
        """从最近一轮往回数，连续多少轮是"refine 且无进展"。

        一次进展即清零：规则要抓的是"卡住了还在原地磨"，不是"总共失败过两次"。
        """
        streak = 0
        for item in reversed(self.rounds(state)):
            if item.stalled_refine:
                streak += 1
                continue
            break
        return streak

    def decide(self, state: RepairState, diagnosis: Diagnosis) -> PolicyDecision:
        """返回本轮**生效**的策略。"""
        proposed = diagnosis.repair_strategy
        if proposed is RepairStrategy.GIVE_UP:
            # give_up 会经 diagnosis_give_up 直接终止，不该被改写成继续修。
            return PolicyDecision(
                proposed=proposed,
                effective=RepairStrategy.GIVE_UP,
                forced=False,
                refine_stall_streak=self.stall_streak(state),
                reason="diagnosis gave up; the run stops",
            )

        streak = self.stall_streak(state)
        if diagnosis.is_patch_caused and streak >= FORCED_NO_PROGRESS_ROUNDS:
            return PolicyDecision(
                proposed=proposed,
                effective=RepairStrategy.REVERT_AND_RETRY,
                forced=True,
                refine_stall_streak=streak,
                reason=(
                    f"is_patch_caused=True and {streak} consecutive stalled refine rounds"
                    f" (>= {FORCED_NO_PROGRESS_ROUNDS}); forced revert_and_retry,"
                    f" overriding model proposal {proposed.value}"
                ),
            )

        return PolicyDecision(
            proposed=proposed,
            effective=proposed,
            forced=False,
            refine_stall_streak=streak,
            reason=f"model proposal accepted (stall streak={streak})",
        )
