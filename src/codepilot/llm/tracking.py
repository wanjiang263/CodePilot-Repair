"""用量与成本统计装饰器（02 §5.4，L-10）。

做成装饰器而不是塞进 provider 里，是为了让**重试与结构化输出的重复调用
也被计入**。``complete_structured`` 内部可能调用三次 provider，如果统计写在
provider 内部的"成功路径"上，前两次失败尝试的 token 就凭空消失了——
而它们同样要花钱。

失败调用也计入 token（服务端确实处理了），但事件里带 ``status="error"``，
这样"花了钱但没拿到结果"的部分可以被单独看见。

每一次调用还会向 :class:`~codepilot.llm.calls.CallRecorder` 落一条**逐请求**记录
（角色 / 图节点 / 修复轮次 / 第几次纠正 / finish_reason / 费用是否确定）。
这一层是可选的：没有 recorder 时行为与从前一致，有 recorder 时它不改任何计费口径，
只是让"谁在什么时候问了模型几次"这件事可查。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from codepilot.llm.base import LLMProvider
from codepilot.llm.calls import CallRecorder, LLMCallRecord
from codepilot.llm.types import LLMRequest, LLMResponse
from codepilot.observability.events import EventSink, EventType, EventUsage
from codepilot.schemas.usage import Usage

__all__ = ["UsageTrackingProvider", "tracks_usage"]

#: 一个 Agent 可能为同一角色发多次不同 purpose 的请求（Localizer 先选文件再选符号）。
#: 不归一的话 ``by_role`` 会出现 ``localizer_files`` 与 ``localizer_symbols`` 两个桶，
#: "每个角色花了多少"就得由读的人自己合并。
_PURPOSE_ROLES: dict[str, str] = {
    "localizer_files": "localizer",
    "localizer_symbols": "localizer",
}


class UsageTrackingProvider:
    """把每次调用的 token / 费用累加到 ``Usage``，并发出 ``llm_call`` 事件。"""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        usage: Usage,
        sink: EventSink | None = None,
        node: str | None = None,
        role: str | None = None,
        provider: str | None = None,
        recorder: CallRecorder | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._usage = usage
        self._sink = sink
        self._node = node
        self._role = role or node
        self._provider = provider
        self._recorder = recorder
        self._monotonic = monotonic

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = self._monotonic()
        # call_id 与是否配置了 recorder 无关：Agent 侧的审计要用它把工具调用对回到
        # 具体那次模型请求。
        call_id = f"llm-{uuid.uuid4().hex[:12]}"
        role = self._role or _PURPOSE_ROLES.get(request.purpose, request.purpose)
        try:
            response = self._inner.complete(request)
        except Exception as exc:
            # 失败也要计数：token 已经消耗掉了，装作没发生会让预算失控。
            self._usage.add_call(
                alias=request.purpose,
                input_tokens=0,
                output_tokens=0,
                role=role,
                cost_known=False,
                currency=self._usage.currency,
            )
            duration = _ms(self._monotonic() - started)
            uncertain = bool(getattr(exc, "uncertain", False))
            self._emit(
                request=request,
                call_id=call_id,
                duration_ms=duration,
                status="error",
                summary=str(exc),
                role=role,
            )
            self._record(
                request=request,
                call_id=call_id,
                duration_ms=duration,
                status="uncertain" if uncertain else "error",
                response=None,
                error=exc,
            )
            raise

        self._usage.add_call(
            alias=response.model_alias or request.purpose,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cached_input_tokens=response.cached_input_tokens,
            cost=response.cost,
            role=role,
            cost_known=response.cost_known,
            currency=self._usage.currency,
        )

        summary = f"{request.purpose}: {response.input_tokens} in / {response.output_tokens} out"
        if not response.cost_known:
            # 报告里必须能一眼看出"这个数字不可信"，否则总成本会被当成真的。
            summary += "（该模型未配置单价，费用记为 0）"

        duration = _ms(self._monotonic() - started)
        self._emit(
            request=request,
            call_id=call_id,
            duration_ms=duration,
            status="ok",
            summary=summary,
            response=response,
            role=role,
        )
        self._record(
            request=request,
            call_id=call_id,
            duration_ms=duration,
            status="ok",
            response=response,
            error=None,
        )
        return response.model_copy(update={"call_id": call_id})

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

    @property
    def role(self) -> str | None:
        return self._role

    def bind_sink(self, sink: EventSink | None, *, recorder: CallRecorder | None = None) -> None:
        """运行期补上事件流与逐请求记录器。

        装配时 provider 先于 run 存在（``ProviderBundle`` 在派发前就建好了），
        而 run_id / artifact 路径要到 :meth:`InferenceAssembly.run` 才知道。
        没有这一步，真实运行的 ``events.jsonl`` 里每条 ``usage`` 都是 ``null``——
        实测正是首次 GLM 运行的样子。
        """
        self._sink = sink
        if recorder is not None:
            self._recorder = recorder

    def _emit(
        self,
        *,
        request: LLMRequest,
        call_id: str,
        duration_ms: int,
        status: str,
        summary: str,
        role: str,
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
                cached_input_tokens=response.cached_input_tokens if response else 0,
                cost=response.cost if response else 0.0,
                cost_known=response.cost_known if response else False,
                currency=self._usage.currency,
            ),
            status=status,
            summary=summary,
            call_id=call_id,
            role=role,
        )

    def _record(
        self,
        *,
        request: LLMRequest,
        call_id: str,
        duration_ms: int,
        status: str,
        response: LLMResponse | None,
        error: BaseException | None,
    ) -> None:
        role = self._role or _PURPOSE_ROLES.get(request.purpose, request.purpose)
        if self._recorder is None:
            return
        context = self._recorder.context
        self._recorder.record(
            LLMCallRecord(
                call_id=call_id,
                run_id=context.run_id,
                task_id=context.task_id,
                # seq 由 recorder 统一分配，避免并发 run 之间抢同一个序号。
                seq=0,
                role=role,
                node=context.node or role,
                iteration=context.iteration,
                purpose=request.purpose,
                provider=self._provider,
                model=(response.model_alias if response else request.purpose) or "",
                attempt=request.attempt,
                correction=request.correction,
                status=status,
                finish_reason=response.finish_reason if response else None,
                input_tokens=response.input_tokens if response else 0,
                output_tokens=response.output_tokens if response else 0,
                cached_tokens=response.cached_input_tokens if response else 0,
                total_tokens=((response.input_tokens + response.output_tokens) if response else 0),
                cost=response.cost if response else None,
                cost_known=response.cost_known if response else False,
                currency=self._usage.currency,
                error_type=type(error).__name__ if error else None,
                http_status=getattr(error, "status", None) if error else None,
                tool_calls=len(response.tool_calls) if response else 0,
                duration_ms=duration_ms,
            )
        )


def tracks_usage(provider: object, usage: Usage) -> bool:
    """这条 provider 链上是否**已经有一层**在往 ``usage`` 记账。

    存在的理由是一次真实运行抓到的重复计费。调用方原本只想"没有统计层就补一层"，判据
    写成 ``isinstance(provider, UsageTrackingProvider)``；可真实链路里这个 provider 常
    被装饰器包在最外面（smoke 的 ``--temperature`` / ``--max-output-tokens`` 覆盖层就是
    一个非 ``UsageTrackingProvider`` 的包装），于是套上第二层 —— 同一个 HTTP 请求记两次
    ``add_call``、发两条 ``llm_call`` 事件，调用数与费用各多算一份。审查是唯一会自己补
    统计层的 Agent，所以这份多算正好落在 Reviewer 那一次调用上（真实运行里表现为
    "账本 26 次 / 派发账本 25 次"，差额恰好等于一次审查费用）。

    判据因此按**账本对象**而不是按最外层的类型：沿 ``inner`` 逐层剥开，只要有任何一层
    在往同一个 :class:`Usage` 记，就不再补第二层。带环的装饰器链用 ``seen`` 兜住。
    """
    seen: set[int] = set()
    node: object = provider
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, UsageTrackingProvider) and node.usage is usage:
            return True
        node = getattr(node, "inner", None)
    return False


def _ms(seconds: float) -> int:
    return max(0, int(seconds * 1000))
