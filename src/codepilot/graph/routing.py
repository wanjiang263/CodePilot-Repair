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
    "reproduction_target_tests",
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
