"""能力差异处理（02 §5.3 的能力差异表）。

这是**唯一**允许出现"因为这家不支持 X 所以换个做法"的地方，而且判断依据是
配置里的能力字段，不是 provider 名字。业务代码里一旦出现
``if provider == "glm"``，接入第四家时就要去全仓库找分支。

四类差异都在这里落地：

======================  ==========================================
差异                     处理
======================  ==========================================
不支持原生 tool calling   prompt 内的 JSON 工具调用协议
不支持 JSON 模式          只把 Schema 写进 prompt，靠校验 + 重试
不支持 temperature        不发送该参数
上下文长度不同            预算 = min(角色预算, context_window × 0.75)
======================  ==========================================
"""

from __future__ import annotations

import json
from typing import Any

from codepilot.llm.config import ModelSpec
from codepilot.llm.types import LLMRequest, Message, ToolCall, ToolSpec

__all__ = [
    "CONTEXT_SAFETY_RATIO",
    "build_request_body",
    "effective_context_budget",
    "parse_tool_protocol",
    "prepare_messages",
    "tool_protocol_prompt",
]

CONTEXT_SAFETY_RATIO = 0.75
"""上下文预算系数。

留 25% 给输出与估算误差。用满 ``context_window`` 会在长对话的最后一轮
突然报超限，而那时已经花了钱。
"""

_TOOL_PROTOCOL_HEADER = """\
你**没有**原生工具调用能力。需要调用工具时，只输出一个 JSON 对象，不要有任何其他文字：

{"tool": "<工具名>", "arguments": {<参数>}}

可用工具：
"""

_TOOL_PROTOCOL_FOOTER = """\

每轮只能调用一个工具。参数必须是合法 JSON 对象。
不需要调用工具时，调用 `finish` 工具并给出最终结论。
"""

_JSON_ONLY_INSTRUCTION = """\

只输出一个 JSON 对象，不要包裹在 ``` 代码块里，不要有任何解释性文字。"""


def effective_context_budget(spec: ModelSpec, role_budget: int) -> int:
    """本次调用可用的输入 token 上限。

    取角色预算与模型上下文上限（打 0.75 折）的较小值。两个都要考虑：
    角色预算控制了"这一步该看多少代码"，上下文上限是模型的硬约束。
    """
    return min(role_budget, int(spec.context_window * CONTEXT_SAFETY_RATIO))


def tool_protocol_prompt(tools: list[ToolSpec]) -> str:
    """为不支持原生 tool calling 的模型生成工具协议说明。"""
    lines = [_TOOL_PROTOCOL_HEADER]
    for tool in tools:
        params = json.dumps(tool.parameters, ensure_ascii=False, sort_keys=True)
        lines.append(f"- {tool.name}: {tool.description}\n  参数 schema: {params}\n")
    lines.append(_TOOL_PROTOCOL_FOOTER)
    return "".join(lines)


def _schema_instruction(request: LLMRequest) -> str:
    assert request.response_schema is not None
    schema = json.dumps(request.response_schema.model_json_schema(), ensure_ascii=False, indent=2)
    if request.response_schema.__doc__:
        head = request.response_schema.__doc__.strip().splitlines()[0]
        intro = f"请输出符合以下 Schema 的 JSON 对象（{head}）：\n"
    else:
        intro = "请输出符合以下 JSON Schema 的对象：\n"
    return intro + schema


def prepare_messages(request: LLMRequest, spec: ModelSpec) -> list[Message]:
    """按能力补齐 prompt。

    追加的说明放在**最前面**（紧跟已有 system 消息之后）：放到末尾在长对话里
    会被淹没，尤其是工具协议这种必须严格遵守的格式约束。
    """
    additions: list[str] = []

    if request.tools and not spec.supports_tools:
        additions.append(tool_protocol_prompt(request.tools))

    if request.response_schema is not None:
        additions.append(_schema_instruction(request))
        if not spec.supports_json_mode:
            # 支持 JSON 模式时不重复叮嘱：那个参数本身就强制了输出格式。
            additions.append(_JSON_ONLY_INSTRUCTION)

    if not additions:
        return list(request.messages)

    extra = [Message.system("\n\n".join(additions))]
    messages = list(request.messages)
    # 插在第一条 system 之后，保留原有 system 指令的最高优先级。
    if messages and messages[0].role == "system":
        return [messages[0], *extra, *messages[1:]]
    return [*extra, *messages]


def build_request_body(
    request: LLMRequest, spec: ModelSpec, messages: list[Message]
) -> dict[str, Any]:
    """构造 OpenAI 兼容的请求体。"""
    body: dict[str, Any] = {
        "model": spec.model,
        "messages": [_encode_message(m) for m in messages],
        "max_tokens": request.max_output_tokens,
    }

    if spec.supports_temperature:
        body["temperature"] = request.temperature

    if request.tools and spec.supports_tools:
        body["tools"] = [t.as_openai_tool() for t in request.tools]
        body["tool_choice"] = "auto"

    if request.response_schema is not None and spec.supports_json_mode:
        body["response_format"] = {"type": "json_object"}

    return body


def _encode_message(message: Message) -> dict[str, Any]:
    """把 ``Message`` 编码成 OpenAI Chat Completions 的消息格式。"""
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content or "",
        }

    encoded: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        encoded["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.raw_arguments
                    if tc.raw_arguments is not None
                    else json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in message.tool_calls
        ]
    if message.name:
        encoded["name"] = message.name
    return encoded


def parse_tool_protocol(content: str) -> ToolCall | None:
    """解析 prompt 内 JSON 工具协议。

    解析失败返回 ``None``（让上层当作普通文本处理）；``arguments`` 不是对象
    时返回一个带 ``arguments_error`` 的 ``ToolCall``，当作一次结构化输出失败
    重试（02 §5.3 表末行）。
    """
    text = content.strip()
    if text.startswith("```"):
        # 模型经常会包一层代码块，虽然 prompt 里明确说了不要。
        text = text.strip("`")
        _, _, text = text.partition("\n")
        text = text.rstrip("`").strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or "tool" not in data:
        return None

    name = data.get("tool")
    if not isinstance(name, str) or not name:
        return None

    arguments = data.get("arguments", {})
    raw = json.dumps(arguments, ensure_ascii=False)
    if not isinstance(arguments, dict):
        return ToolCall(
            id=f"protocol_{name}",
            name=name,
            arguments={},
            raw_arguments=raw,
            arguments_error=f"arguments 必须是 JSON 对象，收到 {type(arguments).__name__}",
        )

    return ToolCall(id=f"protocol_{name}", name=name, arguments=arguments, raw_arguments=raw)
