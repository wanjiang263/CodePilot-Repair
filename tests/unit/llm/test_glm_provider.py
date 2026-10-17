"""GLM 走真实 provider 的离线验收（httpx.MockTransport，零网络、零费用）。

这里的每一段 JSON 都按 GLM 官方 Chat Completions 的返回形状写，包括它特有的
``reasoning_content``、``finish_reason=tool_calls``、以及思考模式下 ``content`` 为空
的情形。断言对象是**仓库里那份 configs/llm.yaml**：配置写错了要在这里炸，而不是等
一次真实付费调用去发现。

MockTransport 通过不等于 GLM 端点已连通——那件事只能由 ``codepilot llm smoke --live``
回答（见 docs/notes/glm-integration.md 的真实冒烟结论）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from codepilot.agents.coder import CodingAgent
from codepilot.llm.capabilities import prepare_messages
from codepilot.llm.config import LLMConfig, ModelSpec, load_llm_config
from codepilot.llm.errors import (
    ContextTooLongError,
    ProviderError,
    ProviderNotConfiguredError,
    StructuredOutputError,
)
from codepilot.llm.glm import GLM_API_KEY_ENV, ZHIPU_API_KEY_ENV
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.structured import complete_structured
from codepilot.llm.types import LLMRequest, Message, ToolCall, ToolSpec
from codepilot.observability.redaction import redact
from codepilot.schemas.diagnosis import Diagnosis
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.review import ReviewResult
from codepilot.tools.base import WorkspacePathPolicy
from codepilot.tools.read_tools import read_file_tool
from codepilot.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]
KEY = "glm-secret-key-abcdef"


class Recorder:
    """按脚本回放响应，同时记下每一个收到的请求。"""

    def __init__(self, *script: httpx.Response | Exception) -> None:
        self._script = list(script)
        self.requests: list[httpx.Request] = []

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.content]

    @property
    def count(self) -> int:
        return len(self.requests)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("响应脚本已用尽，但仍收到新的 HTTP 请求")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _glm_config() -> LLMConfig:
    return load_llm_config(REPO_ROOT / "configs" / "llm.yaml")


def _spec(alias: str) -> ModelSpec:
    return _glm_config().models[alias]


def _provider(
    alias: str,
    recorder: Recorder,
    *,
    base_url: str = "https://open.bigmodel.cn/api/paas/v4",
    policy: RetryPolicy | None = None,
    exchange_rates: dict[str, float] | None = None,
    budget_currency: str = "CNY",
) -> OpenAICompatibleProvider:
    config = _glm_config()
    spec = config.models[alias]
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler), base_url=base_url)
    return OpenAICompatibleProvider(
        spec=spec,
        provider=config.providers[spec.provider],
        api_key=KEY,
        client=client,
        policy=policy or RetryPolicy(max_attempts=3, base_delay=1.0, jitter_ratio=0.0),
        sleep=lambda _s: None,
        exchange_rates=exchange_rates or {"CNY": 1.0},
        budget_currency=budget_currency,
    )


def _chat(content: str | None = "收到", **extra: Any) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    message.update(extra.get("message_extra", {}))
    payload = {
        "model": extra.get("echo_model", "glm-4.7"),
        "choices": [{"message": message, "finish_reason": extra.get("finish_reason", "stop")}],
        "usage": {
            "prompt_tokens": extra.get("prompt_tokens", 1000),
            "completion_tokens": extra.get("completion_tokens", 20),
            "total_tokens": extra.get("prompt_tokens", 1000) + extra.get("completion_tokens", 20),
        },
    }
    if "cached_tokens" in extra:
        # 与官方形状一致：缓存命中数在 prompt_tokens_details 里，不在顶层
        payload["usage"]["prompt_tokens_details"] = {"cached_tokens": extra["cached_tokens"]}
    return httpx.Response(200, json=payload)


def _tool_calls(calls: list[dict[str, Any]], *, content: str | None = None) -> httpx.Response:
    return _chat(
        content,
        finish_reason="tool_calls",
        message_extra={
            "tool_calls": [
                {
                    "id": call.get("id", f"call_{n}"),
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call.get("arguments", "{}"),
                    },
                }
                for n, call in enumerate(calls)
            ]
        },
    )


# --------------------------------------------------------------- 普通对话


def test_text_response_normalizes_into_llmresponse() -> None:
    recorder = Recorder(_chat("已定位到 normalize()", prompt_tokens=1234, completion_tokens=56))
    provider = _provider("glm-main", recorder)

    response = provider.complete(LLMRequest(messages=[Message.user("在哪里")], purpose="plan"))

    assert response.content == "已定位到 normalize()"
    assert response.input_tokens == 1234
    assert response.output_tokens == 56
    assert response.finish_reason == "stop"
    assert response.model_alias == "glm-main"
    assert response.cost_known is True


@pytest.mark.parametrize(
    ("base_url", "alias"),
    [
        ("https://open.bigmodel.cn/api/paas/v4", "glm-main"),
        ("https://api.z.ai/api/paas/v4", "glm-5-zai"),
    ],
)
def test_chat_completions_path_is_correct_on_both_platforms(base_url: str, alias: str) -> None:
    """两家都是 ``<base>/chat/completions``，路径段不能被吞掉也不能变成双斜杠。

    这是"只有真实调用才知道"的那类问题里，唯一能在离线阶段就锁死的部分。
    """
    recorder = Recorder(_chat("收到"))
    provider = _provider(alias, recorder, base_url=base_url)
    provider.complete(LLMRequest(messages=[Message.user("hi")]))

    request = recorder.requests[0]
    assert request.url.path.endswith("/api/paas/v4/chat/completions")
    assert "//chat" not in request.url.path
    assert request.headers["authorization"] == f"Bearer {KEY}"


def test_truncated_response_is_visible_as_truncated() -> None:
    recorder = Recorder(_chat('{"summary": "被砍', finish_reason="length"))
    provider = _provider("glm-main", recorder)
    response = provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert response.finish_reason == "length"


def test_null_content_is_not_an_error() -> None:
    """GLM 在只输出思考内容时会把 content 留成 null。"""
    recorder = Recorder(_chat(None, message_extra={"reasoning_content": "想了一想"}))
    provider = _provider("glm-main", recorder)
    response = provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert response.content is None
    assert response.reasoning_content == "想了一想"


# --------------------------------------------------------------- 请求体形状


def test_glm47_body_disables_thinking_and_keeps_temperature() -> None:
    recorder = Recorder(_chat())
    provider = _provider("glm-main", recorder)
    provider.complete(LLMRequest(messages=[Message.user("hi")], temperature=0.0))

    body = recorder.bodies[0]
    assert body["model"] == "glm-4.7"
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.0
    assert "reasoning_effort" not in body


def test_glm5_body_carries_neither_thinking_nor_temperature() -> None:
    """GLM-5 系思考关不掉、温度取值受限：能力字段让这两个参数完全不出现。"""
    recorder = Recorder(_chat())
    provider = _provider("glm-5-main", recorder)
    provider.complete(LLMRequest(messages=[Message.user("hi")], temperature=0.0))

    body = recorder.bodies[0]
    assert body["model"] == "glm-5"
    assert "thinking" not in body
    assert "temperature" not in body


def test_json_mode_requested_when_schema_present() -> None:
    recorder = Recorder(_chat('{"summary": "x"}'))
    provider = _provider("glm-main", recorder)
    provider.complete(LLMRequest(messages=[Message.user("hi")], response_schema=IssueAnalysis))

    body = recorder.bodies[0]
    assert body["response_format"] == {"type": "json_object"}
    # json_object 只保证"是个 JSON"，不保证字段齐全：Schema 必须同时写进 prompt，
    # 否则本地 Pydantic 校验会收到一份结构合法、字段缺失的对象。
    schema_text = recorder.bodies[0]["messages"][0]["content"]
    assert "summary" in schema_text


def test_schema_instruction_in_system_message_when_json_mode_unsupported() -> None:
    """不支持 JSON 模式的模型要靠 prompt + 本地校验，而不是静默少一层保障。"""
    spec = _spec("glm-main").model_copy(update={"supports_json_mode": False})
    messages = prepare_messages(
        LLMRequest(messages=[Message.user("hi")], response_schema=RepairPlan), spec
    )
    text = "\n".join(m.content or "" for m in messages)
    assert "root_cause_hypothesis" in text
    assert "只输出一个 JSON 对象" in text


def test_tools_are_sent_as_registry_schemas() -> None:
    recorder = Recorder(_chat())
    provider = _provider("glm-main", recorder)
    tool = ToolSpec(name="read_file", description="读文件", parameters={"type": "object"})
    provider.complete(LLMRequest(messages=[Message.user("hi")], tools=[tool]))

    body = recorder.bodies[0]
    assert body["tools"][0]["function"]["name"] == "read_file"
    assert body["tool_choice"] == "auto"


# --------------------------------------------------------------- 结构化输出

REAL_SCHEMAS: list[tuple[type[Any], dict[str, Any]]] = [
    (IssueAnalysis, {"summary": "登录时未校验过期时间"}),
    (RepairPlan, {"root_cause_hypothesis": "缺少边界检查", "alternatives": []}),
    (Diagnosis, {"root_cause": "补丁改错了返回类型"}),
    (ReviewResult, {"approved": True, "summary": "改动局限在一个函数"}),
]


@pytest.mark.parametrize(
    ("schema", "payload"), REAL_SCHEMAS, ids=lambda v: getattr(v, "__name__", str(v))
)
def test_agent_schemas_parse_from_glm_json(schema: Any, payload: dict[str, Any]) -> None:
    """四份真实 Agent Schema 都能从 GLM 形状的 JSON 里解析出来。

    普通文本能回不等于结构化输出可用——docs/llm-config.md 把这句话写进了文档，
    这条测试就是它对应的证据。
    """
    recorder = Recorder(_chat(json.dumps(payload, ensure_ascii=False)))
    provider = _provider("glm-main", recorder)
    response = complete_structured(
        provider, LLMRequest(messages=[Message.user("给结论")], response_schema=schema)
    )
    assert isinstance(response.parsed, schema)
    assert recorder.count == 1


def test_structured_fences_and_prose_are_tolerated() -> None:
    recorder = Recorder(_chat('好的，这是结果：\n```json\n{"summary": "x"}\n```\n'))
    provider = _provider("glm-main", recorder)
    response = complete_structured(
        provider, LLMRequest(messages=[Message.user("给结论")], response_schema=IssueAnalysis)
    )
    assert response.parsed is not None


def test_invalid_json_retries_with_the_previous_output_and_error() -> None:
    recorder = Recorder(_chat("这不是 JSON"), _chat('{"summary": "第二次对了"}'))
    provider = _provider("glm-main", recorder)
    response = complete_structured(
        provider, LLMRequest(messages=[Message.user("给结论")], response_schema=IssueAnalysis)
    )

    assert response.parsed is not None
    assert recorder.count == 2
    second = json.dumps(recorder.bodies[1], ensure_ascii=False)
    assert "不符合要求的 JSON Schema" in second
    assert "这不是 JSON" in second


def test_enum_and_type_violations_fail_explicitly() -> None:
    """缺字段/枚举错/类型错必须明确失败，不能拼一个"看起来对"的对象出来。"""
    recorder = Recorder(
        _chat('{"approved": "yes"}'),
        _chat('{"approved": 1}'),
        _chat('{"summary": 3}'),
    )
    provider = _provider("glm-main", recorder)
    with pytest.raises(StructuredOutputError) as info:
        complete_structured(
            provider, LLMRequest(messages=[Message.user("复审")], response_schema=ReviewResult)
        )
    assert info.value.attempts == 3
    assert recorder.count == 3
    assert info.value.last_content is not None


def test_structured_attempts_are_charged() -> None:
    """重试的三次调用都产生 token：费用必须是三次之和，而不是只算成功那次。"""
    recorder = Recorder(
        _chat("nope", prompt_tokens=1000, completion_tokens=10),
        _chat("nope", prompt_tokens=1000, completion_tokens=10),
        _chat('{"summary": "ok"}', prompt_tokens=1000, completion_tokens=10),
    )
    provider = _provider("glm-main", recorder)
    response = complete_structured(
        provider, LLMRequest(messages=[Message.user("给结论")], response_schema=IssueAnalysis)
    )
    assert response.input_tokens == 1000  # 单次调用的用量
    assert recorder.count == 3


# --------------------------------------------------------------- 函数调用


def test_single_tool_call_with_null_content() -> None:
    recorder = Recorder(_tool_calls([{"name": "read_file", "arguments": '{"path": "a.py"}'}]))
    provider = _provider("glm-main", recorder)
    response = provider.complete(
        LLMRequest(messages=[Message.user("读文件")], tools=[ToolSpec(name="read_file")])
    )

    assert response.content is None
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert (call.name, call.arguments, call.id) == ("read_file", {"path": "a.py"}, "call_0")
    assert call.is_valid


def test_multiple_tool_calls_in_one_response() -> None:
    recorder = Recorder(
        _tool_calls(
            [
                {"name": "read_file", "arguments": '{"path": "a.py"}', "id": "call_a"},
                {"name": "search_code", "arguments": '{"query": "token"}', "id": "call_b"},
            ]
        )
    )
    provider = _provider("glm-main", recorder)
    response = provider.complete(
        LLMRequest(
            messages=[Message.user("看两处")],
            tools=[ToolSpec(name="read_file"), ToolSpec(name="search_code")],
        )
    )
    assert [c.name for c in response.tool_calls] == ["read_file", "search_code"]
    assert [c.id for c in response.tool_calls] == ["call_a", "call_b"]


def test_malformed_arguments_become_a_validation_error_not_a_crash() -> None:
    recorder = Recorder(_tool_calls([{"name": "read_file", "arguments": '{"path": '}]))
    provider = _provider("glm-main", recorder)
    response = provider.complete(
        LLMRequest(messages=[Message.user("读")], tools=[ToolSpec(name="read_file")])
    )
    call = response.tool_calls[0]
    assert not call.is_valid
    assert call.arguments == {}


def test_reasoning_content_is_not_fed_back_into_the_next_request() -> None:
    """思考内容只记录，不回流：下一轮**实际发出去的请求体**里不该出现它。

    多数服务商拒绝接受带思考内容的历史消息；而且把内部推理原样回传，等于把一段
    没人审阅过的文本重新当作输入喂给模型。
    """
    secret_reasoning = "这段推理不应该再发出去"
    recorder = Recorder(
        _chat(
            None,
            finish_reason="tool_calls",
            message_extra={
                "reasoning_content": secret_reasoning,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            },
        ),
        _chat("结论"),
    )
    provider = _provider("glm-main", recorder)
    specs = [ToolSpec(name="read_file")]
    first = provider.complete(
        LLMRequest(messages=[Message.user("读")], tools=specs, purpose="coder")
    )
    assert first.reasoning_content == secret_reasoning

    history = [
        Message.user("读"),
        Message(role="assistant", content=first.content, tool_calls=first.tool_calls),
        Message.tool_result(first.tool_calls[0].id, "文件内容"),
    ]
    provider.complete(LLMRequest(messages=history, tools=specs, purpose="coder"))

    second_body = json.dumps(recorder.bodies[1], ensure_ascii=False)
    assert secret_reasoning not in second_body
    assert "call_1" in second_body  # 工具结果仍然靠 tool_call_id 关联


# ----------------------------------------------------- 完整工具闭环（真实 registry）


def _registry(tmp_path: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(read_file_tool(WorkspacePathPolicy(tmp_path)))
    return registry


def test_full_tool_loop_executes_and_feeds_result_back(tmp_path: Path) -> None:
    """模型请求 → ToolCall → CodePilot 校验并执行 → 结果回传 → 模型继续输出。

    用真实 :class:`ToolRegistry` 与真实 workspace 路径策略，不用假工具：
    这样"权限隔离 + Pydantic 校验 + tool_call_id 关联"三件事同时被验证。
    """
    (tmp_path / "notes.txt").write_text("第一行\nMARKER=7QX4\n", encoding="utf-8")
    registry = _registry(tmp_path)

    recorder = Recorder(
        _tool_calls([{"name": "read_file", "arguments": '{"path": "notes.txt"}'}]),
        _chat("暗号是 7QX4"),
    )
    provider = _provider("glm-main", recorder)
    coder = CodingAgent(provider, registry, max_tool_calls=30)
    result = coder.run(
        issue_text="读出暗号",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=RepairPlan(root_cause_hypothesis="x", alternatives=[]),
    )

    assert result.stop_reason == "model_completed"
    assert [audit.name for audit in result.tool_calls] == ["read_file"]
    assert result.tool_calls[0].ok is True

    # 第二轮请求体里必须带上工具结果，并且用 tool_call_id 指回那次调用。
    second = recorder.bodies[1]["messages"]
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "call_0"
    assert "MARKER=7QX4" in second[-1]["content"]
    assistant = second[-2]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path": "notes.txt"}'


def test_unregistered_tool_is_refused_and_reported_to_the_model(tmp_path: Path) -> None:
    recorder = Recorder(
        _tool_calls([{"name": "delete_repo", "arguments": "{}"}]),
        _chat("好的，我换一个工具"),
    )
    provider = _provider("glm-main", recorder)
    coder = CodingAgent(provider, _registry(tmp_path), max_tool_calls=30)
    result = coder.run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=RepairPlan(root_cause_hypothesis="x", alternatives=[]),
    )

    audit = result.tool_calls[0]
    assert audit.ok is False
    assert audit.error_code == "unregistered_tool"
    assert "delete_repo" in json.dumps(recorder.bodies[1], ensure_ascii=False)


def test_path_escape_in_arguments_is_blocked(tmp_path: Path) -> None:
    """模型伪造的绝对路径不能越出 workspace —— 由真实路径策略拒绝。"""
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("机密", encoding="utf-8")
    recorder = Recorder(
        _tool_calls([{"name": "read_file", "arguments": json.dumps({"path": str(outside)})}]),
        _chat("放弃"),
    )
    provider = _provider("glm-main", recorder)
    coder = CodingAgent(provider, _registry(tmp_path))
    result = coder.run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=RepairPlan(root_cause_hypothesis="x", alternatives=[]),
    )
    assert result.tool_calls[0].ok is False
    assert "机密" not in json.dumps(recorder.bodies[1], ensure_ascii=False)


def test_schema_invalid_arguments_are_rejected_by_pydantic(tmp_path: Path) -> None:
    """arguments 必须过 JSON/Pydantic 校验：类型不对就不进 handler。"""
    recorder = Recorder(
        _tool_calls([{"name": "read_file", "arguments": '{"path": 123, "start_line": "x"}'}]),
        _chat("重来"),
    )
    provider = _provider("glm-main", recorder)
    coder = CodingAgent(provider, _registry(tmp_path))
    result = coder.run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=RepairPlan(root_cause_hypothesis="x", alternatives=[]),
    )
    assert result.tool_calls[0].ok is False


def test_tool_call_limit_still_applies(tmp_path: Path) -> None:
    """GLM 不能绕过 Coding Agent 原有的 30 次工具调用上限。"""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    recorder = Recorder(
        *[_tool_calls([{"name": "read_file", "arguments": '{"path": "a.py"}'}]) for _ in range(31)]
    )
    provider = _provider("glm-main", recorder)
    coder = CodingAgent(provider, _registry(tmp_path), max_tool_calls=30)
    result = coder.run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=RepairPlan(root_cause_hypothesis="x", alternatives=[]),
    )
    assert result.stop_reason == "tool_limit"
    assert len(result.tool_calls) == 30


# --------------------------------------------------------------- 错误映射


@pytest.mark.parametrize("status", [401, 403, 404, 400])
def test_client_errors_are_not_retried(status: int) -> None:
    """认证失败/无权限/模型不存在/参数错：重发只会多三次失败和三次配额消耗。"""
    recorder = Recorder(*[httpx.Response(status, json={"code": str(status), "message": "no"})] * 3)
    provider = _provider("glm-main", recorder)
    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert info.value.status == status
    assert recorder.count == 1
    assert provider.attempts == 1


def test_429_honours_retry_after_and_stays_bounded() -> None:
    slept: list[float] = []
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": "3"}, json={"message": "rate limited"}),
        _chat("限流后成功"),
    )
    provider = _provider("glm-main", recorder, policy=RetryPolicy(max_attempts=2, jitter_ratio=0.0))
    provider.sleep = slept.append  # type: ignore[method-assign]
    response = provider.complete(LLMRequest(messages=[Message.user("hi")]))

    assert response.content == "限流后成功"
    assert slept == [3.0]
    assert recorder.count == 2


def test_glm_429_without_retry_after_uses_backoff() -> None:
    """官方限流文档不提供 Retry-After 头，因此退避必须由本地策略给出。"""
    recorder = Recorder(httpx.Response(429, json={"message": "1302 rpm limit"}), _chat("ok"))
    provider = _provider("glm-main", recorder)
    assert provider.complete(LLMRequest(messages=[Message.user("hi")])).content == "ok"
    assert recorder.count == 2


def test_5xx_exhausts_and_reports_last_status() -> None:
    recorder = Recorder(
        httpx.Response(500, json={"message": "boom"}),
        httpx.Response(503, text="upstream"),
        httpx.Response(500, json={"message": "boom again"}),
    )
    provider = _provider("glm-main", recorder)
    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert recorder.count == 3
    assert provider.attempts == 3
    assert info.value.status == 500


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("r"), httpx.ConnectError("dns")]
)
def test_transport_failures_count_as_unacknowledged(exc: Exception) -> None:
    """已发出但结果未知的请求必须留下痕迹：服务端可能已经生成并计费。"""
    recorder = Recorder(exc, exc, _chat("终于回来"))
    provider = _provider("glm-main", recorder)
    response = provider.complete(LLMRequest(messages=[Message.user("hi")]))

    assert response.content == "终于回来"
    assert provider.unacknowledged == 2
    assert provider.attempts == 3


def test_context_overflow_maps_to_its_own_error() -> None:
    recorder = Recorder(
        httpx.Response(400, json={"message": "the model maximum context length is 200000 tokens"})
    )
    provider = _provider("glm-main", recorder)
    with pytest.raises(ContextTooLongError):
        provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert recorder.count == 1


def test_200_with_non_json_body_is_a_provider_error() -> None:
    """网关截断或 HTML 错误页：不能让 JSONDecodeError 穿透成"无法归因的内部错误"。"""
    recorder = Recorder(httpx.Response(200, text="<html>502 Bad Gateway</html>"))
    provider = _provider("glm-main", recorder)
    with pytest.raises(ProviderError, match="不是合法 JSON"):
        provider.complete(LLMRequest(messages=[Message.user("hi")]))


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"message": "not-a-dict"}]},
        {"choices": [{"message": {"role": "assistant", "content": {"text": "x"}}}]},
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": "oops"}}]},
    ],
    ids=["empty-choices", "message-not-object", "content-not-string", "tool-calls-not-list"],
)
def test_malformed_success_bodies_fail_loudly(payload: dict[str, Any]) -> None:
    recorder = Recorder(httpx.Response(200, json=payload))
    provider = _provider("glm-main", recorder)
    with pytest.raises(ProviderError):
        provider.complete(LLMRequest(messages=[Message.user("hi")], tools=[ToolSpec(name="t")]))


def test_error_body_is_attached_and_redacted_before_printing() -> None:
    """错误体里可能回显请求头，因此它必须带出来、但打印前一定要过脱敏。"""
    body = {"error": {"message": "bad", "request_headers": {"Authorization": f"Bearer {KEY}"}}}
    recorder = Recorder(httpx.Response(400, json=body))
    provider = _provider("glm-main", recorder)
    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=[Message.user("hi")]))

    assert info.value.body is not None
    assert KEY not in str(redact(info.value.body))
    assert "***REDACTED***" in json.dumps(redact(info.value.body), ensure_ascii=False)
    assert KEY not in str(info.value)


# --------------------------------------------------------------- 用量与费用


def test_cost_uses_configured_cny_price_and_cached_discount() -> None:
    """glm-4.7 取公开最高档 4/16、缓存命中 0.8（每百万 token，CNY）。

    缓存命中的那部分必须按**替换价**算：它不是额外加价，而是那批 token 的价。
    """
    recorder = Recorder(
        _chat(
            prompt_tokens=1_000_000,
            completion_tokens=1_000_000,
            cached_tokens=200_000,
        )
    )
    response = _provider("glm-main", recorder).complete(LLMRequest(messages=[Message.user("hi")]))

    expected = (800_000 * 4.0 + 200_000 * 0.8 + 1_000_000 * 16.0) / 1e6
    assert response.cost == pytest.approx(expected)
    assert response.cached_input_tokens == 200_000
    assert response.cost_known is True


def test_zai_price_is_usd_and_never_silently_read_as_cny() -> None:
    """Z.AI 以 USD 计价：不声明汇率就拒绝换算，而不是按 1.0 混进 CNY 预算。"""
    config = _glm_config()
    price = config.models["glm-5-zai"].price
    assert price is not None and price.currency == "USD"

    recorder = Recorder(_chat(prompt_tokens=1_000_000, completion_tokens=1_000_000))
    # 未声明 USD 汇率时 compute_cost 会按 1.0 处理，因此这条换算必须由闸门拦住，
    # 而不是靠调用方记得检查——见 glm_readiness / ConfigReport.currency_unmatched。
    provider = _provider(
        "glm-5-zai",
        recorder,
        base_url="https://api.z.ai/api/paas/v4",
        exchange_rates={"CNY": 1.0},
        budget_currency="CNY",
    )
    response = provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert response.cost == pytest.approx(4.2)  # (1.0 + 3.2) USD，按 1.0 汇率的裸数值

    gated = config.bind_all_roles("glm-5-zai").check()
    assert gated.currency_unmatched == {"glm-5-zai": "USD"}
    assert gated.ready is False, "缺汇率的跨币种必须由配置体检拦住，而不是靠人记得换算"


def test_declared_exchange_rate_converts_into_budget_currency() -> None:
    recorder = Recorder(_chat(prompt_tokens=1_000_000, completion_tokens=1_000_000))
    provider = _provider(
        "glm-5-zai",
        recorder,
        base_url="https://api.z.ai/api/paas/v4",
        exchange_rates={"CNY": 1.0, "USD": 7.0},
        budget_currency="CNY",
    )
    response = provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert response.cost == pytest.approx(4.2 * 7.0)


def test_unpriced_model_reports_cost_as_unknown() -> None:
    """没配单价时费用记 UNKNOWN，不能记成"这次免费"——两者在报表里长得一模一样。"""
    spec = _spec("glm-main").model_copy(update={"price": None})
    recorder = Recorder(_chat())
    client = httpx.Client(
        transport=httpx.MockTransport(recorder.handler),
        base_url="https://open.bigmodel.cn/api/paas/v4",
    )
    provider = OpenAICompatibleProvider(
        spec=spec,
        provider=_glm_config().providers["bigmodel"],
        api_key=KEY,
        client=client,
        sleep=lambda _s: None,
    )
    response = provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert response.cost == 0.0
    assert response.cost_known is False


def test_usage_is_attributed_to_the_model_alias() -> None:
    from codepilot.llm.tracking import UsageTrackingProvider
    from codepilot.schemas.usage import Usage

    recorder = Recorder(_chat(prompt_tokens=100, completion_tokens=7))
    usage = Usage()
    chain = UsageTrackingProvider(_provider("glm-main", recorder), usage=usage)
    chain.complete(LLMRequest(messages=[Message.user("hi")], purpose="plan"))

    assert usage.total_tokens == 107
    assert usage.llm_calls == 1
    assert usage.cost > 0
    # 归属必须落在**模型别名**上，而不是 purpose 字符串上：换模型时"贵在哪个环节"
    # 这个问题仍然要能回答。
    assert usage.by_model["glm-main"].input_tokens == 100
    assert usage.by_model["glm-main"].llm_calls == 1


# --------------------------------------------------------------- 装配与凭证


def test_missing_key_refuses_before_any_http_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有有效密钥时立即报配置错误，一次外网请求都不发。"""
    import codepilot.config as app_config

    monkeypatch.setattr(app_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv(GLM_API_KEY_ENV, raising=False)
    monkeypatch.delenv(ZHIPU_API_KEY_ENV, raising=False)
    app_config._dotenv_values.cache_clear()

    from codepilot.llm.registry import LLMMode, LLMRegistry

    recorder = Recorder(_chat())
    registry = LLMRegistry(
        # 仓库默认角色仍指向 deepseek，所以这里必须显式把角色绑到 GLM 别名上，
        # 否则这条测试报的是 deepseek 缺密钥，而 GLM 的装配路径根本没被走到。
        _glm_config().bind_all_roles("glm-main"),
        mode=LLMMode.LIVE,
        client_factory=lambda spec, key: httpx.Client(
            transport=httpx.MockTransport(recorder.handler), base_url=spec.base_url
        ),
    )
    with pytest.raises(ProviderNotConfiguredError, match=GLM_API_KEY_ENV):
        registry.for_role("coder")
    assert recorder.count == 0


def test_key_never_appears_in_provider_repr_or_response_fields() -> None:
    recorder = Recorder(_chat())
    provider = _provider("glm-main", recorder)
    assert KEY not in repr(provider)
    assert KEY not in str(provider.spec)
    response = provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert KEY not in json.dumps(response.model_dump(mode="json"), ensure_ascii=False)


def test_model_switch_changes_request_body_and_alias() -> None:
    """模型标识由配置决定：同一份代码换个别名就该换个 model 字段。"""
    for alias, expected in (("glm-main", "glm-4.7"), ("glm-5-main", "glm-5")):
        recorder = Recorder(_chat())
        provider = _provider(alias, recorder)
        provider.complete(LLMRequest(messages=[Message.user("hi")]))
        assert recorder.bodies[0]["model"] == expected
        assert provider.spec.model == expected


def test_tool_call_with_arguments_error_is_marked_invalid() -> None:
    """ToolCall 带 arguments_error 时 ``is_valid`` 为 False，上层据此走重试路径。"""
    call = ToolCall(id="c", name="read_file", arguments={}, arguments_error="不是对象")
    assert not call.is_valid


# --------------------------------------------------------------- 凭证回显


def test_key_echoed_inside_error_message_is_scrubbed_at_the_source() -> None:
    """服务商的 401 错误体常常把密钥原样回显在 message 字符串里。

    :func:`redact` 按**键名**脱敏，对"密钥作为某个值的一部分"完全无能为力 ——
    所以这一步必须由持有密钥的 provider 来做，否则一次排错截图就把密钥带进了
    artifacts、Issue 或聊天记录。
    """
    body = {"code": "1000", "message": f"authentication failed, key={KEY}"}
    recorder = Recorder(httpx.Response(401, json=body))
    provider = _provider("glm-main", recorder)

    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=[Message.user("hi")]))

    dumped = json.dumps(info.value.body, ensure_ascii=False)
    assert KEY not in dumped
    assert "authentication failed" in dumped, "错误信息本身要留着，只抹掉密钥"


def test_key_never_reaches_the_exception_of_a_transport_failure() -> None:
    """连接失败时 httpx 会把请求对象带进异常文本；base_url 带凭据也不能漏。"""
    # ConnectError 属于可重试错误，所以脚本要供满 RetryPolicy 的三次尝试，
    # 否则先撞上"响应脚本已用尽"而测不到真正想测的那条路径。
    recorder = Recorder(*[httpx.ConnectError(f"cannot connect with {KEY}")] * 3)
    provider = _provider("glm-main", recorder)
    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert recorder.count == 3
    assert KEY not in str(info.value)
    assert KEY not in str(info.value.body or "")


def test_non_json_error_page_is_kept_but_scrubbed() -> None:
    recorder = Recorder(httpx.Response(400, text=f"<html>invalid request {KEY}</html>"))
    provider = _provider("glm-main", recorder)
    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert KEY not in str(info.value.body)
    assert "invalid request" in str(info.value.body)
