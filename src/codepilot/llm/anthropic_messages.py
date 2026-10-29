"""Native Anthropic Messages adapter.

The project deliberately does not install the Anthropic SDK: the request and
response shapes are small, and keeping one ``httpx`` client makes the same
MockTransport tests cover both OpenAI-compatible and native providers.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, TypedDict

import httpx

from codepilot.llm.capabilities import prepare_messages
from codepilot.llm.config import ModelSpec, ProviderSpec
from codepilot.llm.cost import compute_cost
from codepilot.llm.dispatch_budget import guarded_send
from codepilot.llm.errors import (
    ContextTooLongError,
    ProviderError,
    ProviderNotAllowedError,
    RateLimitError,
)
from codepilot.llm.openai_compatible import looks_like_context_overflow
from codepilot.llm.retry import ConcurrencyLimiter, RetryPolicy, call_with_retry
from codepilot.llm.types import LLMRequest, LLMResponse, Message, Role, ToolCall
from codepilot.net import build_http_client
from codepilot.observability.redaction import REDACTED

__all__ = ["AnthropicMessagesProvider"]

MESSAGES_PATH = "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class _ErrorContext(TypedDict):
    provider: str
    model_alias: str
    status: int


class AnthropicMessagesProvider:
    def __init__(
        self,
        *,
        spec: ModelSpec,
        provider: ProviderSpec,
        api_key: str,
        client: httpx.Client | None = None,
        policy: RetryPolicy | None = None,
        limiter: ConcurrencyLimiter | None = None,
        exchange_rates: dict[str, float] | None = None,
        budget_currency: str = "CNY",
        allowed_providers: list[str] | None = None,
        on_retry: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if allowed_providers is not None and provider.name not in allowed_providers:
            raise ProviderNotAllowedError(
                f"provider {provider.name!r} 不在 allowed_providers 白名单内",
                provider=provider.name,
                model_alias=spec.alias,
            )
        self.spec = spec
        self.provider = provider
        self.policy = policy or RetryPolicy()
        self.limiter = limiter or ConcurrencyLimiter(provider.max_concurrency)
        self.exchange_rates = exchange_rates or {}
        self.budget_currency = budget_currency
        self.on_retry = on_retry
        self.sleep = sleep
        self._api_key = api_key
        base = provider.base_url.rstrip("/")
        self._messages_path = "/messages" if base.endswith("/v1") else MESSAGES_PATH
        self._request_headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            **provider.extra_headers,
        }
        self._owns_client = client is None
        self._client = client or build_http_client(
            base_url=provider.base_url,
            timeout=provider.timeout_seconds,
        )
        self.attempts = 0
        self.unacknowledged = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        body = build_messages_body(request, self.spec)
        with self.limiter:
            response = call_with_retry(
                lambda: self._post(body, request.purpose),
                policy=self.policy,
                sleep=self.sleep,
                on_retry=self.on_retry,
            )
        return self._parse(response)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> AnthropicMessagesProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _scrub(self, text: str) -> str:
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, REDACTED)
        return text

    def _post(self, body: dict[str, Any], purpose: str = "generic") -> httpx.Response:
        from codepilot.observability.dispatch import check_dispatch, dispatch_timeout

        check_dispatch("model")
        headers = {**self._request_headers, "X-CodePilot-Purpose": purpose}

        def send_http() -> httpx.Response:
            remaining = dispatch_timeout(self.provider.timeout_seconds)
            timeout = httpx.Timeout(
                remaining,
                **{
                    key: min(value, remaining) if value is not None else remaining
                    for key, value in self._client.timeout.as_dict().items()
                },
            )
            from codepilot.observability.dispatch import mark_model_dispatch

            mark_model_dispatch()
            self.attempts += 1
            return self._client.post(
                self._messages_path, json=body, headers=headers, timeout=timeout
            )

        try:
            response = guarded_send(
                self.spec,
                body,
                send_http,
            )
        except httpx.TimeoutException as exc:
            self.unacknowledged += 1
            raise ProviderError(
                f"请求超时: {self._scrub(str(exc))}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                uncertain=True,
            ) from exc
        except httpx.TransportError as exc:
            self.unacknowledged += 1
            raise ProviderError(
                f"传输失败: {self._scrub(str(exc))}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                uncertain=True,
            ) from exc
        if not response.is_success:
            self._raise_for_status(response)
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        text = self._scrub(response.text)
        common: _ErrorContext = {
            "provider": self.provider.name,
            "model_alias": self.spec.alias,
            "status": response.status_code,
        }
        if response.status_code == 429:
            raise RateLimitError(
                "触发限流",
                retry_after=_retry_after(response.headers.get("retry-after")),
                body=_safe_json(text),
                **common,
            )
        if response.status_code >= 500:
            raise ProviderError("服务端错误", body=_safe_json(text), **common)
        if response.status_code == 400 and looks_like_context_overflow(text):
            raise ContextTooLongError("上下文超限", body=_safe_json(text), **common)
        raise ProviderError(f"请求被拒绝 {response.status_code}", body=_safe_json(text), **common)

    def _parse(self, response: httpx.Response) -> LLMResponse:
        text = self._scrub(response.text)
        try:
            raw = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"响应体不是合法 JSON: {exc}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=text[:2000],
            ) from exc
        if not isinstance(raw, dict):
            raise ProviderError(
                "响应顶层不是对象",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=raw,
            )

        content_blocks = raw.get("content")
        if not isinstance(content_blocks, list):
            raise ProviderError(
                "响应缺少 content 数组",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=raw,
            )
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block_type == "thinking" and isinstance(block.get("thinking"), str):
                thinking.append(block["thinking"])
            elif block_type == "tool_use":
                arguments = block.get("input")
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id") or ""),
                        name=str(block.get("name") or ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                        raw_arguments=json.dumps(arguments, ensure_ascii=False)
                        if isinstance(arguments, dict)
                        else None,
                    )
                )

        usage = raw.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        cached_tokens = int(usage.get("cache_read_input_tokens") or 0)
        cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_input = input_tokens + cached_tokens + cache_creation
        breakdown = compute_cost(
            price=self.spec.price,
            input_tokens=total_input,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            target_currency=self.budget_currency,
            exchange_rates=self.exchange_rates,
        )
        return LLMResponse(
            content="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            input_tokens=total_input,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            cost=breakdown.amount,
            cost_known=breakdown.known,
            model_alias=self.spec.alias,
            finish_reason=raw.get("stop_reason"),
            reasoning_content="\n".join(thinking) if thinking else None,
            raw=raw,
        )


def build_messages_body(request: LLMRequest, spec: ModelSpec) -> dict[str, Any]:
    messages = prepare_messages(request, spec)
    system_parts = [message.content or "" for message in messages if message.role is Role.SYSTEM]
    body: dict[str, Any] = {
        "model": spec.model,
        "max_tokens": request.max_output_tokens,
        "messages": _encode_messages(messages),
    }
    if system_parts:
        body["system"] = "\n\n".join(part for part in system_parts if part)
    if spec.supports_temperature:
        body["temperature"] = request.temperature
    if request.tools and spec.supports_tools:
        body["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in request.tools
        ]
        body["tool_choice"] = {"type": "auto"}
    return body


def _encode_messages(messages: list[Message]) -> list[dict[str, Any]]:
    encoded: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.SYSTEM:
            continue
        if message.role is Role.TOOL:
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.content or "",
            }
            if (
                encoded
                and encoded[-1]["role"] == "user"
                and isinstance(encoded[-1].get("content"), list)
            ):
                encoded[-1]["content"].append(block)
            else:
                encoded.append({"role": "user", "content": [block]})
            continue
        if message.role is Role.ASSISTANT:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
            encoded.append(
                {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}
            )
            continue
        encoded.append({"role": "user", "content": message.content or ""})
    return encoded


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
