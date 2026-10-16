"""``taskqueue.retry`` 的现有测试。"""

from __future__ import annotations

import pytest

from taskqueue.retry import Exhausted, RetryPolicy, run


def test_policy_accepts_positive_attempts() -> None:
    assert RetryPolicy(attempts=1).attempts == 1


def test_run_succeeds_eventually() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("nope")
        return "ok"

    assert run(flaky, RetryPolicy(attempts=3)) == "ok"


def test_run_exhausts_and_wraps_cause() -> None:
    def always_fails() -> str:
        raise RuntimeError("nope")

    with pytest.raises(Exhausted) as info:
        run(always_fails, RetryPolicy(attempts=2))
    assert isinstance(info.value.__cause__, RuntimeError)
