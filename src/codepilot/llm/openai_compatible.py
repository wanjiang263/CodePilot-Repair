"""OpenAI Chat Completions 兼容 provider（02 §5.3，L-05）。

用 ``httpx`` 而不是各家官方 SDK：SDK 会把"服务商的差异"编进依赖树，
三家就要装三个包，而它们对 ``httpx`` 的版本要求经常互相冲突。
直接用 HTTP 之后，接入新服务商只是往 YAML 里加一段配置。

测试用 ``httpx.MockTransport``（02 §5.3 明确要求），因此**不需要**
真实密钥、不产生费用、不依赖网络状态。这也是唯一能稳定断言
"请求体长什么样"的方式。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, TypedDict

import httpx

from codepilot.llm.capabilities import build_request_body, parse_tool_protocol, prepare_messages
from codepilot.llm.config import ModelSpec, ProviderSpec
from codepilot.llm.cost import compute_cost
from codepilot.llm.errors import (
    ContextTooLongError,
    ProviderError,
    ProviderNotAllowedError,
    RateLimitError,
)
from codepilot.llm.retry import ConcurrencyLimiter, RetryPolicy, call_with_retry
from codepilot.llm.types import LLMRequest, LLMResponse, ToolCall

__all__ = ["CONTEXT_OVERFLOW_MARKERS", "OpenAICompatibleProvider", "looks_like_context_overflow"]


class _ErrorContext(TypedDict):
    """所有错误都带上的定位信息。"""

    provider: str
    model_alias: str
    status: int


CHAT_COMPLETIONS_PATH = "/chat/completions"

CONTEXT_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "context window",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "input is too long",
)
"""判定 400 是否为"上下文超限"的关键词。

刻意**不含** ``max_tokens``：那通常是参数本身不合法，重建上下文解决不了，
误判会白白多花一次重建 + 调用的钱。
"""


def looks_like_context_overflow(body_text: str) -> bool:
    lowered = body_text.lower()
    return any(marker in lowered for marker in CONTEXT_OVERFLOW_MARKERS)


class OpenAICompatibleProvider:
    """兼容 OpenAI Chat Completions 协议的服务商客户端。"""

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
            # 在构造时拒绝，而不是在调用时：一次跑 100 个任务的 benchmark
            # 应该在开始前就失败，而不是跑到第 37 个任务才报错。
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

        # 鉴权头逐次请求显式带上，而不是只配在自建 client 上。
        # 如果一个 client 被多个 provider 共享（测试里很常见），
        # 后者会覆盖前者的默认头，请求就会**静默地无鉴权发出**，
        # 症状是 401 而不是"配置漏了"。
        self._request_headers = {
            "Authorization": f"Bearer {api_key}",
            **provider.extra_headers,
        }

        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=provider.base_url,
            timeout=provider.timeout_seconds,
        )

    # ---------------------------------------------------------------- 调用

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = prepare_messages(request, self.spec)
        body = build_request_body(request, self.spec, messages)

        with self.limiter:
            response = call_with_retry(
                lambda: self._post(body),
                policy=self.policy,
                sleep=self.sleep,
                on_retry=self.on_retry,
            )

        return self._parse(response, request)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatibleProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 内部

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        """发一次请求，把 HTTP 层的失败翻译成本层的异常类型。"""
        try:
            response = self._client.post(
                CHAT_COMPLETIONS_PATH, json=body, headers=self._request_headers
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"请求超时: {exc}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
            ) from exc
        except httpx.TransportError as exc:
            # 连接被重置、DNS 失败等等，都属于可重试。
            raise ProviderError(
                f"传输失败: {exc}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
            ) from exc

        if response.is_success:
            return response

        self._raise_for_status(response)

        # _raise_for_status 必然抛异常；这行只是为了让类型检查器满意。
        raise ProviderError(  # pragma: no cover
            "unreachable", provider=self.provider.name, model_alias=self.spec.alias
        )

    def _raise_for_status(self, response: httpx.Response) -> None:
        text = response.text
        # 显式声明类型，而不是让它推成 dict[str, object]：
        # 后者用 ** 展开时与 LLMError 的具名关键字参数对不上。
        common: _ErrorContext = {
            "provider": self.provider.name,
            "model_alias": self.spec.alias,
            "status": response.status_code,
        }

        if response.status_code == 429:
            raise RateLimitError(
                "触发限流",
                retry_after=_parse_retry_after(response.headers.get("retry-after")),
                body=_safe_json(text),
                **common,
            )

        if response.status_code >= 500:
            raise ProviderError(
                f"服务端错误 {response.status_code}",
                body=_safe_json(text),
                **common,
            )

        if response.status_code == 400 and looks_like_context_overflow(text):
            raise ContextTooLongError(
                "上下文超限",
                body=_safe_json(text),
                **common,
            )

        raise ProviderError(
            f"请求被拒绝 {response.status_code}",
            body=_safe_json(text),
            **common,
        )

    def _parse(self, response: httpx.Response, request: LLMRequest) -> LLMResponse:
        raw = response.json()
        choices = raw.get("choices") or []
        if not choices:
            raise ProviderError(
                "响应中没有 choices",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=raw,
            )

        message = choices[0].get("message") or {}
        content = message.get("content")
        tool_calls = self._extract_tool_calls(message, content, request)

        usage = raw.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)

        breakdown = compute_cost(
            price=self.spec.price,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            target_currency=self.budget_currency,
            exchange_rates=self.exchange_rates,
        )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            cost=breakdown.amount,
            cost_known=breakdown.known,
            model_alias=self.spec.alias,
            # 思考内容只记录、不回流：多数服务商拒绝接受带思考内容的历史消息。
            reasoning_content=message.get("reasoning_content") or message.get("reasoning"),
            raw=raw,
        )

    def _extract_tool_calls(
        self, message: dict[str, Any], content: str | None, request: LLMRequest
    ) -> list[ToolCall]:
        native = message.get("tool_calls") or []
        if native:
            return [_parse_native_tool_call(tc) for tc in native]

        # 没有原生 tool calling 时，模型按 prompt 里的协议把调用写在 content 里。
        if request.tools and not self.spec.supports_tools and content:
            parsed = parse_tool_protocol(content)
            if parsed is not None:
                return [parsed]

        return []


def _parse_native_tool_call(raw: dict[str, Any]) -> ToolCall:
    """把 OpenAI 格式的 tool_call 转成本项目的 ``ToolCall``。

    参数是 JSON **字符串**。模型偶尔会产出非法 JSON，这不能直接抛异常——
    按 02 §5.3，那属于一次结构化输出失败，要带着错误信息重试，
    所以这里把错误记在 ``arguments_error`` 上交给上层。
    """
    function = raw.get("function") or {}
    name = function.get("name") or ""
    raw_arguments = function.get("arguments")

    if raw_arguments is None:
        raw_arguments = "{}"

    if not isinstance(raw_arguments, str):
        # 有些服务商直接返回对象，不返回字符串。
        return ToolCall(
            id=str(raw.get("id") or ""),
            name=name,
            arguments=raw_arguments if isinstance(raw_arguments, dict) else {},
            raw_arguments=json.dumps(raw_arguments, ensure_ascii=False),
        )

    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return ToolCall(
            id=str(raw.get("id") or ""),
            name=name,
            arguments={},
            raw_arguments=raw_arguments,
            arguments_error=f"arguments 不是合法 JSON: {exc.msg} (位置 {exc.pos})",
        )

    if not isinstance(arguments, dict):
        return ToolCall(
            id=str(raw.get("id") or ""),
            name=name,
            arguments={},
            raw_arguments=raw_arguments,
            arguments_error=f"arguments 必须是 JSON 对象，收到 {type(arguments).__name__}",
        )

    return ToolCall(
        id=str(raw.get("id") or ""),
        name=name,
        arguments=arguments,
        raw_arguments=raw_arguments,
    )


def _parse_retry_after(header: str | None) -> float | None:
    """``Retry-After`` 可以是秒数，也可以是 HTTP 日期。这里只处理秒数。

    日期形式返回 ``None``：解析它需要当前时间，而重试退避用相对秒数就够了，
    退回指数退避是安全的。
    """
    if not header:
        return None
    try:
        return float(header.strip())
    except ValueError:
        return None


def _safe_json(text: str) -> Any:
    """尽力把错误体解析成 JSON，失败就原样返回字符串。

    调用方在把它写进日志前必须过 ``redact()``——错误体里常带回显的请求头。
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
