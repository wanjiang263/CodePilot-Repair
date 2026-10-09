"""L-08 的证明：只重试可自愈的错误；退避带抖动；Retry-After 优先但有上限。

这里的 ``sleep`` 与 ``rand`` 都被替换掉，所以测试既不需要真的等待，
也能对退避时长做精确断言。
"""

from __future__ import annotations

import pytest

from codepilot.llm.errors import (
    ContextTooLongError,
    LLMError,
    ProviderError,
    RateLimitError,
    StructuredOutputError,
)
from codepilot.llm.retry import (
    ConcurrencyLimiter,
    RetryPolicy,
    call_with_retry,
    delay_for,
    is_retryable,
)

POLICY = RetryPolicy(max_attempts=4, base_delay=1.0, max_delay=30.0, jitter_ratio=0.0)


class _NoSleep:
    """记录睡眠时长而不真的睡。"""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


# --------------------------------------------------------------- 可重试判定


@pytest.mark.parametrize(
    "exc",
    [
        RateLimitError("429"),
        ProviderError("500", status=500),
        ProviderError("502", status=502),
        LLMError("503", status=503),
        LLMError("408", status=408),
    ],
)
def test_retryable_errors(exc: Exception) -> None:
    assert is_retryable(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ContextTooLongError("too long", status=400),
        LLMError("bad request", status=400),
        LLMError("unauthorized", status=401),
        LLMError("forbidden", status=403),
        LLMError("not found", status=404),
        StructuredOutputError("bad json"),
        ValueError("not an LLM error"),
    ],
)
def test_non_retryable_errors(exc: Exception) -> None:
    """上下文超限不重试：同样长度的请求再发一次结果一样。"""
    assert is_retryable(exc) is False


# --------------------------------------------------------------- 退避计算


def test_exponential_growth_without_jitter() -> None:
    delays = [delay_for(n, POLICY, rand=lambda: 0.5) for n in (1, 2, 3, 4)]
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(base_delay=10.0, max_delay=15.0, jitter_ratio=0.0)
    assert delay_for(1, policy, rand=lambda: 0.5) == 10.0
    assert delay_for(5, policy, rand=lambda: 0.5) == 15.0


def test_jitter_stays_within_band() -> None:
    policy = RetryPolicy(base_delay=10.0, max_delay=100.0, jitter_ratio=0.25)
    low = delay_for(1, policy, rand=lambda: 0.0)
    high = delay_for(1, policy, rand=lambda: 1.0)
    assert low == pytest.approx(7.5)
    assert high == pytest.approx(12.5)


def test_retry_after_takes_precedence() -> None:
    """服务端给的等待时长是它算好的配额窗口，不该再叠抖动。"""
    delay = delay_for(1, POLICY, retry_after=3.0, rand=lambda: 0.0)
    assert delay == 3.0


def test_retry_after_over_cap_gives_up() -> None:
    """等 300 秒不如如实报错——静默挂住五分钟比失败更难排查。"""
    assert delay_for(1, POLICY, retry_after=300.0) is None


def test_retry_after_ignored_when_disabled() -> None:
    policy = RetryPolicy(
        base_delay=1.0, max_delay=10.0, jitter_ratio=0.0, respect_retry_after=False
    )
    assert delay_for(1, policy, retry_after=5.0, rand=lambda: 0.5) == 1.0


# --------------------------------------------------------------- 执行


def test_succeeds_without_retry() -> None:
    sleep = _NoSleep()
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "ok"

    assert call_with_retry(fn, policy=POLICY, sleep=sleep) == "ok"
    assert len(calls) == 1
    assert sleep.slept == []


def test_retries_then_succeeds() -> None:
    sleep = _NoSleep()
    attempts: list[int] = []

    def fn() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RateLimitError("429")
        return "ok"

    assert call_with_retry(fn, policy=POLICY, sleep=sleep) == "ok"
    assert len(attempts) == 3
    assert sleep.slept == [1.0, 2.0]


def test_raises_last_error_after_exhaustion() -> None:
    """抛出最后一次的异常：它带的信息最新，比第一次的原因更有诊断价值。"""
    sleep = _NoSleep()

    def fn() -> str:
        raise ProviderError(f"失败第 {len(sleep.slept) + 1} 次", status=500)

    with pytest.raises(ProviderError, match="失败第 4 次"):
        call_with_retry(fn, policy=POLICY, sleep=sleep)
    assert sleep.slept == [1.0, 2.0, 4.0]


def test_non_retryable_raises_immediately() -> None:
    sleep = _NoSleep()
    attempts: list[int] = []

    def fn() -> str:
        attempts.append(1)
        raise LLMError("unauthorized", status=401)

    with pytest.raises(LLMError):
        call_with_retry(fn, policy=POLICY, sleep=sleep)
    assert len(attempts) == 1
    assert sleep.slept == []


def test_context_overflow_is_not_retried() -> None:
    sleep = _NoSleep()
    attempts: list[int] = []

    def fn() -> str:
        attempts.append(1)
        raise ContextTooLongError("too long", status=400)

    with pytest.raises(ContextTooLongError):
        call_with_retry(fn, policy=POLICY, sleep=sleep)
    assert len(attempts) == 1


def test_long_retry_after_aborts_without_sleeping() -> None:
    sleep = _NoSleep()

    def fn() -> str:
        raise RateLimitError("429", retry_after=600.0)

    with pytest.raises(RateLimitError):
        call_with_retry(fn, policy=POLICY, sleep=sleep)
    assert sleep.slept == []


def test_on_retry_callback_reports_each_attempt() -> None:
    """重试是静默失败的主要来源，必须留下痕迹。"""
    sleep = _NoSleep()
    seen: list[tuple[int, str, float]] = []
    attempts: list[int] = []

    def fn() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RateLimitError("429")
        return "ok"

    call_with_retry(
        fn,
        policy=POLICY,
        sleep=sleep,
        on_retry=lambda attempt, exc, delay: seen.append((attempt, type(exc).__name__, delay)),
    )
    assert seen == [(1, "RateLimitError", 1.0), (2, "RateLimitError", 2.0)]


# --------------------------------------------------------------- 并发限制


def test_limiter_rejects_zero_limit() -> None:
    with pytest.raises(ValueError):
        ConcurrencyLimiter(0)


def test_limiter_releases_on_exit() -> None:
    limiter = ConcurrencyLimiter(2)
    assert limiter.available == 2
    with limiter:
        assert limiter.available == 1
    assert limiter.available == 2


def test_limiter_releases_on_exception() -> None:
    limiter = ConcurrencyLimiter(1)
    with pytest.raises(RuntimeError), limiter:
        raise RuntimeError("boom")
    assert limiter.available == 1
