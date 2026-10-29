"""重试与并发控制（02 §5.3，L-08）。

两条规则值得说明：

1. **只重试可能自愈的错误**：429 和 5xx。400 参数错误、401 鉴权失败、
   上下文超限都属于"再发一次结果一样"，重试只会把延迟乘以三，还会浪费配额。
2. **退避必须带抖动**：benchmark 并行跑 20 个任务时，它们几乎同时撞上 429，
   固定退避会让 20 个请求在同一毫秒再次涌向服务端，把限流变成持续的自我拥塞。

``sleep`` 与 ``rand`` 都是可注入的，因此重试逻辑的测试不需要真的等待。
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from codepilot.llm.errors import ContextTooLongError, LLMError, ProviderError, RateLimitError

_entries: ContextVar[tuple[tuple[int, float], ...]] = ContextVar("provider_slots", default=())

__all__ = [
    "RETRYABLE_STATUS",
    "RetryPolicy",
    "call_with_retry",
    "delay_for",
    "is_retryable",
]


RETRYABLE_STATUS: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504})
"""可重试的 HTTP 状态码。

不含 400/401/403/404/422：这些是请求本身的问题，重试没有意义。
"""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """退避参数。"""

    max_attempts: int = 4
    """总尝试次数（含首次）。02 §5.3 定为 3 次重试，即最多 4 次尝试。"""

    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter_ratio: float = 0.25
    """抖动幅度占退避时长的比例，取 ``[1-r, 1+r]`` 区间内的随机倍率。"""

    respect_retry_after: bool = True
    retry_after_cap: float = 60.0
    """服务端给的 ``Retry-After`` 可能很长（比如 300 秒）。超过上限就放弃等待，
    把失败如实上报比让一次调用悄悄挂住五分钟要好。"""

    @property
    def max_retries(self) -> int:
        return max(0, self.max_attempts - 1)


def is_retryable(exc: BaseException) -> bool:
    """该异常是否值得重试。

    **先看状态码，再看类型。** ``ProviderError`` 同时承载两类失败：5xx（服务端
    抖动，该重试）和 4xx（请求本身有问题，重试无意义）。只按类型判断会让一个
    "temperature 超出范围"的 400 被重发三次，既浪费时间又浪费配额。

    没有状态码的 ``ProviderError`` 来自传输层（超时、连接重置），属于可重试。
    """
    if isinstance(exc, ContextTooLongError):
        # 上下文超限走的是"按 50% 预算重建"的路径，不是重试。
        return False
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, LLMError):
        if exc.status is not None:
            return exc.status in RETRYABLE_STATUS
        return isinstance(exc, ProviderError)
    return False


def delay_for(
    attempt: int,
    policy: RetryPolicy,
    *,
    retry_after: float | None = None,
    rand: Callable[[], float] = random.random,
) -> float | None:
    """第 ``attempt`` 次失败后应等待的秒数。

    ``attempt`` 从 1 开始计（首次调用失败为 1）。返回 ``None`` 表示
    ``Retry-After`` 超过了上限，调用方应放弃重试而不是等待。
    """
    if retry_after is not None and policy.respect_retry_after:
        if retry_after > policy.retry_after_cap:
            return None
        # 服务端明确给了等待时长，就不要再叠抖动：那是它算好的配额窗口。
        return max(retry_after, 0.0)

    # 用 2.0 而不是 2：`int ** int` 在 typeshed 里返回 Any（负指数会得到 float），
    # 那个 Any 会顺着表达式污染整个函数的返回类型。
    backoff = min(policy.base_delay * (2.0 ** (attempt - 1)), policy.max_delay)
    factor = 1.0 + policy.jitter_ratio * (2.0 * rand() - 1.0)
    return max(backoff * factor, 0.0)


def call_with_retry[T](
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    before_retry: Callable[[], None] | None = None,
) -> T:
    """执行 ``fn``，按策略重试。

    ``on_retry(attempt, exc, delay)`` 在每次等待前调用，用于上报事件——
    重试是静默失败的主要来源，必须留下痕迹。

    重试耗尽后抛出**最后一次**的异常：它携带最新一次的错误信息，
    比第一次失败的原因更有诊断价值。
    """
    policy = policy or RetryPolicy()
    last_exc: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc) or attempt >= policy.max_attempts:
                raise

            retry_after = getattr(exc, "retry_after", None)
            delay = delay_for(attempt, policy, retry_after=retry_after, rand=rand)
            if delay is None:
                raise

            if on_retry is not None:
                on_retry(attempt, exc, delay)
            if before_retry is not None:
                # 总时限可能在两次尝试之间耗尽。此时再发只会在派发前被拦下，
                # 而且"已派发、结果未知"的原始错误会被"未派发"掩盖——预扣的费用
                # 会因此被当成未发生而退款。直接抛原始错误并保留停止原因为 cause。
                try:
                    before_retry()
                except Exception as stop:
                    raise exc from stop
            sleep(delay)

    # 循环体要么 return 要么 raise，走到这里只可能是 max_attempts <= 0。
    assert last_exc is not None
    raise last_exc


@dataclass
class ConcurrencyLimiter:
    """按 provider 限制并发（02 §5.3）。

    用 ``threading.Semaphore`` 而不是 asyncio 信号量：LLM 调用是同步阻塞的，
    benchmark 的并行来自进程/线程池，不是事件循环。
    """

    limit: int = 4
    _semaphore: threading.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"并发上限必须 >= 1，收到 {self.limit}")
        self._semaphore = threading.Semaphore(self.limit)

    def __enter__(self) -> ConcurrencyLimiter:
        from codepilot.observability.dispatch import check_dispatch, observe_dispatch

        started = time.monotonic()
        observe_dispatch("resource_wait", resource="model")
        while True:
            check_dispatch("model")
            if self._semaphore.acquire(timeout=0.1):
                break
        try:
            check_dispatch("model")
        except BaseException:
            self._semaphore.release()
            raise
        observe_dispatch(
            "resource_acquired", resource="model", queued_seconds=time.monotonic() - started
        )
        _entries.set((*_entries.get(), (id(self), time.monotonic())))
        return self

    def __exit__(self, *exc_info: object) -> None:
        from codepilot.observability.dispatch import observe_dispatch

        self._semaphore.release()
        entries = list(_entries.get())
        for index in range(len(entries) - 1, -1, -1):
            if entries[index][0] == id(self):
                _, started = entries.pop(index)
                _entries.set(tuple(entries))
                observe_dispatch(
                    "resource_released",
                    resource="model",
                    execution_seconds=time.monotonic() - started,
                )
                break

    @property
    def available(self) -> int:
        """当前剩余许可数。仅用于监控，不用来做控制判断——那会有竞态。"""
        return self._semaphore._value
