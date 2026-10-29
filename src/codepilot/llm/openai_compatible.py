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
from codepilot.llm.dispatch_budget import DispatchBudgetStoppedError, guarded_send
from codepilot.llm.errors import (
    ContextTooLongError,
    ProviderError,
    ProviderNotAllowedError,
    RateLimitError,
)
from codepilot.llm.http_lifecycle import send_request
from codepilot.llm.retry import ConcurrencyLimiter, RetryPolicy, call_with_retry
from codepilot.llm.types import LLMRequest, LLMResponse, ToolCall
from codepilot.net import build_http_client
from codepilot.observability.redaction import REDACTED

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
        # 只为了在错误文本里把它的值抹掉，见 :meth:`_scrub`。
        self._api_key = api_key

        self._owns_client = client is None
        self._client = client or build_http_client(
            base_url=provider.base_url,
            timeout=httpx.Timeout(
                provider.timeout_seconds,
                connect=provider.connect_timeout_seconds or provider.timeout_seconds,
                write=provider.write_timeout_seconds or provider.timeout_seconds,
                pool=provider.pool_timeout_seconds or provider.timeout_seconds,
            ),
            trust_env=provider.trust_env,
        )

        #: 真正发出去的 HTTP 请求次数（含每一次重试）。``Usage`` 只能记录"成功的
        #: 调用消耗了多少 token"，而"一共打了多少次服务端"是限流与账单核对的唯一依据。
        self.attempts = 0
        #: 已经发出但**结果未知**的请求：超时与连接中断时，服务端可能已经生成并计费，
        #: 也可能没有。这个数字必须留着，否则费用会被当成确定的。
        self.unacknowledged = 0

    # ---------------------------------------------------------------- 调用

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = prepare_messages(request, self.spec)
        body = build_request_body(request, self.spec, messages)

        from codepilot.observability.dispatch import check_dispatch, model_request_scope

        with model_request_scope(self.provider.timeout_seconds), self.limiter:
            response = call_with_retry(
                lambda: self._post(body, request.purpose),
                policy=self.policy,
                sleep=self.sleep,
                on_retry=self.on_retry,
                before_retry=lambda: check_dispatch("model"),
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

    def _scrub(self, text: str) -> str:
        """把本次请求用的密钥从任何将要不落盘/落盘的文本里抹掉。

        :func:`codepilot.observability.redaction.redact` 按**键名**脱敏，管不住
        "密钥作为某个 message 字符串的一部分被回显"这种情况 —— 而服务商的 401
        错误体恰恰会这么回显。到这一步只剩值本身可认，所以由持有密钥的 provider
        负责。不做这件事的后果是：一次排错截图就把密钥带进了 artifacts、Issue 或
        聊天记录。
        """
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, REDACTED)
        return text

    def _post(self, body: dict[str, Any], purpose: str = "generic") -> httpx.Response:
        """发一次请求，把 HTTP 层的失败翻译成本层的异常类型。"""
        from codepilot.observability.dispatch import check_dispatch, dispatch_timeout

        check_dispatch("model")
        headers = {**self._request_headers, "X-CodePilot-Purpose": purpose}

        def send_http() -> httpx.Response:
            remaining = dispatch_timeout(self.provider.timeout_seconds)
            deadline = time.monotonic() + remaining
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
            return self._send_before_deadline(
                body=body, headers=headers, timeout=timeout, deadline=deadline
            )

        try:
            response = guarded_send(
                self.spec,
                body,
                send_http,
                metadata={
                    "purpose": purpose,
                    "request_model": body.get("model"),
                    "request_temperature": body.get("temperature"),
                    "request_max_tokens": body.get("max_tokens"),
                    "request_body_bytes": len(json.dumps(body).encode("utf-8")),
                    "message_roles": [m.get("role") for m in body.get("messages", [])],
                    "timeout": self._client.timeout.as_dict(),
                    "trust_env": self._client.trust_env,
                },
            )
        except DispatchBudgetStoppedError as exc:
            if exc.dispatched:
                self.unacknowledged += 1
            raise
        except httpx.TimeoutException as exc:
            # 请求已经离开本机：服务端可能已经生成并计费，也可能没有。
            self.unacknowledged += 1
            raise ProviderError(
                f"请求超时: {self._scrub(str(exc))}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                uncertain=True,
            ) from exc
        except httpx.TransportError as exc:
            # 连接被重置、DNS 失败等等，都属于可重试。
            # DNS 失败与"连上之后断开"是两类：只有后者可能已经发出过请求体。
            # 这里一律记为结果未知 —— 少报一次比假装确定一次安全。
            self.unacknowledged += 1
            raise ProviderError(
                f"传输失败: {self._scrub(str(exc))}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                uncertain=True,
            ) from exc

        if response.is_success:
            return response

        self._raise_for_status(response)

        # _raise_for_status 必然抛异常；这行只是为了让类型检查器满意。
        raise ProviderError(  # pragma: no cover
            "unreachable", provider=self.provider.name, model_alias=self.spec.alias
        )

    def _send_before_deadline(
        self,
        *,
        body: dict[str, Any],
        headers: dict[str, str],
        timeout: httpx.Timeout,
        deadline: float,
    ) -> httpx.Response:
        """Read and close one request under an interruptible absolute deadline."""
        return send_request(
            self._client,
            CHAT_COMPLETIONS_PATH,
            body=body,
            headers=headers,
            timeout=timeout,
            deadline=deadline,
            trust_env=self.provider.trust_env,
        )

    def _raise_for_status(self, response: httpx.Response) -> None:
        text = self._scrub(response.text)
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
        try:
            raw = response.json()
        except ValueError as exc:
            # 200 但正文不是 JSON：网关截断、HTML 错误页都是这个症状。它必须报成
            # 一次 provider 失败，而不是让 JSONDecodeError 穿透到 agent 层变成
            # "无法归因的内部错误"。
            raise ProviderError(
                f"响应体不是合法 JSON: {exc}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=self._scrub(response.text)[:2000],
            ) from exc
        if not isinstance(raw, dict):
            raise ProviderError(
                f"响应顶层不是对象，收到 {type(raw).__name__}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=raw,
            )
        choices = raw.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else None
        if first is None:
            raise ProviderError(
                "响应中没有 choices",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=raw,
            )

        message = first.get("message") or {}
        if not isinstance(message, dict):
            raise ProviderError(
                "choices[0].message 不是对象",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=raw,
            )
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            # 把非字符串 content 当成"读不出来的最终答案"，而不是 repr() 一个 dict
            # 冒充模型说的话。reasoning_content / tool_calls 仍然照常解析。
            raise ProviderError(
                f"content 不是字符串，收到 {type(content).__name__}",
                provider=self.provider.name,
                model_alias=self.spec.alias,
                body=raw,
            )
        tool_calls = self._extract_tool_calls(message, content, request)
        finish_reason = first.get("finish_reason")

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
            finish_reason=finish_reason,
            # 思考内容只记录、不回流：多数服务商拒绝接受带思考内容的历史消息。
            reasoning_content=message.get("reasoning_content") or message.get("reasoning"),
            raw=raw,
        )

    def _extract_tool_calls(
        self, message: dict[str, Any], content: str | None, request: LLMRequest
    ) -> list[ToolCall]:
        native = message.get("tool_calls") or []
        if native:
            if not isinstance(native, list) or any(not isinstance(item, dict) for item in native):
                # 结构不对就报错，绝不当作"模型没有调用工具"：静默丢弃一次工具调用
                # 会让 Agent 以为模型直接给出了答案。
                raise ProviderError(
                    "tool_calls 结构非法（不是对象数组）",
                    provider=self.provider.name,
                    model_alias=self.spec.alias,
                )
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
