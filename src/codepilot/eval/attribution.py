"""评测归因（Harness Hardening）。

一件事有三个不同的问题，以前被压成一个：

1. **派发完成了吗**（runner 层：预算截停、任务没跑）。
2. **Agent 执行完成了吗**（图层：终态、归因、有没有产出补丁）。
3. **最终补丁被独立评测判定了吗**（评测层：RESOLVED / NOT_RESOLVED / 没测出来）。

首次真实 GLM 运行把三层压扁后的后果演了一遍：``execution_state=completed``
（外层 runner 确实跑完了）、``stop_reason=workflow_error``（Harness 把一次可纠正的
计划字段错误当成崩溃）、补丁为空、Validator/Regression/Reviewer 一个都没执行——
而报告里写的是 ``evaluation_status=NOT_RESOLVED``、``resolved=false``、
``environment_failures=0``。读者会得出"模型没修好"，事实是"模型没被给到修的机会"。

这里的规则是**先问有没有结论，再问结论是什么**：拿不出可评测补丁的运行不能被
当成一次普通的失败计数。同时，归因绝不缩小总体分母——HARNESS_ERROR 依然留在
端到端分母里（否则一次崩溃就把成功率抬高了），只是不再冒充"模型修复失败"。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Attribution", "EvaluationState", "attribute_run", "is_model_side"]


class Attribution(StrEnum):
    """一次 (task, seed) 观测的归因类别。互斥且完备。"""

    RESOLVED = "resolved"
    MODEL_NOT_RESOLVED = "model_not_resolved"
    MODEL_INVALID_PLAN = "model_invalid_plan"
    MODEL_OUTPUT_ERROR = "model_output_error"
    HARNESS_ERROR = "harness_error"
    SANDBOX_ERROR = "sandbox_error"
    ENV_ERROR = "env_error"
    BUDGET_STOPPED = "budget_stopped"
    EVALUATION_NOT_RUN = "evaluation_not_run"
    EVALUATION_ERROR = "evaluation_error"


class EvaluationState(StrEnum):
    """独立评测到底有没有发生。与 :class:`Attribution` 正交。"""

    EVALUATED = "evaluated"
    NOT_RUN = "not_run"
    ERROR = "error"


#: 模型侧的归因——只有这些才进入"模型能力"的解读。
_MODEL_SIDE: frozenset[Attribution] = frozenset(
    {
        Attribution.RESOLVED,
        Attribution.MODEL_NOT_RESOLVED,
        Attribution.MODEL_INVALID_PLAN,
        Attribution.MODEL_OUTPUT_ERROR,
    }
)

#: Harness / 环境 / 沙箱侧的归因。
_INFRA_SIDE: frozenset[Attribution] = frozenset(
    {
        Attribution.HARNESS_ERROR,
        Attribution.SANDBOX_ERROR,
        Attribution.ENV_ERROR,
        Attribution.EVALUATION_ERROR,
    }
)


def is_model_side(attribution: Attribution) -> bool:
    return attribution in _MODEL_SIDE


def is_infra_side(attribution: Attribution) -> bool:
    return attribution in _INFRA_SIDE


def attribute_run(
    *,
    execution_state: str,
    evaluation_state: EvaluationState,
    resolved: bool | None,
    error_class: str | None = None,
    error_origin: str | None = None,
    stop_reason: str | None = None,
    status: str | None = None,
    patch_produced: bool = False,
    claims_success: bool = False,
) -> Attribution:
    """把一次观测归到一个类别。**纯函数**，判定顺序即文档。

    顺序是有意的：派发层的问题先于执行层，执行层先于评测层。反过来先问"评了吗"，
    一次 Harness 崩溃会被算成"模型没修好"——那正是本轮要消灭的误读。
    """
    if execution_state in {"not_executed", "budget_blocked"}:
        return Attribution.BUDGET_STOPPED
    if execution_state == "evaluation_error":
        return Attribution.EVALUATION_ERROR

    budget_stopped = (stop_reason or "").startswith("budget_") or status == "budget_exhausted"
    sandbox_stopped = (stop_reason or "") in {"sandbox_failure", "validation_loop"} or status in {
        "sandbox_error"
    }
    env_stopped = status == "env_error" or (stop_reason or "") == "env_error"

    if evaluation_state is EvaluationState.EVALUATED:
        # 评测判定了就按判定说；但基础设施问题仍要单独可见（它们可能评出个错的结论）。
        if env_stopped:
            return Attribution.ENV_ERROR
        if sandbox_stopped:
            return Attribution.SANDBOX_ERROR
        if budget_stopped:
            return Attribution.BUDGET_STOPPED
        if resolved is True:
            return Attribution.RESOLVED
        if resolved is False:
            return Attribution.MODEL_NOT_RESOLVED
        return Attribution.EVALUATION_ERROR

    # 没做独立评测：先说清楚是**谁**让评测没发生的。
    if error_class == "plan_semantic_invalid":
        return Attribution.MODEL_INVALID_PLAN
    if error_class == "target_test_unavailable":
        return Attribution.MODEL_OUTPUT_ERROR
    if error_class == "model_output_invalid" and error_origin == "model":
        return Attribution.MODEL_OUTPUT_ERROR
    if env_stopped:
        return Attribution.ENV_ERROR
    if sandbox_stopped:
        return Attribution.SANDBOX_ERROR
    if budget_stopped:
        return Attribution.BUDGET_STOPPED
    if error_origin in {"provider", "tool", "workspace", "harness"}:
        # provider 故障不是模型能力问题，也不是"环境坏了"；它归到 Harness 一侧，
        # 因为它衡量的是我们这套接入的可靠性。
        return Attribution.HARNESS_ERROR
    if error_class == "llm_provider_error" or error_class == "llm_request_uncertain":
        return Attribution.HARNESS_ERROR
    if patch_produced:
        # 有补丁却没评测：评测器没跑起来，是我们没测到，不是模型没做出来。
        return Attribution.EVALUATION_NOT_RUN
    if claims_success:
        # Agent 自称成功但没有任何独立评测——绝不写成 resolved，也不写成模型失败。
        return Attribution.EVALUATION_NOT_RUN
    if stop_reason == "workflow_error" or status == "error":
        return Attribution.HARNESS_ERROR
    return Attribution.EVALUATION_NOT_RUN
