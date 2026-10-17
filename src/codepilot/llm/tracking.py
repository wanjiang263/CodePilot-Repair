"""用量与成本统计装饰器（02 §5.4，L-10）。

做成装饰器而不是塞进 provider 里，是为了让**重试与结构化输出的重复调用
也被计入**。``complete_structured`` 内部可能调用三次 provider，如果统计写在
provider 内部的"成功路径"上，前两次失败尝试的 token 就凭空消失了——
而它们同样要花钱。

失败调用也计入 token（服务端确实处理了），但事件里带 ``status="error"``，
这样"花了钱但没拿到结果"的部分可以被单独看见。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from codepilot.llm.base import LLMProvider
from codepilot.llm.types import LLMRequest, LLMResponse
from codepilot.observability.events import EventSink, EventType, EventUsage
from codepilot.schemas.usage import Usage

__all__ = ["UsageTrackingProvider"]


class UsageTrackingProvider:
    """把每次调用的 token / 费用累加到 ``Usage``，并发出 ``llm_call`` 事件。"""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        usage: Usage,
        sink: EventSink | None = None,
        node: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._usage = usage
        self._sink = sink
        self._node = node
        self._monotonic = monotonic

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = self._monotonic()
        try:
            response = self._inner.complete(request)
        except Exception as exc:
            # 失败也要计数：token 已经消耗掉了，装作没发生会让预算失控。
            self._usage.add_call(alias=request.purpose, input_tokens=0, output_tokens=0)
            self._emit(
                request=request,
                duration_ms=_ms(self._monotonic() - started),
                status="error",
                summary=str(exc),
            )
            raise

        self._usage.add_call(
            alias=response.model_alias or request.purpose,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached_input_tokens=response.cached_input_tokens,
            cost=response.cost,
        )

        summary = f"{request.purpose}: {response.input_tokens} in / {response.output_tokens} out"
        if not response.cost_known:
            # 报告里必须能一眼看出"这个数字不可信"，否则总成本会被当成真的。
            summary += "（该模型未配置单价，费用记为 0）"

        self._emit(
            request=request,
            duration_ms=_ms(self._monotonic() - started),
            status="ok",
            summary=summary,
            response=response,
        )
        return response

    def close(self) -> None:
        self._inner.close()

    @property
    def inner(self) -> LLMProvider:
        """被包装者。装饰器链需要一层层剥开才能拿到真实 provider ——
        例如冒烟脚本要知道"一共发了几次 HTTP"，而这个数字只存在于最内层。
        """
        return self._inner

    @property
    def usage(self) -> Usage:
        return self._usage

    def _emit(
        self,
        *,
        request: LLMRequest,
        duration_ms: int,
        status: str,
        summary: str,
        response: LLMResponse | None = None,
    ) -> None:
        if self._sink is None:
            return
        self._sink.emit(
            node=self._node or request.purpose,
            type=EventType.LLM_CALL,
            duration_ms=duration_ms,
            usage=EventUsage(
                model=response.model_alias if response else request.purpose,
                input_tokens=response.input_tokens if response else 0,
                output_tokens=response.output_tokens if response else 0,
                cost=response.cost if response else 0.0,
            ),
            status=status,
            summary=summary,
        )


def _ms(seconds: float) -> int:
    return max(0, int(seconds * 1000))
