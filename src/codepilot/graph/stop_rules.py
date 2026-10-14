"""停止规则（02 §15.5、G-08）。

02 §15.5 的表格逐项展开是 **11 个** ``stop_reason``（5 个 ``budget_*`` +
``repeated_patch`` / ``repeated_failure`` / ``no_progress`` / ``sandbox_failure`` /
``validation_loop`` / ``diagnosis_give_up``），docs/05 里"8 种"的说法把
``budget_*`` 当成了 1 种。本模块以 :class:`~codepilot.schemas.enums.StopReason`
这个权威枚举为准逐条判定，"8"只是表格行数的简称。

``StopReason`` 里还有两个不属于 §15.5 表格的成员，本模块也显式接住，不静默忽略：

* ``budget_tool_calls``：每轮工具调用上限。它是 02 §15.3 里的第六类预算，
  但 §15.5 的表格没有对应行——由 :class:`~codepilot.graph.budget.BudgetManager`
  在 ``Activity.REPAIR`` 判定里一并给出。
* ``patch_rejected`` / ``tests_failed`` / ``tool_error`` / ``workflow_error``：
  Phase 5 单次图已有的终态标签，属于"节点直接给出结论"，不是迭代停止规则。

## Patch hash：为什么不"去掉所有空白"

02 §15.5 写的是"patch hash 对 diff 去掉空白差异后计算"。**按字面实现是错的**：
Python 的缩进就是语法，而三引号字符串**行尾的空格也是数据**。把 diff 里所有空白
抹掉（``"".join(diff.split())``）会让下面两个语义不同的补丁得到同一个哈希：

```diff
-    return value - 1        →  返回 2
+        return value - 1    →  SyntaxError / 不同的缩进层级
```

于是 ``repeated_patch`` 会把"模型换了缩进又交了同一份坏代码"误判成"重复补丁"并
提前停止——或者反过来，让真正重复的补丁绕过去。这里改成只归一化**不含内容的
git 元数据**，正文行（含缩进与行尾空格）一律逐字保留：

* 丢弃 ``index <sha>..<sha>``（纯 blob 元数据，内容由正文行决定）
* 丢弃 ``--- a/`` / ``+++ b/``（文件身份已在 ``diff --git`` 行里）
* 把 ``\\ No newline at end of file`` 转成显式标记 ``NOEOL`` ——它**是**内容差异，
  不能当噪声丢掉，但也不该因为出现在 diff 里就让整块哈希漂移
* 统一 CRLF → LF

代价是"只差一个行尾空格"的两个补丁会被判为不同补丁，多花一轮预算。这个方向是
安全的：**误判重复会提前停止修复（漏修），漏判重复只是多修一轮**。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from codepilot.graph.budget import Activity, BudgetManager
from codepilot.graph.progress import ProgressResult, ProgressTracker
from codepilot.schemas.enums import ExecutionStatus, RepairStrategy, RunStatus, StopReason
from codepilot.schemas.state import RepairState

__all__ = [
    "StopDecision",
    "StopRules",
    "canonical_patch",
    "patch_hash",
]

_INDEX_LINE = re.compile(r"^index [0-9a-fA-F]{7,40}(?:\.\.[0-9a-fA-F]{7,40})?(?: \d{6})?$")
_SOURCE_HEADER = re.compile(r"^--- a/")
_TARGET_HEADER = re.compile(r"^\+\+\+ b/")
_NO_NEWLINE = re.compile(r"^\\ No newline at end of file\s*$")

#: 归一化后的显式标记：文件末尾没有换行符。它代表真实内容差异，必须参与哈希。
_NO_NEWLINE_TOKEN = "!NOEOL!"


def canonical_patch(diff: str) -> str:
    """保持文件身份、增删行语义与缩进的 diff 规范化。"""
    lines: list[str] = []
    for raw in diff.replace("\r\n", "\n").split("\n"):
        if _INDEX_LINE.match(raw) or _SOURCE_HEADER.match(raw) or _TARGET_HEADER.match(raw):
            continue
        if _NO_NEWLINE.match(raw):
            lines.append(_NO_NEWLINE_TOKEN)
            continue
        lines.append(raw)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def patch_hash(diff: str) -> str:
    """规范化 diff 的 SHA-256。空补丁同样有确定值。"""
    return hashlib.sha256(canonical_patch(diff).encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True, slots=True)
class StopDecision:
    """停止判定。

    ``primary`` 是按固定优先级选出的一个原因；``all_reasons`` 是**全部**命中的原因。
    只报一个会把"预算耗尽且已在重复失败"压成单条信息，报告里就看不出真实缺口。
    """

    primary: StopReason | None
    all_reasons: tuple[StopReason, ...]
    explanations: tuple[str, ...]
    progress: ProgressResult | None = None

    @property
    def should_stop(self) -> bool:
        return self.primary is not None


class StopRules:
    """按固定优先级评估 §15.5 的全部停止条件。

    优先级（先判定者为 primary，其余仍出现在 ``all_reasons`` 里）：

    1. ``sandbox_failure`` —— 环境不可信时，其他一切信号都是从坏运行里读出来的。
    2. ``budget_*`` —— 可证明的算术事实，且**不可被模型策略绕过**（G-03 第 7 条）。
    3. ``diagnosis_give_up`` —— 诊断明确放弃；继续跑只会重复同一个结论。
    4. ``validation_loop`` —— 连续三次无效补丁，说明生成端没有收敛。
    5. ``repeated_patch`` —— 当前补丁与历史等价重复。
    6. ``repeated_failure`` —— 连续两轮失败签名集合与失败身份一致。
    7. ``no_progress`` —— 连续两轮无进展，或签名集合回到过更早一轮（振荡）。

    把 ``budget_*`` 排在启发式重复检测之前是刻意的：预算是记账事实，重复判定是
    启发式；两者同时成立时报告可证明的那个更安全。
    """

    def __init__(
        self,
        budget: BudgetManager | None = None,
        progress: ProgressTracker | None = None,
    ) -> None:
        self.budget = budget or BudgetManager()
        self.progress = progress or ProgressTracker()

    def evaluate(self, state: RepairState) -> StopDecision:
        reasons: list[StopReason] = []
        explanations: list[str] = []

        # 1) 环境与沙箱故障。
        execution = state.execution
        if execution is not None and execution.is_environment_problem:
            reasons.append(StopReason.SANDBOX_FAILURE)
            explanations.append(
                f"execution status={execution.status.value} "
                f"reason={execution.error_reason or 'unspecified'}"
            )

        # 2) 六类预算。
        evaluation = self.budget.evaluate(state, Activity.REPAIR)
        if evaluation.exhausted:
            reasons.extend(evaluation.exhausted)
            explanations.append(
                "budget exhausted: "
                + ", ".join(reason.value for reason in evaluation.exhausted)
                + f" usage={evaluation.summary()}"
            )

        # 3) 诊断放弃。
        last = state.current_attempt
        if (
            last is not None
            and last.diagnosis is not None
            and last.diagnosis.repair_strategy is RepairStrategy.GIVE_UP
        ):
            reasons.append(StopReason.DIAGNOSIS_GIVE_UP)
            explanations.append(f"diagnosis strategy=give_up iteration={last.iteration}")

        # 4) 连续三次校验失败。
        validation_failures = self.consecutive_validation_failures(state)
        if validation_failures >= 3:
            reasons.append(StopReason.VALIDATION_LOOP)
            explanations.append(f"consecutive patch validation failures={validation_failures}")

        # 5) 等价重复补丁。
        repeated_patch = self.repeated_patch(state)
        if repeated_patch is not None:
            reasons.append(StopReason.REPEATED_PATCH)
            explanations.append(f"patch identical to iteration {repeated_patch}")

        # 6/7) 进度侧判定。
        progress = self.progress.evaluate(state)
        if progress.repeated_failure:
            reasons.append(StopReason.REPEATED_FAILURE)
        if progress.is_no_progress or progress.oscillated:
            reasons.append(StopReason.NO_PROGRESS)
        explanations.extend(progress.reasons)
        if progress.signature_collision_suspected:
            # 疑似 DA-010 误聚合：签名相等但失败身份不同，**不**计入停止原因。
            explanations.append("collision kept the loop running (not a stop signal)")

        return StopDecision(
            primary=reasons[0] if reasons else None,
            all_reasons=tuple(reasons),
            explanations=tuple(explanations),
            progress=progress,
        )

    # ------------------------------------------------------------------ 单项

    def consecutive_validation_failures(self, state: RepairState) -> int:
        """从最近一轮往回数，连续多少次 Patch Validation 失败。"""
        streak = 0
        for attempt in reversed(state.attempts):
            if attempt.validation.passed:
                break
            streak += 1
        return streak

    def repeated_patch(self, state: RepairState) -> int | None:
        """当前补丁与历史上更早一轮等价时返回那一轮的 iteration。

        比较对象是**已存进 Attempt 的规范化哈希**，因此恢复后身份稳定；只与
        "更早的轮"比较，否则刚追加的当前轮会立刻判自己重复。
        """
        attempt = state.current_attempt
        if attempt is None or not attempt.patch_hash:
            return None
        for earlier in state.attempts[:-1]:
            if earlier.patch_hash == attempt.patch_hash:
                return earlier.iteration
        return None

    def status_for(self, decision: StopDecision) -> RunStatus | None:
        """停止时应写入的 run 状态。

        ``sandbox_failure`` 单独映射成 ``SANDBOX_ERROR``；预算类映射成
        ``BUDGET_EXHAUSTED``；其余是"被停止规则拦下"，统一 ``STOPPED``。
        """
        if decision.primary is None:
            return None
        if decision.primary is StopReason.SANDBOX_FAILURE:
            return RunStatus.SANDBOX_ERROR
        if decision.primary.value.startswith("budget_"):
            return RunStatus.BUDGET_EXHAUSTED
        if decision.primary in (StopReason.TESTS_FAILED, StopReason.PATCH_REJECTED):
            return RunStatus.FAILED
        return RunStatus.STOPPED

    def execution_terminal(self, state: RepairState) -> bool:
        """执行结果是否属于"环境问题"这一类终止信号。"""
        execution = state.execution
        return execution is not None and execution.status in (
            ExecutionStatus.ENV_ERROR,
            ExecutionStatus.SANDBOX_ERROR,
        )
