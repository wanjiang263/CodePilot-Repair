"""L-02 / L-03 的证明：能力差异完全由配置字段驱动，不看 provider 名字。

这些测试同时是"禁止 if provider == ..."这条规则的可执行版本：
把能力字段翻过来，请求体就应该跟着变，不需要改任何别的东西。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from codepilot.llm.capabilities import (
    CONTEXT_SAFETY_RATIO,
    build_request_body,
    effective_context_budget,
    parse_tool_protocol,
    prepare_messages,
    tool_protocol_prompt,
)
from codepilot.llm.config import ModelSpec
from codepilot.llm.types import LLMRequest, Message, ToolSpec


class _Answer(BaseModel):
    """用来验证 Schema 会被写进 prompt。"""

    line: int
    reason: str


def _spec(**overrides: Any) -> ModelSpec:
    base: dict[str, Any] = {
        "alias": "m",
        "provider": "p",
        "model": "real-model",
        "context_window": 100_000,
    }
    base.update(overrides)
    return ModelSpec.model_validate(base)


TOOLS = [
    ToolSpec(
        name="read_file",
        description="读取文件",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
]

MESSAGES = [Message.system("你是助手"), Message.user("开始")]


# --------------------------------------------------------------- 请求体


def test_body_has_required_fields() -> None:
    spec = _spec()
    request = LLMRequest(messages=MESSAGES, purpose="plan")
    body = build_request_body(request, spec, prepare_messages(request, spec))

    assert body["model"] == "real-model"
    assert body["max_tokens"] == 4096
    assert body["messages"][0] == {"role": "system", "content": "你是助手"}


def test_temperature_sent_when_supported() -> None:
    spec = _spec(supports_temperature=True)
    request = LLMRequest(messages=MESSAGES, temperature=0.7)
    body = build_request_body(request, spec, request.messages)
    assert body["temperature"] == 0.7


def test_temperature_omitted_when_unsupported() -> None:
    """有些模型只接受默认温度，传了会直接 400。"""
    spec = _spec(supports_temperature=False)
    request = LLMRequest(messages=MESSAGES, temperature=0.7)
    body = build_request_body(request, spec, request.messages)
    assert "temperature" not in body


def test_native_tools_sent_when_supported() -> None:
    spec = _spec(supports_tools=True)
    request = LLMRequest(messages=MESSAGES, tools=TOOLS)
    body = build_request_body(request, spec, prepare_messages(request, spec))
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert body["tool_choice"] == "auto"


def test_tools_omitted_when_unsupported() -> None:
    spec = _spec(supports_tools=False)
    request = LLMRequest(messages=MESSAGES, tools=TOOLS)
    body = build_request_body(request, spec, prepare_messages(request, spec))
    assert "tools" not in body


def test_json_mode_sent_when_supported() -> None:
    spec = _spec(supports_json_mode=True)
    request = LLMRequest(messages=MESSAGES, response_schema=_Answer)
    body = build_request_body(request, spec, prepare_messages(request, spec))
    assert body["response_format"] == {"type": "json_object"}


def test_json_mode_omitted_when_unsupported() -> None:
    spec = _spec(supports_json_mode=False)
    request = LLMRequest(messages=MESSAGES, response_schema=_Answer)
    body = build_request_body(request, spec, prepare_messages(request, spec))
    assert "response_format" not in body


@pytest.mark.parametrize("provider", ["first-service", "second-service"])
@pytest.mark.parametrize("mode", ["enabled", "disabled"])
def test_extra_body_forwarded_without_provider_branches(provider: str, mode: str) -> None:
    spec = _spec(provider=provider, extra_body={"thinking": {"type": mode}})
    request = LLMRequest(messages=MESSAGES)
    body = build_request_body(request, spec, request.messages)
    assert body["thinking"] == {"type": mode}
    assert body["model"] == spec.model
    assert body["max_tokens"] == request.max_output_tokens


def test_request_body_does_not_share_nested_extra_body_values() -> None:
    spec = _spec(extra_body={"thinking": {"type": "disabled"}})
    request = LLMRequest(messages=MESSAGES)
    first = build_request_body(request, spec, request.messages)
    first["thinking"]["type"] = "enabled"
    second = build_request_body(request, spec, request.messages)
    assert spec.extra_body["thinking"] == {"type": "disabled"}
    assert second["thinking"] == {"type": "disabled"}


# --------------------------------------------------------------- prompt 补齐


def test_no_tools_means_no_protocol_prompt() -> None:
    spec = _spec(supports_tools=False)
    request = LLMRequest(messages=MESSAGES)
    assert prepare_messages(request, spec) == MESSAGES


def test_tool_protocol_added_when_no_native_tools() -> None:
    spec = _spec(supports_tools=False)
    request = LLMRequest(messages=MESSAGES, tools=TOOLS)
    messages = prepare_messages(request, spec)
    assert len(messages) == 3
    assert "read_file" in (messages[1].content or "")
    assert '{"tool"' in (messages[1].content or "")


def test_tool_protocol_inserted_after_leading_system_message() -> None:
    """原 system 指令的优先级必须保住，新说明插在它后面。"""
    spec = _spec(supports_tools=False)
    request = LLMRequest(messages=MESSAGES, tools=TOOLS)
    messages = prepare_messages(request, spec)
    assert messages[0].content == "你是助手"
    assert messages[-1].content == "开始"


def test_protocol_inserted_at_front_without_system_message() -> None:
    spec = _spec(supports_tools=False)
    request = LLMRequest(messages=[Message.user("开始")], tools=TOOLS)
    messages = prepare_messages(request, spec)
    assert messages[0].role == "system"
    assert messages[1].content == "开始"


def test_schema_instruction_always_added() -> None:
    """即使支持 JSON 模式，也要把 Schema 告诉模型——那个参数只说"输出 JSON"。"""
    spec = _spec(supports_json_mode=True)
    request = LLMRequest(messages=MESSAGES, response_schema=_Answer)
    messages = prepare_messages(request, spec)
    text = messages[1].content or ""
    assert "line" in text and "reason" in text
    assert "只输出" not in text  # 支持 JSON 模式时不必重复叮嘱


def test_json_only_instruction_added_when_no_json_mode() -> None:
    spec = _spec(supports_json_mode=False)
    request = LLMRequest(messages=MESSAGES, response_schema=_Answer)
    text = prepare_messages(request, spec)[1].content or ""
    assert "只输出" in text


def test_tool_protocol_prompt_lists_every_tool() -> None:
    text = tool_protocol_prompt(TOOLS)
    assert "read_file" in text
    assert "读取文件" in text
    assert "finish" in text


# --------------------------------------------------------------- 上下文预算


def test_context_budget_takes_the_smaller() -> None:
    spec = _spec(context_window=100_000)
    # 角色预算 10000 < 100000*0.75
    assert effective_context_budget(spec, 10_000) == 10_000
    # 角色预算 100000 > 75000
    assert effective_context_budget(spec, 100_000) == int(100_000 * CONTEXT_SAFETY_RATIO)


def test_context_budget_leaves_headroom() -> None:
    """用满 context_window 会在长对话的最后一轮突然超限，而那时已经花了钱。"""
    spec = _spec(context_window=1000)
    assert effective_context_budget(spec, 10_000) < 1000


# --------------------------------------------------------------- 内置工具协议解析


def test_parse_plain_protocol() -> None:
    call = parse_tool_protocol('{"tool": "read_file", "arguments": {"path": "a.py"}}')
    assert call is not None
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.py"}
    assert call.is_valid is True


def test_parse_fenced_protocol() -> None:
    """模型经常无视"不要用代码块"的指示。"""
    call = parse_tool_protocol('```json\n{"tool": "read_file", "arguments": {"path": "a.py"}}\n```')
    assert call is not None
    assert call.arguments == {"path": "a.py"}


def test_parse_protocol_with_surrounding_prose() -> None:
    call = parse_tool_protocol(
        '好的，我需要先看文件。\n{"tool": "read_file", "arguments": {"path": "a.py"}}\n就这样。'
    )
    assert call is not None
    assert call.name == "read_file"


def test_parse_missing_arguments_key_defaults_to_empty() -> None:
    call = parse_tool_protocol('{"tool": "list_files"}')
    assert call is not None
    assert call.arguments == {}


def test_parse_non_object_arguments_is_an_error() -> None:
    """按 02 §5.3，这属于一次结构化输出失败，要重试而不是静默丢弃。"""
    call = parse_tool_protocol('{"tool": "read_file", "arguments": "a.py"}')
    assert call is not None
    assert call.is_valid is False
    assert call.arguments_error is not None


@pytest.mark.parametrize(
    "content",
    ["普通回答，没有工具调用", "", "{}", '{"arguments": {}}', "{不是 JSON}", "```\n```"],
)
def test_parse_returns_none_for_non_tool_output(content: str) -> None:
    assert parse_tool_protocol(content) is None
