"""Agent 工具调用审计（Harness Hardening）。

审计要回答的是"模型请求过什么、有没有被拒"，而不只是"成功了几次"。首次真实 GLM
运行里 ``Planner.tool_records`` 只活在内存中，节点没写进 state、事件或 artifact，
所以报告里的 ``tool_calls`` 为空 —— 而空会被读成"模型没调用工具"，这是把
**记录缺失**当成了**事实为零**。因此这里的每条审计都带 ``records_present``：
它不存在 = 我们不知道，而不是 0 次。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

__all__ = ["PlannerToolAudit", "PlannerToolRecord", "ToolCallStatus"]

#: 参数摘要的字符上限。审计要能认出"读的是哪个文件"，但不必带上整份文件内容。
_ARGUMENT_SUMMARY_LIMIT = 160


class ToolCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    """工具执行完成且 ``ToolResponse.ok`` 为真。"""

    DENIED = "denied"
    """被权限/路径策略拒绝（越界、受保护路径、未注册给该角色）。"""

    FAILED = "failed"
    """工具本身执行失败（非法参数、索引缺失、运行期错误）。"""


class PlannerToolRecord(BaseModel):
    """Planner 的一次只读工具调用。

    ``arguments`` 只存**脱敏后的摘要**，不存原参数：工具参数里可能出现源码片段，
    而 state / checkpoint / report 会被复制、打包、外发排查。
    """

    tool_call_id: str
    name: str
    role: str = "planner"
    permission: str = "read"
    status: ToolCallStatus
    request_id: str | None = None
    """产生这次调用的模型请求 id；见 :mod:`codepilot.llm.calls`。"""

    arguments_summary: str = ""
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    iteration: int = 0

    @property
    def ok(self) -> bool:
        return self.status is ToolCallStatus.SUCCEEDED

    @classmethod
    def denied(
        cls,
        *,
        tool_call_id: str,
        name: str,
        error_code: str,
        error_message: str,
        arguments_summary: str = "",
        role: str = "planner",
        permission: str = "read",
        request_id: str | None = None,
        iteration: int = 0,
    ) -> PlannerToolRecord:
        return cls(
            tool_call_id=tool_call_id,
            name=name,
            role=role,
            permission=permission,
            status=ToolCallStatus.DENIED,
            request_id=request_id,
            arguments_summary=arguments_summary,
            error_code=error_code,
            error_message=error_message,
            iteration=iteration,
        )


class PlannerToolAudit(BaseModel):
    """一个 Planner 实例在某次 ``plan()`` 调用里的完整工具轨迹。"""

    role: str = "planner"
    node: str = "plan"
    iteration: int = 0
    records: list[PlannerToolRecord] = Field(default_factory=list)
    records_present: bool = True
    """审计是否真的被采集到。``False`` 时**不得**把 ``requested`` 当成 0。"""

    requested_but_denied: int = 0
    """模型请求了工具但被权限层拒绝的次数——只读契约唯一能证明它没被绕过的证据。"""

    @property
    def requested(self) -> int:
        return len(self.records)

    @property
    def succeeded(self) -> int:
        return sum(1 for item in self.records if item.status is ToolCallStatus.SUCCEEDED)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.records if item.status is ToolCallStatus.FAILED)

    def summary(self) -> dict[str, object]:
        return {
            "role": self.role,
            "iteration": self.iteration,
            "records_present": self.records_present,
            "requested": self.requested,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "denied": self.requested_but_denied,
        }
