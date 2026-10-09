"""LLM 调用层的接口类型（02 §5.1）。

``LLMRequest`` / ``LLMResponse`` 是**唯一**允许跨越 LLM 边界的结构。
Agent 只能看到这两个类型和 ``LLMProvider`` Protocol，永远不该 import
``httpx``、``openai`` 或任何具体 provider 的名字——否则"换一家服务商"
就变成了一次全局重构。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LLMRequest",
    "LLMResponse",
    "Message",
    "Role",
    "ToolCall",
    "ToolSpec",
    "request_fingerprint",
]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """模型发起的一次工具调用。

    ``arguments`` 是**已解析**的 dict。参数是非法 JSON 时不在这里抛异常：
    按 02 §5.3，那属于"一次结构化输出失败"，要带错误信息重试，所以这里保留
    ``raw_arguments`` 并让上层决定怎么重试。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None
    arguments_error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.arguments_error is None


class ToolSpec(BaseModel):
    """暴露给模型的工具声明。``parameters`` 是 JSON Schema。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    def as_openai_tool(self) -> dict[str, Any]:
        """OpenAI Chat Completions 的 ``tools`` 数组元素。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Message(BaseModel):
    """一条对话消息。

    ``reasoning_content``（推理模型的思考内容）**只进不出**：它被记录到
    ``llm_calls.jsonl``，但不放回下一轮对话，也不进入 ``RepairState``
    （02 §5.3）。原因是多数服务商拒绝接受带思考内容的历史消息。
    """

    role: Role
    content: str | None = None

    tool_calls: list[ToolCall] = Field(default_factory=list)
    """``role=assistant`` 且模型发起了工具调用时非空。"""

    tool_call_id: str | None = None
    """``role=tool`` 时指回对应的 ``ToolCall.id``。"""

    name: str | None = None

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str | None) -> Message:
        return cls(role=Role.ASSISTANT, content=content)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Message:
        return cls(role=Role.TOOL, content=content, tool_call_id=tool_call_id)


class LLMRequest(BaseModel):
    """一次调用请求。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: list[Message]
    tools: list[ToolSpec] | None = None
    response_schema: type[BaseModel] | None = None

    temperature: float = 0.0
    max_output_tokens: int = 4096

    purpose: str = "generic"
    """``"plan"`` / ``"code"`` / ``"diagnose"`` …… 用于按环节统计成本。

    它的存在使得"钱花在哪一步"这个问题可以回答，而不是只知道总花费。
    """

    def has_schema(self) -> bool:
        return self.response_schema is not None


class LLMResponse(BaseModel):
    """一次调用的结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    parsed: BaseModel | None = None

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0

    cost: float = 0.0
    cost_known: bool = True
    """``False`` 表示该模型没有配置单价，``cost`` 只是占位 0。

    必须显式区分，否则"没配价格"和"这次调用免费"在报表里长得一模一样。
    """

    model_alias: str = ""
    latency_ms: int = 0

    reasoning_content: str | None = None
    """思考内容。单独保存，不回流到后续消息。"""

    raw: dict[str, Any] | None = None
    """provider 返回的原始 JSON。只在排查兼容性问题时用。"""


def request_fingerprint(request: LLMRequest) -> str:
    """请求的稳定指纹，用于 ``RecordingLLM`` 与 ``ReplayLLM`` 配对。

    刻意**不**用 ``request.model_dump_json()``：``response_schema`` 是一个
    类型对象，序列化结果依赖类定义的位置，跨进程不稳定。这里改用它的
    ``model_json_schema()``，那是只由字段定义决定的纯数据。

    ``temperature`` 与 ``max_output_tokens`` 参与指纹：它们会改变模型输出，
    回放时必须匹配，否则录制的响应可能对应一次完全不同的调用。
    """
    schema: dict[str, Any] | None = None
    if request.response_schema is not None:
        schema = request.response_schema.model_json_schema()

    canonical = {
        "messages": [
            {
                "role": m.role.value,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "name": m.name,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
                ],
            }
            for m in request.messages
        ],
        "tools": (None if request.tools is None else [t.as_openai_tool() for t in request.tools]),
        "schema": schema,
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "purpose": request.purpose,
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
