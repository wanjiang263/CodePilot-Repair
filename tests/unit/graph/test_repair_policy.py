"""G-10：连续 refine 无进展时的强制 revert_and_retry。

规则（02 §15.6）：``is_patch_caused=True`` 且连续两轮 ``refine`` 无进展时，下一轮
**强制**改为 ``revert_and_retry``。

先固定两个容易搞错的口径：

* **第 1 轮不算 refine**。它是 ``generate_patch`` 的产物，没有前置诊断；把它算进
  "连续两轮 refine"会让任何两轮无进展的 run 提前触发强制回退，那是 off-by-one。
* 某一轮**实际执行**的策略 = 上一轮诊断里那条**生效**策略。所以下面 ``decided``
  描述的是"这一轮结束时写进 Attempt 的诊断建议"，它决定的是**下一轮**执行什么。

因此"两轮停滞的 refine"需要三条 Attempt：第 2、3 轮都在 refine 且都没进展，
第 4 轮才会被强制成 revert。

这条规则的价值全在"强制"两个字上，所以测试盯三件事：模型自报 confidence 再高
也不能豁免、模型提议别的策略也要被改写、一次进展之后计数必须清零。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codepilot.graph.repair_policy import FORCED_NO_PROGRESS_ROUNDS, RepairPolicy
from codepilot.schemas.diagnosis import Diagnosis
from codepilot.schemas.enums import ExecutionStatus, RepairStrategy
from codepilot.schemas.execution import ExecutionResult
from codepilot.schemas.progress import ProgressSnapshot
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.validation import ValidationResult


def _snapshot(failed: int, signature: str) -> ProgressSnapshot:
    return ProgressSnapshot(
        target_failed=failed,
        regression_new_failures=0,
        signatures=frozenset({signature}),
        regression_measured=False,
    )


def _attempt(
    iteration: int,
    *,
    signature: str,
    failed: int,
    decided: RepairStrategy = RepairStrategy.REFINE,
) -> Attempt:
    """一轮修复的记录。

    ``decided`` 是本轮结束时诊断给出的**生效**策略，也就是下一轮真正执行的策略。
    """
    return Attempt(
        iteration=iteration,
        patch_hash=f"h{iteration}",
        validation=ValidationResult(passed=True),
        execution=ExecutionResult(status=ExecutionStatus.FAIL),
        failure_signatures=[signature],
        diagnosis=Diagnosis(root_cause="hypothesis", repair_strategy=decided),
        progress=_snapshot(failed, signature),
    )


def _state(*attempts: Attempt) -> RepairState:
    state = RepairState(
        task_id="t",
        run_id="r",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime(2026, 10, 14, tzinfo=UTC),
    )
    state.attempts.extend(attempts)
    return state


def _two_stalled_refines() -> RepairState:
    """第 2、3 轮都执行 refine 且都没有进展 —— 强制条件刚好成立的那组历史。"""
    return _state(
        _attempt(1, signature="A", failed=2, decided=RepairStrategy.REFINE),
        _attempt(2, signature="A", failed=2, decided=RepairStrategy.REFINE),
        _attempt(3, signature="A", failed=2),
    )


def _fresh(
    strategy: RepairStrategy = RepairStrategy.REFINE,
    *,
    is_patch_caused: bool = True,
    confidence: float = 0.5,
) -> Diagnosis:
    return Diagnosis(
        root_cause="hypothesis",
        is_patch_caused=is_patch_caused,
        repair_strategy=strategy,
        confidence=confidence,
    )


# ------------------------------------------------------------------ 计数口径


def test_first_generate_round_is_not_counted_as_a_refine() -> None:
    """首轮没有前置诊断，因此不进入 refine 计数。"""
    state = _state(
        _attempt(1, signature="A", failed=2, decided=RepairStrategy.REFINE),
        _attempt(2, signature="B", failed=1),
    )
    rounds = RepairPolicy().rounds(state)
    assert [item.iteration for item in rounds] == [2]
    assert rounds[0].progressed is True
    assert RepairPolicy().stall_streak(state) == 0


def test_two_stalled_refines_build_a_streak_of_two() -> None:
    state = _two_stalled_refines()
    rounds = RepairPolicy().rounds(state)
    assert [item.strategy for item in rounds] == [RepairStrategy.REFINE, RepairStrategy.REFINE]
    assert [item.progressed for item in rounds] == [False, False]
    assert RepairPolicy().stall_streak(state) == FORCED_NO_PROGRESS_ROUNDS


def test_one_progress_resets_the_streak() -> None:
    """规则抓的是"卡住还在原地磨"，不是"总共失败过两次"。"""
    state = _state(
        _attempt(1, signature="A", failed=3, decided=RepairStrategy.REFINE),
        _attempt(2, signature="A", failed=3, decided=RepairStrategy.REFINE),
        _attempt(3, signature="B", failed=1),
    )
    assert RepairPolicy().stall_streak(state) == 0


def test_stalled_expand_scope_does_not_count_toward_the_refine_streak() -> None:
    state = _state(
        _attempt(1, signature="A", failed=2, decided=RepairStrategy.EXPAND_SCOPE),
        _attempt(2, signature="A", failed=2, decided=RepairStrategy.EXPAND_SCOPE),
        _attempt(3, signature="A", failed=2),
    )
    assert RepairPolicy().stall_streak(state) == 0


def test_streak_counts_backwards_from_the_most_recent_round() -> None:
    """streak 只数"最近的连续停滞"，顺序决定结果。

    两组历史里的停滞轮数相同，只是顺序相反：进展在前 → 之后又停滞两次 = 2；
    进展在后 → 停滞被打断 = 0。用同一个数断言两种顺序，就等于没在测"连续"两个字。
    """
    stalls_then_progress = _state(
        _attempt(1, signature="A", failed=2, decided=RepairStrategy.REFINE),
        _attempt(2, signature="A", failed=2, decided=RepairStrategy.REFINE),
        _attempt(3, signature="B", failed=1),
    )
    assert RepairPolicy().stall_streak(stalls_then_progress) == 0

    progress_then_stalls = _state(
        _attempt(1, signature="A", failed=2, decided=RepairStrategy.REFINE),
        _attempt(2, signature="B", failed=1, decided=RepairStrategy.REFINE),
        _attempt(3, signature="A", failed=2, decided=RepairStrategy.REFINE),
        _attempt(4, signature="A", failed=2),
    )
    assert RepairPolicy().stall_streak(progress_then_stalls) == 2


# ------------------------------------------------------------------ 强制切换


@pytest.mark.parametrize(
    "proposed", [RepairStrategy.REFINE, RepairStrategy.EXPAND_SCOPE], ids=["refine", "expand_scope"]
)
def test_forced_revert_overrides_any_non_giveup_proposal(proposed: RepairStrategy) -> None:
    decision = RepairPolicy().decide(_two_stalled_refines(), _fresh(proposed))
    assert decision.effective is RepairStrategy.REVERT_AND_RETRY
    assert decision.forced is True
    assert decision.proposed is proposed
    assert decision.overridden is True


def test_is_patch_caused_false_does_not_force() -> None:
    decision = RepairPolicy().decide(
        _two_stalled_refines(), _fresh(RepairStrategy.REFINE, is_patch_caused=False)
    )
    assert decision.effective is RepairStrategy.REFINE
    assert decision.forced is False


def test_one_stalled_round_is_not_enough() -> None:
    state = _state(
        _attempt(1, signature="A", failed=2, decided=RepairStrategy.REFINE),
        _attempt(2, signature="A", failed=2),
    )
    assert RepairPolicy().stall_streak(state) == 1
    assert RepairPolicy().decide(state, _fresh()).forced is False


@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.99, 1.0])
def test_confidence_cannot_exempt_a_forced_switch(confidence: float) -> None:
    """模型把置信度报到 1.0 也不能绕开强制规则（02 §14.6）。"""
    decision = RepairPolicy().decide(_two_stalled_refines(), _fresh(confidence=confidence))
    assert decision.effective is RepairStrategy.REVERT_AND_RETRY
    assert decision.forced is True


def test_give_up_is_never_rewritten_into_another_repair() -> None:
    """give_up 会经 diagnosis_give_up 终止；把它改成 revert 等于无视模型的放弃。"""
    decision = RepairPolicy().decide(_two_stalled_refines(), _fresh(RepairStrategy.GIVE_UP))
    assert decision.effective is RepairStrategy.GIVE_UP
    assert decision.forced is False
    assert decision.overridden is False


def test_switch_reason_names_both_the_streak_and_the_overridden_proposal() -> None:
    """历史策略与切换原因必须可审计。"""
    reason = RepairPolicy().decide(_two_stalled_refines(), _fresh()).reason
    assert "is_patch_caused=True" in reason
    assert "2 consecutive stalled refine rounds" in reason
    assert "overriding model proposal refine" in reason


def test_accepted_proposal_records_the_streak_it_saw() -> None:
    decision = RepairPolicy().decide(_state(_attempt(1, signature="A", failed=2)), _fresh())
    assert decision.forced is False
    assert decision.effective is RepairStrategy.REFINE
    assert "stall streak=0" in decision.reason


def test_refine_refine_revert_full_sequence() -> None:
    """refine → refine → 强制 revert 的完整序列（05 G-10 的验收点）。"""
    policy = RepairPolicy()
    state = _state(_attempt(1, signature="A", failed=2, decided=RepairStrategy.REFINE))

    # 只有一轮停滞的 refine：仍然接受 refine。
    state.attempts.append(_attempt(2, signature="A", failed=2, decided=RepairStrategy.REFINE))
    assert policy.stall_streak(state) == 1
    assert policy.decide(state, _fresh()).effective is RepairStrategy.REFINE

    # 第 3 轮又停滞：两轮连续停滞的 refine 成立，下一轮被强制回退。
    state.attempts.append(_attempt(3, signature="A", failed=2))
    assert policy.stall_streak(state) == 2
    assert policy.decide(state, _fresh()).effective is RepairStrategy.REVERT_AND_RETRY
