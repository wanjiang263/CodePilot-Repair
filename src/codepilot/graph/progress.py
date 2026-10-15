"""进度判定、振荡检测与签名误聚合控制（02 §15.4、G-06、G-07、DA-010）。

``made_progress`` 本身在 :mod:`codepilot.schemas.progress`（L0 契约，Phase 0 已冻结
并被单测覆盖）。本模块补的是它**拿不到的东西**：跨轮历史。

三个判定分开实现，因为它们各自的失效方式不同：

* **连续两轮无进展** → ``no_progress``（02 §15.5）。
* **振荡**：当前签名集合曾在更早的**任意**一轮出现过 → 判为无进展。只看上一轮
  只能抓 A→B→A，抓不到 A→B→C→A；而后者在真实多轮修复里很常见（改回一个更早的
  实现方式）。
* **签名碰撞**（DA-010）：Phase 7 的 SHA1 会把消息里的数字与长字符串归一化，
  因此两个**实际不同**的故障可能得到同一个签名。这里不改冻结算法（改了会让历史
  run 的签名全部失效），而是在"停止"这一侧加核验：签名集合相等**且**失败测试
  身份（node_id + 异常类型 + 类别）也相等时，才认定是同一批失败；只相等签名而
  身份不同，则记为疑似碰撞、**不据此停止**。

最后一条是刻意的保守取舍：把不同根因误判成同一根因会让系统提前停止修复（漏修），
而漏判一次重复最多是多花一轮预算。前者的代价高得多。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult
from codepilot.schemas.progress import ProgressSnapshot, made_progress
from codepilot.schemas.regression import RegressionResult
from codepilot.schemas.state import Attempt, RepairState

__all__ = [
    "ProgressResult",
    "ProgressTracker",
    "RoundEvidence",
    "build_progress_snapshot",
    "round_evidence",
]


@dataclass(frozen=True, slots=True)
class RoundEvidence:
    """一轮失败的可核验身份：签名 + 测试身份 + 类别 + 证据引用。"""

    iteration: int
    signatures: frozenset[str]
    test_ids: frozenset[str]
    categories: frozenset[str]
    artifact_refs: frozenset[str] = field(default_factory=frozenset)
    failure_details: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)

    def same_failures_as(self, other: RoundEvidence) -> bool:
        """签名相等**且**身份相等，才认为是同一批失败。"""
        return (
            self.signatures == other.signatures
            and self.test_ids == other.test_ids
            and self.categories == other.categories
            and self.failure_details == other.failure_details
        )

    def signature_collision_with(self, other: RoundEvidence) -> bool:
        """签名集合相同，但失败身份不同——这就是 DA-010 描述的误聚合。"""
        return self.signatures == other.signatures and not self.same_failures_as(other)


def build_progress_snapshot(
    execution: ExecutionResult | None,
    *,
    signatures: Sequence[str],
    regression: RegressionResult | None,
) -> ProgressSnapshot:
    """由真实执行结果构造进度快照。

    回归未测量时 ``regression_new_failures`` 写 0 但 ``regression_measured=False``：
    0 是"没有数据"的占位，判定只看 ``regression_measured`` 为 True 的那一侧。
    """
    if execution is None or execution.status in (
        ExecutionStatus.ENV_ERROR,
        ExecutionStatus.SANDBOX_ERROR,
    ):
        # 无效/缺失的执行结果不能自动视为进展，也不能视为"0 个失败"。
        target_failed = 0 if execution is None else len(execution.failed_tests)
    else:
        target_failed = len(execution.failed_tests)
    if regression is None:
        return ProgressSnapshot(
            target_failed=target_failed,
            regression_new_failures=0,
            signatures=frozenset(signatures),
            regression_measured=False,
        )
    return ProgressSnapshot(
        target_failed=target_failed,
        regression_new_failures=len(regression.new_failures),
        signatures=frozenset(signatures),
        regression_measured=True,
    )


def signature_sets(attempts: Sequence[Attempt]) -> list[frozenset[str]]:
    """历史各轮的签名集合，按轮次顺序。"""
    return [frozenset(attempt.failure_signatures) for attempt in attempts]


def round_evidence(attempt: Attempt) -> RoundEvidence | None:
    """从 Attempt 里恢复该轮的可核验失败身份。

    身份不额外存字段，而是从 ``attempt.execution`` 的失败用例现算：Phase 7 的
    签名是哈希，不可逆，但 Attempt 里同时存着原始 ``TestCaseResult``，
    node_id / failure_type / 类别都在，恢复时也不需要额外 Artifact。
    """
    execution = attempt.execution
    if attempt.regression is not None and attempt.regression.has_regression:
        cases = [
            case
            for result in attempt.regression.executions.values()
            for case in result.tests
            if case.node_id in attempt.regression.new_failures
        ]
        execution = ExecutionResult(
            status=ExecutionStatus.FAIL, tests=cases, tests_failed=len(cases)
        )
    if execution is None:
        return None
    failures: list[tuple[str, str, str]] = []
    ids: set[str] = set()
    categories: set[str] = set()
    for case in execution.tests:
        if case.outcome not in (TestOutcome.FAILED, TestOutcome.ERROR):
            continue
        ids.add(case.node_id)
        categories.add(case.failure_type or "unknown")
        failures.append((case.node_id, case.failure_type or "unknown", case.message or ""))
    if not ids and execution.status is not ExecutionStatus.PASS:
        # 执行整体失败但没有逐用例记录（COLLECTION_ERROR / TIMEOUT / OOM）。
        ids.add(f"<execution:{execution.status.value}>")
        categories.add(execution.status.value)
    return RoundEvidence(
        failure_details=frozenset(failures),
        iteration=attempt.iteration,
        signatures=frozenset(attempt.failure_signatures),
        test_ids=frozenset(ids),
        categories=frozenset(categories),
        artifact_refs=frozenset({execution.artifact_dir})
        if execution.artifact_dir
        else frozenset(),
    )


@dataclass(frozen=True, slots=True)
class ProgressResult:
    """一个 run 到当前轮为止的进度判定。"""

    made_progress: bool
    no_progress_streak: int
    repeated_failure: bool
    oscillated: bool
    oscillation_round: int | None
    signature_collision_suspected: bool
    reasons: tuple[str, ...]

    @property
    def is_no_progress(self) -> bool:
        """``no_progress`` 停止规则的触发条件（02 §15.5）。"""
        return self.no_progress_streak >= 2


class ProgressTracker:
    """把 state 历史变成进度/振荡/碰撞三类判定；纯读，不写回。"""

    def evidence(self, state: RepairState) -> list[RoundEvidence]:
        items: list[RoundEvidence] = []
        for attempt in state.attempts:
            found = round_evidence(attempt)
            if found is not None:
                items.append(found)
        return items

    def snapshots(self, state: RepairState) -> list[ProgressSnapshot]:
        return [attempt.progress for attempt in state.attempts if attempt.progress is not None]

    def streak(self, snapshots: Sequence[ProgressSnapshot]) -> int:
        """从最近一轮往回数，连续多少次"没有进展"。

        只有第 2 轮起才存在比较基线：首轮没有 prev 快照，既不算进展也不算无进展
        （把首轮记成无进展会让任何单轮 run 都被判成停滞）。
        """
        streak = 0
        for index in range(len(snapshots) - 1, 0, -1):
            if made_progress(snapshots[index - 1], snapshots[index]):
                break
            streak += 1
        return streak

    def evaluate(self, state: RepairState) -> ProgressResult:
        snapshots = self.snapshots(state)
        evidence = self.evidence(state)
        reasons: list[str] = []

        streak = self.streak(snapshots)
        if streak:
            reasons.append(f"no_progress_streak={streak}")

        # 重复失败与振荡共用**同一套**身份核验。只给 repeated_failure 加核验没有
        # 意义：no_progress 会因为"签名集合回到过更早一轮"把同一个 run 停在完全
        # 相同的地方，DA-010 的误聚合照样生效。
        repeated_failure = False
        collision = False
        if len(evidence) >= 2:
            previous, current = evidence[-2], evidence[-1]
            if previous.signatures == current.signatures:
                if previous.same_failures_as(current):
                    repeated_failure = True
                    reasons.append(
                        f"repeated_failure rounds={previous.iteration},{current.iteration}"
                    )
                else:
                    collision = True
                    reasons.append(
                        f"signature_collision rounds={previous.iteration},{current.iteration}"
                        "（DA-010：签名相同但失败身份不同，不作为停止依据）"
                    )

        oscillation_round: int | None = None
        if len(evidence) >= 2 and evidence[-1].signatures:
            current = evidence[-1]
            for position, earlier in enumerate(evidence[:-1], start=1):
                if earlier.signatures != current.signatures:
                    continue
                if earlier.same_failures_as(current):
                    oscillation_round = position
                    break
                collision = True
                reasons.append(
                    f"signature_collision round {position} vs {current.iteration}"
                    "（DA-010：签名相同但失败身份不同，不判振荡）"
                )
        oscillated = oscillation_round is not None
        if oscillated and oscillation_round is not None:
            reasons.append(f"oscillation back to round {oscillation_round}")

        # 最近一轮相对上一轮是否推进。02 §15.4 末段：签名集合回到过更早的一轮，
        # 即使数字上"看起来变了"也判为无进展——那是振荡不是推进。
        latest_made = False
        if len(snapshots) >= 2:
            latest_made = made_progress(snapshots[-2], snapshots[-1])
            if oscillated:
                latest_made = False
                reasons.append("oscillation overrides apparent progress")
        elif snapshots:
            reasons.append("first round has no baseline snapshot; progress is undefined")

        return ProgressResult(
            made_progress=latest_made,
            no_progress_streak=streak,
            repeated_failure=repeated_failure,
            oscillated=oscillated,
            oscillation_round=oscillation_round,
            signature_collision_suspected=collision,
            reasons=tuple(reasons),
        )
