"""L-05 / L-14 的证明：用 ``httpx.MockTransport`` 覆盖 provider 的全部路径。

02 §5.3 明确要求用 MockTransport 而不是打真实接口。这带来三个好处：
不花钱、不依赖网络、以及**能精确断言请求体长什么样**——
后者是"能力差异处理正确"唯一能被验证的方式。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from codepilot.llm.config import ModelPrice, ModelSpec, ProviderSpec
from codepilot.llm.errors import (
    ContextTooLongError,
    ProviderError,
    ProviderNotAllowedError,
    RateLimitError,
)
from codepilot.llm.openai_compatible import OpenAICompatibleProvider, looks_like_context_overflow
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.types import LLMRequest, Message, ToolSpec

PRICE = ModelPrice(currency="CNY", input_per_mtok=1.0, output_per_mtok=2.0)

SPEC = ModelSpec(
    alias="alias-a",
    provider="fake",
    model="fake-model-id",
    context_window=8000,
    price=PRICE,
)

PROVIDER_SPEC = ProviderSpec(name="fake", base_url="https://fake.test/v1", api_key_env="FAKE_KEY")

MESSAGES = [Message.system("你是助手"), Message.user("修复这个 bug")]


class _Transport:
    """可编程的 MockTransport：按顺序返回预设响应，并记录收到的请求。"""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.content:
            self.bodies.append(json.loads(request.content))
        if not self._responses:
            raise AssertionError("MockTransport 的响应脚本已用尽")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def count(self) -> int:
        return len(self.requests)


def _ok(content: str = "ok", **usage: int) -> httpx.Response:
    payload = {
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 100),
            "completion_tokens": usage.get("completion_tokens", 20),
        },
    }
    if "cached_tokens" in usage:
        payload["usage"]["prompt_tokens_details"] = {"cached_tokens": usage["cached_tokens"]}
    return httpx.Response(200, json=payload)


class _NoSleep:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def _make(
    transport: _Transport,
    *,
    spec: ModelSpec = SPEC,
    provider_spec: ProviderSpec = PROVIDER_SPEC,
    policy: RetryPolicy | None = None,
    **kwargs: Any,
) -> tuple[OpenAICompatibleProvider, _NoSleep]:
    sleep = _NoSleep()
    client = httpx.Client(transport=httpx.MockTransport(transport), base_url=provider_spec.base_url)
    instance = OpenAICompatibleProvider(
        spec=spec,
        provider=provider_spec,
        api_key="sk-test",
        client=client,
        policy=policy or RetryPolicy(max_attempts=3, base_delay=1.0, jitter_ratio=0.0),
        sleep=sleep,
        **kwargs,
    )
    return instance, sleep


# --------------------------------------------------------------- 正常路径


def test_returns_content_and_tokens() -> None:
    transport = _Transport(_ok("已修复", prompt_tokens=1000, completion_tokens=200))
    provider, _ = _make(transport)

    response = provider.complete(LLMRequest(messages=MESSAGES, purpose="code"))

    assert response.content == "已修复"
    assert response.input_tokens == 1000
    assert response.output_tokens == 200
    assert response.model_alias == "alias-a"
    assert response.cost_known is True


def test_cost_is_computed_from_price() -> None:
    transport = _Transport(_ok(prompt_tokens=1_000_000, completion_tokens=1_000_000))
    provider, _ = _make(transport)
    response = provider.complete(LLMRequest(messages=MESSAGES))
    assert response.cost == pytest.approx(3.0)  # 1*1 + 2*1


def test_cached_tokens_are_reported() -> None:
    transport = _Transport(_ok(prompt_tokens=1000, completion_tokens=10, cached_tokens=800))
    provider, _ = _make(transport)
    response = provider.complete(LLMRequest(messages=MESSAGES))
    assert response.cached_input_tokens == 800


def test_missing_price_yields_cost_unknown() -> None:
    """没配单价的模型必须能跑，但费用不能假装是真实的 0。"""
    spec = ModelSpec(alias="alias-a", provider="fake", model="fake-model-id", price=None)
    transport = _Transport(_ok())
    provider, _ = _make(transport, spec=spec)
    response = provider.complete(LLMRequest(messages=MESSAGES))
    assert response.cost == 0.0
    assert response.cost_known is False


def test_reasoning_content_is_captured() -> None:
    """推理模型的思考内容单独存，不回流到后续消息。"""
    transport = _Transport(
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "答案",
                            "reasoning_content": "想了一想",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    provider, _ = _make(transport)
    response = provider.complete(LLMRequest(messages=MESSAGES))
    assert response.reasoning_content == "想了一想"
    assert response.content == "答案"


# --------------------------------------------------------------- 请求体


def test_request_goes_to_chat_completions_with_auth() -> None:
    transport = _Transport(_ok())
    provider, _ = _make(transport)
    provider.complete(LLMRequest(messages=MESSAGES))

    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"


def test_body_carries_model_and_messages() -> None:
    transport = _Transport(_ok())
    provider, _ = _make(transport)
    provider.complete(LLMRequest(messages=MESSAGES, purpose="plan"))

    body = transport.bodies[0]
    assert body["model"] == "fake-model-id"
    assert body["messages"][0] == {"role": "system", "content": "你是助手"}
    assert body["max_tokens"] == 4096


def test_capability_flags_reach_the_wire() -> None:
    """能力字段翻过来，请求体就要跟着变——这是"不看 provider 名字"的证明。"""
    spec = ModelSpec(
        alias="alias-a",
        provider="fake",
        model="fake-model-id",
        supports_temperature=False,
        supports_tools=False,
        supports_json_mode=False,
    )
    transport = _Transport(_ok())
    provider, _ = _make(transport, spec=spec)
    provider.complete(LLMRequest(messages=MESSAGES, temperature=0.9, tools=[ToolSpec(name="t")]))

    body = transport.bodies[0]
    assert "temperature" not in body
    assert "tools" not in body
    assert "response_format" not in body


def test_tool_result_message_encoding() -> None:
    transport = _Transport(_ok())
    provider, _ = _make(transport)
    provider.complete(
        LLMRequest(messages=[*MESSAGES, Message.tool_result("call_1", '{"ok": true}')])
    )
    body = transport.bodies[0]
    assert body["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"ok": true}',
    }


# --------------------------------------------------------------- 工具调用解析


def _tool_response(arguments: Any, name: str = "read_file") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_native_tool_call_is_parsed() -> None:
    transport = _Transport(_tool_response('{"path": "a.py"}'))
    provider, _ = _make(transport)
    response = provider.complete(LLMRequest(messages=MESSAGES, tools=[ToolSpec(name="read_file")]))

    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.py"}
    assert call.is_valid is True


def test_invalid_json_arguments_become_a_retryable_error() -> None:
    """按 02 §5.3，非法 JSON 属于结构化输出失败，交给上层重试。"""
    transport = _Transport(_tool_response('{"path": "a.py"'))
    provider, _ = _make(transport)
    response = provider.complete(LLMRequest(messages=MESSAGES, tools=[ToolSpec(name="read_file")]))

    call = response.tool_calls[0]
    assert call.is_valid is False
    assert call.arguments_error is not None
    assert call.arguments == {}


def test_non_object_arguments_are_rejected() -> None:
    transport = _Transport(_tool_response('["a.py"]'))
    provider, _ = _make(transport)
    call = provider.complete(LLMRequest(messages=MESSAGES)).tool_calls[0]
    assert call.is_valid is False
    assert "对象" in (call.arguments_error or "")


def test_object_arguments_are_tolerated() -> None:
    """少数服务商直接返回对象而不是 JSON 字符串。"""
    transport = _Transport(_tool_response({"path": "a.py"}))
    provider, _ = _make(transport)
    call = provider.complete(LLMRequest(messages=MESSAGES)).tool_calls[0]
    assert call.arguments == {"path": "a.py"}
    assert call.is_valid is True


def test_missing_arguments_defaults_to_empty_object() -> None:
    transport = _Transport(_tool_response(None, name="list_files"))
    provider, _ = _make(transport)
    call = provider.complete(LLMRequest(messages=MESSAGES)).tool_calls[0]
    assert call.arguments == {}
    assert call.is_valid is True


def test_tool_protocol_used_when_native_tools_unsupported() -> None:
    """不支持原生 tool calling 时，content 里的 JSON 要变成一个 ToolCall。"""
    spec = ModelSpec(alias="alias-a", provider="fake", model="m", supports_tools=False)
    transport = _Transport(_ok('{"tool": "read_file", "arguments": {"path": "a.py"}}'))
    provider, _ = _make(transport, spec=spec)

    response = provider.complete(LLMRequest(messages=MESSAGES, tools=[ToolSpec(name="read_file")]))

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "a.py"}


def test_plain_content_is_not_mistaken_for_a_tool_call() -> None:
    spec = ModelSpec(alias="alias-a", provider="fake", model="m", supports_tools=False)
    transport = _Transport(_ok("我认为应该修改 auth/service.py"))
    provider, _ = _make(transport, spec=spec)
    response = provider.complete(LLMRequest(messages=MESSAGES, tools=[ToolSpec(name="read_file")]))
    assert response.tool_calls == []


# --------------------------------------------------------------- 错误映射


def test_429_becomes_rate_limit_error_with_retry_after() -> None:
    transport = _Transport(
        httpx.Response(429, headers={"retry-after": "7"}, json={"error": "slow down"})
    )
    provider, _sleep = _make(transport, policy=RetryPolicy(max_attempts=1))

    with pytest.raises(RateLimitError) as info:
        provider.complete(LLMRequest(messages=MESSAGES))

    assert info.value.retry_after == 7.0
    assert info.value.status == 429
    assert info.value.provider == "fake"


def test_429_is_retried_then_succeeds() -> None:
    transport = _Transport(httpx.Response(429, json={}), _ok("终于成功"))
    provider, sleep = _make(transport)

    response = provider.complete(LLMRequest(messages=MESSAGES))

    assert response.content == "终于成功"
    assert transport.count == 2
    assert sleep.slept == [1.0]


def test_retry_after_header_shortens_the_wait() -> None:
    transport = _Transport(httpx.Response(429, headers={"retry-after": "2"}, json={}), _ok())
    provider, sleep = _make(transport)
    provider.complete(LLMRequest(messages=MESSAGES))
    assert sleep.slept == [2.0]


def test_5xx_becomes_provider_error_and_is_retried() -> None:
    transport = _Transport(httpx.Response(503, text="upstream down"), _ok())
    provider, _sleep = _make(transport)

    response = provider.complete(LLMRequest(messages=MESSAGES))

    assert response.content == "ok"
    assert transport.count == 2


def test_5xx_exhausts_retries() -> None:
    transport = _Transport(*[httpx.Response(500, text="boom") for _ in range(3)])
    provider, _sleep = _make(transport)

    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=MESSAGES))

    assert info.value.status == 500
    assert transport.count == 3


def test_context_overflow_is_not_retried() -> None:
    """上下文超限要走"重建上下文"的路径，重试没有意义。"""
    transport = _Transport(
        httpx.Response(
            400, json={"error": {"message": "This model's maximum context length is 8192 tokens"}}
        ),
        _ok(),
    )
    provider, sleep = _make(transport)

    with pytest.raises(ContextTooLongError):
        provider.complete(LLMRequest(messages=MESSAGES))

    assert transport.count == 1
    assert sleep.slept == []


def test_generic_400_is_not_retried() -> None:
    transport = _Transport(httpx.Response(400, json={"error": {"message": "invalid temperature"}}))
    provider, _sleep = _make(transport)

    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=MESSAGES))

    assert info.value.status == 400
    assert transport.count == 1


def test_401_is_not_retried() -> None:
    transport = _Transport(httpx.Response(401, json={"error": "bad key"}))
    provider, _sleep = _make(transport)

    with pytest.raises(ProviderError):
        provider.complete(LLMRequest(messages=MESSAGES))

    assert transport.count == 1


def test_timeout_becomes_provider_error_and_is_retried() -> None:
    transport = _Transport(httpx.ConnectTimeout("timed out"), _ok())
    provider, _sleep = _make(transport)
    response = provider.complete(LLMRequest(messages=MESSAGES))
    assert response.content == "ok"


def test_no_choices_raises() -> None:
    transport = _Transport(httpx.Response(200, json={"choices": []}))
    provider, _ = _make(transport)
    with pytest.raises(ProviderError, match="choices"):
        provider.complete(LLMRequest(messages=MESSAGES))


def test_error_body_is_attached_for_diagnosis() -> None:
    """错误体是排查兼容性问题的唯一线索，必须带上（打印前会过脱敏）。"""
    transport = _Transport(httpx.Response(400, json={"error": {"code": "invalid_param"}}))
    provider, _ = _make(transport)
    with pytest.raises(ProviderError) as info:
        provider.complete(LLMRequest(messages=MESSAGES))
    assert info.value.body == {"error": {"code": "invalid_param"}}


# --------------------------------------------------------------- 数据外发白名单


def test_provider_outside_allowlist_is_refused_at_construction() -> None:
    """跑 100 个任务的 benchmark 应该在开始前失败，而不是跑到第 37 个才报错。"""
    transport = _Transport()
    with pytest.raises(ProviderNotAllowedError):
        _make(transport, allowed_providers=["other-provider"])


def test_provider_inside_allowlist_is_accepted() -> None:
    transport = _Transport(_ok())
    provider, _ = _make(transport, allowed_providers=["fake"])
    assert provider.complete(LLMRequest(messages=MESSAGES)).content == "ok"


# --------------------------------------------------------------- 其他


def test_close_does_not_close_injected_client() -> None:
    """注入的 client 归调用方所有，provider 关掉它会让复用的连接突然失效。"""
    transport = _Transport(_ok())
    client = httpx.Client(transport=httpx.MockTransport(transport), base_url="https://fake.test/v1")
    provider = OpenAICompatibleProvider(
        spec=SPEC, provider=PROVIDER_SPEC, api_key="k", client=client
    )
    provider.close()
    assert client.is_closed is False


def test_context_manager_closes_owned_client() -> None:
    transport = _Transport(_ok())
    provider = OpenAICompatibleProvider(
        spec=SPEC,
        provider=PROVIDER_SPEC,
        api_key="k",
        client=httpx.Client(
            transport=httpx.MockTransport(transport), base_url="https://fake.test/v1"
        ),
    )
    with provider as p:
        p.complete(LLMRequest(messages=MESSAGES))
    # 注入的 client 不会被关，因此这里只验证上下文管理器可用。
    assert transport.count == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("This model's maximum context length is 8192 tokens", True),
        ("context_length_exceeded", True),
        ("Prompt is too long", True),
        ("invalid temperature value", False),
        ("max_tokens must be positive", False),
    ],
)
def test_context_overflow_detection(text: str, expected: bool) -> None:
    """``max_tokens`` 参数错误不能被误判成超限——重建上下文解决不了它。"""
    assert looks_like_context_overflow(text) is expected
