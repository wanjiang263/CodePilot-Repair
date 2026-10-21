"""确定性路由（02 §15.2、03 §5、G-03）。

路由函数是**纯函数**：只读 ``RepairState``，不碰 deps、不写事件、不改状态。
03 §5 要求每条分支都有单元测试，只有纯函数才谈得上"分支可测"——一旦路由里
藏了副作用，路径测试就只能跑整张图然后从结果反推它走了哪条边。

三条贯穿全表的约定：

1. ``state.status`` 不是 ``running`` 就是短路信号。任何节点把状态写成终态，
   下一条边一律指向 ``finalize``。这让"错误不能被转换成假成功"有一条统一
   的执行路径，而不是每个节点各自决定要不要继续。
2. ``ENV_ERROR`` / ``SANDBOX_ERROR`` **永远不进诊断循环**（02 §13.4）。OOM 在
   Phase 4 已被映射成 ``SANDBOX_ERROR/OOM_KILLED``，所以它天然走这条终止边，
   不需要、也不允许在这里再判一次 ``error_reason == "OOM_KILLED"``。
3. ``Diagnosis.confidence`` 不参与任何路由决策（02 §14.6 末句）。模型的自报
   置信度没有校准，用它分支会得到一个无法调试的系统。

Phase 9 使用注入的 RegressionRunner / PatchReviewer。兼容旧调用方的 None 仍表示
未测量；完整性和高危问题由确定性路由及 finalize 强制核验。
"""

from __future__ import annotations

from enum import StrEnum

from codepilot.schemas.enums import ExecutionStatus, RunStatus
from codepilot.schemas.reproduction import ReproductionStatus
from codepilot.schemas.state import RepairState

__all__ = [
    "Node",
    "pending_review_calls",
    "reproduction_target_tests",
    "review_preconditions",
    "route_after_check_budget",
    "route_after_diagnose",
    "route_after_regression",
    "route_after_review",
    "route_after_rules",
    "route_after_target_tests",
    "route_after_validate_patch",
    "selected_target_tests",
    "terminated",
]


class Node(StrEnum):
    """02 §15.2 的 16 个节点名；同时是事件里 ``node`` 字段的取值。"""

    UNDERSTAND_ISSUE = "understand_issue"
    RETRIEVE = "retrieve"
    LOCALIZE = "localize"
    ANALYZE_IMPACT = "analyze_impact"
    BUILD_ENV = "build_env"
    REPRODUCE = "reproduce"
    PLAN = "plan"
    GENERATE_PATCH = "generate_patch"
    VALIDATE_PATCH = "validate_patch"
    RUN_TARGET_TESTS = "run_target_tests"
    RUN_REGRESSION = "run_regression"
    REVIEW = "review"
    DIAGNOSE = "diagnose"
    CHECK_BUDGET = "check_budget"
    REPAIR_PATCH = "repair_patch"
    FINALIZE = "finalize"


def terminated(state: RepairState) -> bool:
    """节点是否已把 run 推进到终态（错误、停止或成功）。"""
    return state.status is not RunStatus.RUNNING


def _or_finalize(state: RepairState, nxt: Node) -> Node:
    return Node.FINALIZE if terminated(state) else nxt


def route_after_rules(state: RepairState, nxt: Node) -> Node:
    """线性节点的唯一规则：没有终态就走下一个节点。"""
    return _or_finalize(state, nxt)


def reproduction_target_tests(state: RepairState) -> tuple[str, ...]:
    """复现成功时的专用目标测试 NodeID。

    只取"与最终 ``test_path`` 同文件且带 node_id"的历史条目：复现过程中被否决的
    候选也写在 history 里，把它们当目标测试会让一个从未落盘的文件被送去执行。
    """
    reproduction = state.reproduction
    if reproduction is None or reproduction.status is not ReproductionStatus.REPRODUCED:
        return ()
    path = reproduction.test_path
    return tuple(
        item.node_id
        for item in reproduction.history
        if item.node_id is not None and item.test_path == path
    )


def selected_target_tests(state: RepairState) -> tuple[str, ...]:
    """ "目标测试"的口径（02 §15.2 末段）：复现测试优先，否则 Planner 选的已有测试。"""
    repro = reproduction_target_tests(state)
    if repro:
        return repro
    if state.plan is None:
        return ()
    return tuple(state.plan.tests_to_run)


def route_after_validate_patch(state: RepairState) -> Node:
    """validate_patch → check_budget | run_target_tests | run_regression | finalize。

    无效补丁**不执行测试**，直接进 check_budget：那里才会数出"连续三次校验失败"
    并触发 ``validation_loop``（02 §15.5）。
    """
    if terminated(state):
        return Node.FINALIZE
    attempt = state.current_attempt
    if attempt is None:
        # 校验节点没留下任何 Attempt：这是流程错误，不能猜它成功。
        return Node.FINALIZE
    if not attempt.validation.passed:
        return Node.CHECK_BUDGET
    if not selected_target_tests(state):
        # 02 §15.2：没有目标测试时直接进入回归/待验收边界，不伪造目标测试通过。
        return Node.RUN_REGRESSION
    return Node.RUN_TARGET_TESTS


def route_after_target_tests(state: RepairState) -> Node:
    """run_target_tests → run_regression | diagnose | finalize。"""
    if terminated(state):
        return Node.FINALIZE
    execution = state.execution
    if execution is None:
        return Node.FINALIZE
    if execution.is_environment_problem:
        return Node.FINALIZE
    if execution.status is ExecutionStatus.PASS:
        return Node.RUN_REGRESSION
    return Node.DIAGNOSE


def route_after_regression(state: RepairState) -> Node:
    """run_regression → review | diagnose | finalize。

    ``regression is None`` 是**未测量**（Phase 8 的真实状态），不是"没有回归"，
    所以它继续走 review 而不进入诊断；最终成功判定由 finalize 保守处理。
    """
    if terminated(state):
        return Node.FINALIZE
    regression = state.regression
    if regression is not None and regression.has_regression:
        return Node.DIAGNOSE
    if regression is not None and not regression.complete:
        return Node.FINALIZE
    return Node.REVIEW


def review_preconditions(state: RepairState) -> str | None:
    """独立审查的前置条件；不满足时返回**未执行原因**，满足时返回 ``None``。

    这条判据同时被 ``review`` 节点与 :class:`~codepilot.agents.reviewer.PatchReviewer`
    使用，所以它是"不满足前置条件时不得强行调用 Reviewer"的唯一口径（Phase 11 场景 B）：
    没有真实候选补丁、目标测试没跑过或跑成失败、回归没测完时，让模型对着一句
    "我觉得修好了"产出一个 approved，比不调用 Reviewer 有害得多。
    """
    attempt = state.current_attempt
    if attempt is None:
        return "no attempt: the coding stage produced no patch record"
    if not attempt.validation.passed:
        return "the current patch was not approved by PatchValidator"
    if state.issue_analysis is None:
        return "no issue analysis to review against"
    if state.execution is None:
        return "target tests were never executed"
    if state.execution.status is not ExecutionStatus.PASS:
        return f"target tests ended as {state.execution.status.value}"
    if state.regression is None:
        return "regression was not measured"
    if not state.regression.complete:
        return f"regression is incomplete ({state.regression.status})"
    if state.regression.has_regression:
        return f"regression confirmed new failures: {sorted(state.regression.new_failures)[:5]}"
    return None


def pending_review_calls(state: RepairState, *, candidate_digest: str = "") -> int:
    """闭环还**欠着**几次独立审查（预留的计数口径）。

    与 :func:`review_preconditions` 的区别必须说清，否则两者会被当同一个东西用：
    那一条判的是"**此刻**能不能审"，这一条判的是"还要为审查留多少钱"。编码派发时
    补丁还不存在，用前置条件去算预留会一律得出 0 —— 于是"给审查留钱"这件事从未
    发生过，闭环照样饿死。

    计数规则（与 ``max_review_calls=2`` 的"初审 + 一次高危复审"政策同源）：

    * run 已进终态 → 0：审查不再可能发生，预留必须立刻释放，不得永久占用预算。
    * 审查专属额度已花完 → 0：再有请求也只会被 ``budget_review_calls`` 拦下。
    * 当前候选补丁**已有**审查结论 → 初审不再占额度；只有结论里带 high 且
      ``review_repairs`` 还没用掉时，才欠一次复审。
    * 当前候选补丁**没有**审查结论 → 欠 1 次初审；``review_repairs`` 未用掉时
      还可能再欠 1 次复审。
    * 结果受剩余专属额度封顶：为审不成的调用留钱等于白留。

    ``candidate_digest`` 由调用方用 :func:`~codepilot.graph.stop_rules.patch_hash`
    算出后传入：本模块刻意只做纯路由判定，不引入哈希依赖。
    """
    if terminated(state):
        return 0
    allowance = state.budget.max_review_calls - state.review_calls
    if allowance <= 0:
        return 0
    review = state.review
    reviewed_current = (
        review is not None
        and candidate_digest != ""
        and review.reviewed_patch_hash == candidate_digest
    )
    if reviewed_current and review is not None:
        # 初审已经落在这版补丁上：只有"high + 还没用过修复轮"才欠一次复审，
        # 而那一次修复之后的复审就是最后一次（review_repairs 上限为 1）。
        owed = 1 if review.has_high_issues and state.review_repairs < 1 else 0
        return min(owed, allowance)
    owed = 1
    if state.review_repairs < 1:
        owed += 1
    return min(owed, allowance)


def route_after_review(state: RepairState) -> Node:
    """review → finalize | check_budget。

    仅有效高危问题允许一次额外修复；review_repairs 与既有预算共同约束。
    """
    if terminated(state):
        return Node.FINALIZE
    review = state.review
    if review is None:
        return Node.FINALIZE
    if not review.has_high_issues or state.review_repairs >= 1:
        return Node.FINALIZE
    return Node.CHECK_BUDGET


def route_after_diagnose(state: RepairState) -> Node:
    """diagnose → check_budget | finalize。诊断本身不做停止决策。"""
    return _or_finalize(state, Node.CHECK_BUDGET)


def route_after_check_budget(state: RepairState, *, stop: bool) -> Node:
    """check_budget → finalize | repair_patch。

    ``stop`` 由调用方从 :mod:`codepilot.graph.stop_rules` 传入而不是在这里计算：
    保持路由为纯函数，同时让"停止判定"能被单独测试。
    """
    if terminated(state) or stop:
        return Node.FINALIZE
    return Node.REPAIR_PATCH
