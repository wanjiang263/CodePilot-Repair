"""Native Anthropic and Bedrock protocol adapters (offline, deterministic)."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import pytest

from codepilot.llm.anthropic_messages import AnthropicMessagesProvider
from codepilot.llm.bedrock_converse import BedrockConverseProvider
from codepilot.llm.config import ModelPrice, ModelSpec, ProviderSpec
from codepilot.llm.errors import ProviderError, RateLimitError
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.types import LLMRequest, Message, ToolSpec

_KEY = "sk-ant-secret-value"


def _model(price: ModelPrice | None = None) -> ModelSpec:
    return ModelSpec(
        alias="claude-test",
        provider="anthropic-test",
        model="claude-test-1",
        context_window=200_000,
        max_output_tokens=1024,
        price=price or ModelPrice(currency="CNY", input_per_mtok=10.0, output_per_mtok=30.0),
    )


def _provider(protocol: str = "anthropic_messages") -> ProviderSpec:
    return ProviderSpec(
        name="anthropic-test",
        base_url="https://api.anthropic.test",
        protocol=protocol,
        timeout_seconds=5.0,
    )


def test_anthropic_messages_request_and_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello from Claude"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 2,
                },
            },
        )

    client = httpx.Client(
        base_url="https://api.anthropic.test",
        transport=httpx.MockTransport(handler),
    )
    provider = AnthropicMessagesProvider(
        spec=_model(),
        provider=_provider(),
        api_key=_KEY,
        client=client,
        policy=RetryPolicy(max_attempts=1),
    )
    response = provider.complete(
        LLMRequest(
            messages=[Message.system("Be terse."), Message.user("Hi")],
            max_output_tokens=32,
            purpose="unit_test",
        )
    )
    assert response.content == "Hello from Claude"
    assert response.finish_reason == "end_turn"
    assert response.input_tokens == 14
    assert response.cached_input_tokens == 2
    assert response.cost_known is True
    assert response.cost > 0
    request = seen[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == _KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["x-codepilot-purpose"] == "unit_test"
    body = request.read().decode("utf-8")
    assert '"system":"Be terse."' in body
    assert '"max_tokens":32' in body


def test_anthropic_tool_calls_are_native() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "I will read it."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "src/app.py"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        )

    client = httpx.Client(
        base_url="https://api.anthropic.test",
        transport=httpx.MockTransport(handler),
    )
    provider = AnthropicMessagesProvider(
        spec=_model(),
        provider=_provider(),
        api_key=_KEY,
        client=client,
    )
    response = provider.complete(
        LLMRequest(
            messages=[Message.user("read")],
            tools=[
                ToolSpec(
                    name="read_file",
                    description="read a file",
                    parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                )
            ],
            max_output_tokens=64,
        )
    )
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "src/app.py"}
    assert response.content == "I will read it."


def test_anthropic_auth_error_never_echoes_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": f"invalid key {_KEY}"}},
        )

    client = httpx.Client(
        base_url="https://api.anthropic.test",
        transport=httpx.MockTransport(handler),
    )
    provider = AnthropicMessagesProvider(
        spec=_model(),
        provider=_provider(),
        api_key=_KEY,
        client=client,
    )
    with pytest.raises(ProviderError) as caught:
        provider.complete(LLMRequest(messages=[Message.user("hi")]))
    assert _KEY not in str(caught.value)
    assert _KEY not in str(getattr(caught.value, "body", ""))


class _FakeBedrockClient:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.closed = True


def test_bedrock_converse_maps_tools_and_usage() -> None:
    client = _FakeBedrockClient(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"text": "reading"},
                        {
                            "toolUse": {
                                "toolUseId": "tool-1",
                                "name": "read_file",
                                "input": {"path": "a.py"},
                            }
                        },
                    ],
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 20, "outputTokens": 4},
        }
    )
    provider = BedrockConverseProvider(
        spec=_model(),
        provider=ProviderSpec(
            name="bedrock-test",
            base_url="",
            protocol="bedrock_converse",
            region="us-east-1",
        ),
        client=client,
    )
    response = provider.complete(
        LLMRequest(
            messages=[Message.system("sys"), Message.user("read")],
            tools=[ToolSpec(name="read_file", description="read")],
            max_output_tokens=128,
            temperature=0.2,
        )
    )
    assert response.content == "reading"
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.cost_known is True
    call = client.calls[0]
    assert call["modelId"] == "claude-test-1"
    assert call["system"] == [{"text": "sys"}]
    assert call["inferenceConfig"]["maxTokens"] == 128
    assert call["inferenceConfig"]["temperature"] == 0.2
    assert call["toolConfig"]["tools"][0]["toolSpec"]["name"] == "read_file"


def test_bedrock_rate_limit_is_translated() -> None:
    class _FakeError(Exception):
        response: ClassVar[dict[str, Any]] = {
            "Error": {"Code": "ThrottlingException"},
            "ResponseMetadata": {"HTTPStatusCode": 429},
        }

    client = _FakeBedrockClient(_FakeError("slow down"))
    provider = BedrockConverseProvider(
        spec=_model(),
        provider=ProviderSpec(
            name="bedrock-test",
            base_url="",
            protocol="bedrock_converse",
            region="us-east-1",
        ),
        client=client,
    )
    with pytest.raises(RateLimitError):
        provider.complete(LLMRequest(messages=[Message.user("hi")]))
