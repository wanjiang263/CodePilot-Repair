"""失败归因分类（Harness Hardening）。

这里解决的问题只有一个：**谁错了**。

真实 GLM 单任务实验里，Planner 的语义校验抛出一个 ``PathAccessError``，节点边界的
``except Exception`` 把它写成 ``workflow_error`` ——于是一次"模型多写了一个
``planned:`` 前缀、本可以纠正"的字段错误，在报告里变成了"Harness 崩溃"，
并且继续被计入模型未解决的分母。两个结论都是错的，而且错得看不出来。

因此异常在跨越节点边界时必须先被翻译成 :class:`AgentFailureError`：

* ``origin=model`` —— 模型输出有问题。可纠正的就给纠正反馈，纠正不动的就显式终止。
* ``origin=provider`` —— 服务商侧失败（限流、5xx、密钥没配）。与模型能力无关。
* ``origin=tool`` / ``workspace`` —— 路径越界、权限拒绝。属于工具契约，不是崩溃。
* ``origin=harness`` —— 真的程序异常。**只有这一类**留在 ``workflow_error``。

``PathAccessError`` 在 :func:`classify_exception` 里**延迟导入**：
``tools.base`` → ``llm.types`` → ``llm/__init__`` → ``llm.structured`` → 本模块，
在模块顶层反向 import 会撞上一个还没初始化完的 ``tools.base``。

:func:`classify_exception` 是唯一的翻译点，默认值刻意是
``HARNESS_INTERNAL_ERROR``：分类不确定时宁可承认"是我们的问题"，
也不要把基础设施缺陷算到模型头上。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from codepilot.llm.errors import LLMError, StructuredOutputError
from codepilot.observability.redaction import redact_text

__all__ = [
    "AgentFailureError",
    "ErrorOrigin",
    "FailureClass",
    "PlanSemanticError",
    "SemanticFieldError",
    "attribution_fields",
    "classify_exception",
    "terminal_status_for",
]


class FailureClass(StrEnum):
    """失败的大类。值进入 ``RepairState.error_class``、RunReport 与评测归因。"""

    MODEL_OUTPUT_INVALID = "model_output_invalid"
    PLAN_SEMANTIC_INVALID = "plan_semantic_invalid"
    TOOL_PERMISSION_DENIED = "tool_permission_denied"
    LLM_PROVIDER_ERROR = "llm_provider_error"
    LLM_REQUEST_UNCERTAIN = "llm_request_uncertain"
    WORKSPACE_ERROR = "workspace_error"
    HARNESS_INTERNAL_ERROR = "harness_internal_error"


class ErrorOrigin(StrEnum):
    """责任归属。与 :class:`FailureClass` 分开：同一类问题可能来自不同侧。"""

    MODEL = "model"
    PROVIDER = "provider"
    TOOL = "tool"
    WORKSPACE = "workspace"
    HARNESS = "harness"


#: 路径策略里属于"权限/边界被拒"的码；其余 ``PathAccessError`` 码是"路径本身不对"。
_PERMISSION_CODES = frozenset({"path_escape", "forbidden_path", "identity_changed"})


class SemanticFieldError(BaseModel):
    """一条带字段路径的语义错误。

    必须带 ``field``：只说"计划不合法"的反馈改不动模型——它会重复同一个错误，
    于是纠正重试退化成纯粹的费用消耗。
    """

    field: str
    code: str
    reason: str
    hint: str | None = None
    value: str | None = None

    def render(self) -> str:
        shown = f"（你给的是 {self.value!r}）" if self.value else ""
        line = f"- 字段 `{self.field}` [{self.code}]：{self.reason}{shown}"
        return line + (f"\n  纠正方式：{self.hint}" if self.hint else "")


class AgentFailureError(RuntimeError):
    """节点边界可识别的失败，携带归因所需的结构化信息。"""

    def __init__(
        self,
        message: str,
        *,
        failure_class: FailureClass,
        origin: ErrorOrigin,
        node: str | None = None,
        field_errors: tuple[SemanticFieldError, ...] = (),
        corrections_used: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.failure_class = failure_class
        self.origin = origin
        self.node = node
        self.field_errors = field_errors
        self.corrections_used = corrections_used
        self.details: dict[str, Any] = details or {}

    @property
    def retryable(self) -> bool:
        """模型侧的错误值得再问一次；其余都不该靠重试掩盖。"""
        return self.origin is ErrorOrigin.MODEL

    @property
    def is_harness_defect(self) -> bool:
        return self.origin is ErrorOrigin.HARNESS

    def reason(self) -> str:
        """写进 ``failure_reasons`` 的一行说明（已脱敏）。"""
        prefix = f"{self.node}: " if self.node else ""
        return redact_text(
            f"{prefix}[{self.failure_class.value}/{self.origin.value}] {self.message}"
        )

    def __str__(self) -> str:
        return self.reason()


class PlanSemanticError(AgentFailureError):
    """JSON 合法、Pydantic 通过，但计划本身不可执行或越权。"""

    def __init__(
        self,
        message: str,
        field_errors: tuple[SemanticFieldError, ...] | list[SemanticFieldError],
        *,
        node: str | None = None,
        corrections_used: int = 0,
    ) -> None:
        super().__init__(
            message,
            failure_class=FailureClass.PLAN_SEMANTIC_INVALID,
            origin=ErrorOrigin.MODEL,
            node=node,
            field_errors=tuple(field_errors),
            corrections_used=corrections_used,
        )

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.field_errors)

    def correction_prompt(self) -> str:
        """给模型的纠正反馈：只给必要的错误，不复述上一轮的源码或上下文。"""
        return "\n".join(
            [
                "你上一轮的 RepairPlan 通过了 JSON Schema 校验，但没有通过语义与权限校验，"
                "因此不能执行。请只修正下面列出的字段，然后重新输出**完整**的 RepairPlan JSON 对象。",
                "",
                "契约：",
                "- `tests_to_run` 只填仓库里**已存在**、且被授权执行的测试文件路径或 pytest "
                "NodeID（例如 `tests/test_ledger.py::test_add_rejects_negative`）。"
                "它是「可执行目标」清单，不是「打算写的测试」清单。",
                "- 打算**新增**的测试写进 `planned_new_tests`（例如 `tests/test_ledger.py::test_new`），"
                "不要加 `planned:` 之类的前缀，也不要写进 `tests_to_run`。"
                "是否允许新增测试由运行配置决定，不由你决定。",
                "- 已有的测试文件受保护：不得计划修改或删除它们。",
                "- 如果没有任何可信的既有测试可用来验证，就把 `tests_to_run` 留空，"
                "并在 `root_cause_hypothesis` 里说明缺少可信测试；不要伪造测试目标。",
                "",
                "需要修正的错误：",
                *(item.render() for item in self.field_errors),
            ]
        )


def classify_exception(exc: BaseException, *, node: str | None = None) -> AgentFailureError:
    """把任意异常翻译成带归因的 :class:`AgentFailureError`；**不抛异常**。

    未识别的一律 ``HARNESS_INTERNAL_ERROR``：这不是省事，而是方向性的选择——
    把未知错误默认算成模型错误，等于让评测报告替 Harness 免责。
    """
    from codepilot.tools.base import PathAccessError

    if isinstance(exc, AgentFailureError):
        if exc.node is None:
            exc.node = node
        return exc

    if isinstance(exc, StructuredOutputError):
        semantic = exc.last_stage == "semantic"
        return AgentFailureError(
            f"模型输出连续 {exc.attempts} 次未通过"
            f"{'语义' if semantic else ' JSON Schema'}校验："
            f"{redact_text(exc.last_error or str(exc))}",
            failure_class=(
                FailureClass.PLAN_SEMANTIC_INVALID
                if semantic
                else FailureClass.MODEL_OUTPUT_INVALID
            ),
            origin=ErrorOrigin.MODEL,
            node=node,
            corrections_used=exc.attempts,
            details={"last_content": redact_text((exc.last_content or "")[:500])},
        )
    if isinstance(exc, ValidationError):
        errors = [
            SemanticFieldError(
                field=".".join(str(part) for part in item.get("loc", ())) or "<root>",
                code=str(item.get("type", "invalid")),
                reason=str(item.get("msg", "invalid")),
            )
            for item in exc.errors()[:10]
        ]
        return AgentFailureError(
            f"模型输出不符合 Schema（{len(errors)} 处）",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            origin=ErrorOrigin.MODEL,
            node=node,
            field_errors=tuple(errors),
        )
    if isinstance(exc, LLMError):
        uncertain = bool(getattr(exc, "uncertain", False))
        return AgentFailureError(
            redact_text(str(exc)),
            failure_class=(
                FailureClass.LLM_REQUEST_UNCERTAIN if uncertain else FailureClass.LLM_PROVIDER_ERROR
            ),
            origin=ErrorOrigin.PROVIDER,
            node=node,
            details={"error_type": type(exc).__name__, "status": getattr(exc, "status", None)},
        )
    if isinstance(exc, PathAccessError):
        denied = exc.code in _PERMISSION_CODES
        return AgentFailureError(
            redact_text(str(exc)),
            failure_class=(
                FailureClass.TOOL_PERMISSION_DENIED if denied else FailureClass.WORKSPACE_ERROR
            ),
            origin=ErrorOrigin.TOOL if denied else ErrorOrigin.WORKSPACE,
            node=node,
            details={"path_error_code": exc.code},
        )
    if isinstance(exc, LookupError):
        # ``UnregisteredToolError``：模型请求了不存在的工具，属于可纠正的输出问题。
        return AgentFailureError(
            redact_text(str(exc)),
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
            origin=ErrorOrigin.MODEL,
            node=node,
        )
    return AgentFailureError(
        f"{type(exc).__name__}: {redact_text(str(exc))}",
        failure_class=FailureClass.HARNESS_INTERNAL_ERROR,
        origin=ErrorOrigin.HARNESS,
        node=node,
        details={"exception_type": type(exc).__name__},
    )


def terminal_status_for(failure: AgentFailureError) -> dict[str, Any]:
    """归因 → 图终态。

    计划本身被拒（语义不合法，或 JSON Schema 连续纠正失败）写 ``plan_rejected``；
    其余仍留在 ``error`` + ``workflow_error``，但 ``error_class`` / ``error_origin``
    已经写进状态，评测层因此能把"服务商 5xx"、"权限拒绝"与"真的程序崩溃"区分开——
    判定看归因字段，不再只看 ``stop_reason``。
    """
    from codepilot.schemas.enums import RunStatus, StopReason

    rejected_plan = failure.failure_class is FailureClass.PLAN_SEMANTIC_INVALID or (
        failure.failure_class is FailureClass.MODEL_OUTPUT_INVALID and failure.node == "plan"
    )
    if rejected_plan:
        return {"status": RunStatus.PLAN_REJECTED, "stop_reason": StopReason.PLAN_REJECTED}
    return {"status": RunStatus.ERROR, "stop_reason": StopReason.WORKFLOW_ERROR}


def attribution_fields(failure: AgentFailureError) -> dict[str, Any]:
    """写进 ``RepairState`` 的归因字段。"""
    return {
        "error_class": failure.failure_class.value,
        "error_origin": failure.origin.value,
    }
