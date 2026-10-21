"""装饰器链上的用量记账查重（真实 GLM 运行抓到的重复计费回归）。

现象：真实单题运行里 ``state.usage`` 记了 26 次调用 / ¥0.19581048，而派发账本只有
25 条 HTTP 请求 / ¥0.19192368，差额恰好等于一次审查的费用；``llm_call`` 事件里同一次
审查出现两条，role 分别是 ``reviewer`` 与 ``review``。

根因：``PatchReviewer`` 只想在"没有统计层"时补一层，判据写成最外层的
``isinstance(provider, UsageTrackingProvider)``。冒烟链路把每个角色 provider 都包进
一个请求参数覆盖层（``--temperature`` / ``--max-output-tokens``），最外层就不再是
``UsageTrackingProvider``，于是套上第二层 —— 同一个请求记两次。审查是唯一自己补统计层
的 Agent，所以正好落在 Reviewer 上；前两次真实运行都没能执行到审查，这条一直没暴露。
"""

from __future__ import annotations

import pytest

from codepilot.llm.fake import FakeLLM
from codepilot.llm.tracking import UsageTrackingProvider, tracks_usage
from codepilot.llm.types import LLMRequest, Message
from codepilot.schemas.usage import Usage


class _Override:
    """与冒烟脚本的请求参数覆盖层同形：不是 ``UsageTrackingProvider``，但有 ``inner``。"""

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def complete(self, request: LLMRequest):
        return self.inner.complete(request)  # type: ignore[union-attr]

    def close(self) -> None:
        self.inner.close()  # type: ignore[union-attr]


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message.user("review this patch")], purpose="reviewer")


def test_a_decorator_over_the_tracker_does_not_open_a_second_ledger() -> None:
    """覆盖层之上再补统计层 = 同一笔钱记两次。判据必须看**账本对象**，不是最外层类型。"""
    usage = Usage()
    tracked = UsageTrackingProvider(FakeLLM(["{}"]), usage=usage)
    assert tracks_usage(tracked, usage) is True
    assert tracks_usage(_Override(tracked), usage) is True
    assert tracks_usage(_Override(_Override(tracked)), usage) is True
    # 另一本账（另一个 run）上的同名统计层不算数：跨 run 串账比少记一层更糟。
    assert tracks_usage(_Override(tracked), Usage()) is False
    assert tracks_usage(FakeLLM(["{}"]), usage) is False


def test_a_decorated_tracker_books_one_call_per_http_request() -> None:
    """走一次装饰后的请求：账本只多一次调用（修复前这里会是 2）。"""
    usage = Usage()
    tracked = UsageTrackingProvider(FakeLLM(['{"approved": true, "summary": "ok"}']), usage=usage)
    provider = _Override(tracked)
    assert tracks_usage(provider, usage) is True
    provider.complete(_request())
    assert usage.llm_calls == 1


def test_a_cyclic_decorator_chain_terminates_instead_of_hanging() -> None:
    """装饰器链成环时查重必须**停下来**，而不是把宿主进程挂死。"""
    usage = Usage()

    class _Loop:
        def __init__(self) -> None:
            self.inner: object = self

        def complete(self, request: object) -> object:  # pragma: no cover
            raise AssertionError("不该被调用")

    assert tracks_usage(_Loop(), usage) is False

    tracker = UsageTrackingProvider(FakeLLM(["{}"]), usage=usage)
    entry = _Override(tracker)
    assert tracks_usage(entry, usage) is True


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_the_probe_finds_the_tracker_at_any_depth(depth: int) -> None:
    usage = Usage()
    chain: object = UsageTrackingProvider(FakeLLM(["{}"]), usage=usage)
    for _ in range(depth):
        chain = _Override(chain)
    assert tracks_usage(chain, usage) is True


# ------------------------------------------------- 一次请求 = 一次记录 + 一笔费用（§四）


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_one_request_books_one_call_and_one_charge_at_any_decorator_depth(depth: int) -> None:
    """记账的**金额**也必须只有一份：只数 ``llm_calls`` 会漏掉"同一笔钱记两次"。

    真实缺陷的表现正是两样一起翻倍（26 次 vs 25 行、¥0.19581048 vs ¥0.19192368），
    而 ``by_role`` 里会同时出现 ``reviewer`` 与 ``review`` 两个桶。
    """
    usage = Usage()
    tracked = UsageTrackingProvider(
        FakeLLM(['{"approved": true}'], cost_per_call=0.25), usage=usage
    )
    chain: object = tracked
    for _ in range(depth):
        chain = _Override(chain)
    # 与 ``agents/reviewer.py`` 同一句判据：链上已有统计层就不再补第二层。
    if not tracks_usage(chain, usage):
        chain = UsageTrackingProvider(chain, usage=usage, node="review")
    chain.complete(_request())  # type: ignore[union-attr]

    assert usage.llm_calls == 1
    assert usage.cost == pytest.approx(0.25)
    assert sorted(usage.by_role) == ["reviewer"]
    assert usage.role_totals_reconcile()


def test_interleaved_decorator_layers_still_resolve_to_one_ledger() -> None:
    """统计层夹在多层装饰器中间（真实链路就是这种形状）时仍然只记一次。

    真实冒烟链路是 ``registry 统计层 → _Override → 覆盖层``：判据只看最外层类型时，
    这一条会补出第二层；按**账本对象**查重后，无论装饰器怎么交错都只有一笔记账。
    """
    usage = Usage()
    inner = UsageTrackingProvider(
        FakeLLM(['{"approved": true}'], cost_per_call=0.5), usage=usage, role="reviewer"
    )
    chain: object = _Override(_Override(_Override(inner)))
    assert tracks_usage(chain, usage) is True
    chain.complete(_request())  # type: ignore[union-attr]
    assert usage.llm_calls == 1
    assert usage.cost == pytest.approx(0.5)
    assert sorted(usage.by_role) == ["reviewer"]


def test_the_same_usage_object_is_the_only_ledger_that_matters() -> None:
    """同一个 provider 换一本账（另一个 run 的 ``Usage``）时必须重新补层。

    查重按账本对象而不是按类型，反向也成立：不能因为"链上有个统计层"就把它服务的
    另一本账当成自己的。那会让第二个 run 的用量永远为 0，预算判定跟着一起瞎。
    """
    first = Usage()
    second = Usage()
    tracker = UsageTrackingProvider(FakeLLM(['{"approved": true}'], cost_per_call=0.1), usage=first)
    assert tracks_usage(tracker, first) is True
    assert tracks_usage(tracker, second) is False

    wrapped = UsageTrackingProvider(tracker, usage=second, node="review")
    assert tracks_usage(wrapped, second) is True
    wrapped.complete(_request())
    assert first.llm_calls == 1 and second.llm_calls == 1
    assert first.cost == pytest.approx(0.1)
    assert second.cost == pytest.approx(0.1)
    assert sorted(first.by_role) == ["reviewer"]
    assert sorted(second.by_role) == ["review"]
