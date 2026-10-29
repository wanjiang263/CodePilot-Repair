"""派发账本（HTTP 真值）↔ 内部账本（``state.usage``）的对账（DA-023 §四）。

三套统计口径各有用途，必须说清各自管什么：

* **派发账本**（:mod:`codepilot.llm.dispatch_budget`）按**每一次真正离开本机的 HTTP 尝试**
  记一行，先落盘再发请求；结算后的 ``cost_cny`` 是服务商口径的真值。它是"一共打了多少次
  服务端、承诺出去多少钱"的唯一依据。
* **内部账本**（:class:`~codepilot.schemas.usage.Usage`）按**每一次经过统计层的 provider
  调用**记 ``add_call``，是图里预算判定与报告读数的来源。
* **provider ``attempts``** 是第三种口径：``call_with_retry`` 里每一次重试都算一次
  ``_post``，因此它可能大于 ``Usage.llm_calls``（重试花钱、但不算第二次 Agent 调用）。

三者**应当**相等的情形：一次调用只发一次请求、结果确定、链路上只有一个统计层。
真实单题运行（glm14，2026-10-20）里 ``state.usage`` 报 26 次 / ¥0.19581048，而派发账本
只有 25 行 / ¥0.19192368，差额恰好等于一次审查费用（¥0.0038868）——那是"Reviewer 被记
了两次账"的缺陷形状（修复见 :func:`codepilot.llm.tracking.tracks_usage`），**不是**合法
差异。那次的历史产物按原样保留，没有被改写。

合法的差异只有一类并且必须能解释：结果不确定的请求在派发账本里留下一行
``unknown_frozen``（保留整笔预留并冻结后续派发），而 ``Usage`` 那边记的是一次
``cost_known=False``、``cost=0`` 的调用 —— 行数说的是同一件事，钱却是"预留 vs 实际"两种
口径。所以批量自检要按**状态**分桶比对，不能只比总数。

本文件的数字全部来自脚本化的 HTTP 响应，与任何真实运行无关；它守的是**当前实现**的一致性。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from codepilot.llm.config import ModelPrice, ModelSpec
from codepilot.llm.cost import compute_cost
from codepilot.llm.dispatch_budget import DispatchBudget, DispatchBudgetStoppedError
from codepilot.llm.tracking import UsageTrackingProvider, tracks_usage
from codepilot.llm.types import LLMRequest, LLMResponse, Message
from codepilot.schemas.usage import Usage

INPUT_TOKENS = 24_000
OUTPUT_TOKENS = 4_000
OUTPUT_LIMIT = 4_096
PRICES = ModelPrice(input_per_mtok=0.8, output_per_mtok=2.8)
#: 一次审查的真实费用口径（与 glm14 那次同数量级；预留估算对不对得上要靠它复核）。
REVIEW_COST = compute_cost(
    price=PRICES, input_tokens=INPUT_TOKENS, output_tokens=OUTPUT_TOKENS
).amount


def _model() -> ModelSpec:
    return ModelSpec(
        alias="reviewer-alias",
        provider="p",
        model="glm-test",
        context_window=100_000,
        price=PRICES,
    )


def _http_response() -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://example.test"),
        json={
            "usage": {
                "prompt_tokens": INPUT_TOKENS,
                "completion_tokens": OUTPUT_TOKENS,
                "prompt_tokens_details": {"cached_tokens": 0},
            }
        },
    )


class _GuardedHTTP:
    """一次 ``complete`` = 一次受派发账本保护的 HTTP 请求。

    它是这条链路上唯一的 HTTP 出口，所以 ``sent`` 就是"真实发了几次请求"。对账要问的
    正是"内部账本有没有比真实请求多记"。
    """

    def __init__(self, spec: ModelSpec, guard: DispatchBudget) -> None:
        self.spec = spec
        self.guard = guard
        self.sent = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        self.sent += 1
        response = self.guard.send(self.spec, {"max_tokens": OUTPUT_LIMIT}, _http_response)
        usage = response.json()["usage"]
        settled = self.guard.records[-1]
        assert settled["status"] == "settled"
        return LLMResponse(
            content="{}",
            model_alias=self.spec.alias,
            input_tokens=int(usage["prompt_tokens"]),
            output_tokens=int(usage["completion_tokens"]),
            cost=float(settled["cost_cny"]),
            cost_known=True,
        )

    def close(self) -> None:
        return None


class _Override:
    """与冒烟脚本的请求参数覆盖层同形：不是统计层，但有 ``inner``。"""

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self.inner.complete(request)  # type: ignore[union-attr]

    def close(self) -> None:
        self.inner.close()  # type: ignore[union-attr]


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message.user("review this patch")], purpose="reviewer")


def _chain(spec: ModelSpec, guard: DispatchBudget) -> _GuardedHTTP:
    return _GuardedHTTP(spec, guard)


def test_one_http_request_books_one_call_and_one_charge(tmp_path: Path) -> None:
    """口径一致的正例：N 次请求 = N 行账本 = N 次调用 = 同一笔费用。"""
    spec = _model()
    guard = DispatchBudget(10.0, tmp_path / "ledger.json")
    usage = Usage()
    provider = UsageTrackingProvider(_chain(spec, guard), usage=usage, role="reviewer")

    for _ in range(4):
        provider.complete(_request())

    assert provider.inner.sent == 4  # type: ignore[attr-defined]
    assert len(guard.records) == 4, "每次真实请求留一行"
    assert all(record["status"] == "settled" for record in guard.records)
    assert usage.llm_calls == 4, "派发账本与内部账本同行数"
    ledger_total = sum(float(record["cost_cny"]) for record in guard.records)
    assert usage.cost == pytest.approx(ledger_total)
    assert ledger_total == pytest.approx(4 * REVIEW_COST)
    assert guard.outstanding() == 0.0, "全部结算之后没有已承诺未落账的钱"
    # 按角色的桶也必须只有一份：glm14 的形状是 ``reviewer`` 与 ``review`` 各记同一请求。
    assert sorted(usage.by_role) == ["reviewer"]
    assert usage.by_role["reviewer"].llm_calls == 4
    assert usage.role_totals_reconcile()
    assert tracks_usage(provider, usage) is True


def test_a_reviewer_behind_a_decorator_keeps_the_two_ledgers_equal(tmp_path: Path) -> None:
    """真实链路形状（覆盖层在最外面）：加了保护之后两边读数仍然一致。

    修复前这里会是"账本 2 次 / 派发 1 行"，也就是 glm14 报告里那个 26 vs 25 的差额。
    """
    spec = _model()
    guard = DispatchBudget(10.0, tmp_path / "ledger.json")
    usage = Usage()
    tracked = _chain(spec, guard)
    decorated: object = _Override(
        UsageTrackingProvider(tracked, usage=usage, role="reviewer", node="reviewer")
    )
    # PatchReviewer 的补层判据（与 agents/reviewer.py 同一句）。
    assert tracks_usage(decorated, usage) is True
    if not tracks_usage(decorated, usage):  # pragma: no cover
        decorated = UsageTrackingProvider(decorated, usage=usage, node="review")

    decorated.complete(_request())  # type: ignore[attr-defined]
    assert len(guard.records) == 1 == usage.llm_calls
    assert sorted(usage.by_role) == ["reviewer"]
    assert usage.cost == pytest.approx(REVIEW_COST)


def test_the_double_booked_shape_is_detectable_by_reconciliation(tmp_path: Path) -> None:
    """负控制：无条件再补一层统计层，就会复现"内部读数多于派发行数"。

    这条用例说明那个差额是**可检测**的，因此"账本调用数 == 派发行数"可以当批跑自检；
    它也证明 :func:`tracks_usage` 拦掉的确实是这个形状，而不是纸面上的担忧。
    """
    spec = _model()
    guard = DispatchBudget(10.0, tmp_path / "ledger.json")
    usage = Usage()
    inner = UsageTrackingProvider(_chain(spec, guard), usage=usage, role="reviewer")
    doubled = UsageTrackingProvider(inner, usage=usage, node="review")

    doubled.complete(_request())

    assert len(guard.records) == 1, "真实 HTTP 只有一次"
    assert usage.llm_calls == 2, "缺陷形状：一次请求、两笔记账"
    assert sorted(usage.by_role) == ["review", "reviewer"]
    assert usage.cost == pytest.approx(2 * REVIEW_COST)
    # 差额恰好等于一次审查费用 —— 与 glm14 的 ¥0.19581048 - ¥0.19192368 同一形状。
    assert usage.cost - sum(float(r["cost_cny"]) for r in guard.records) == pytest.approx(
        REVIEW_COST
    )


def test_an_uncertain_response_is_a_legitimate_difference_and_says_so(
    tmp_path: Path,
) -> None:
    """唯一的合法差异：不确定的请求留下**预留**行与一次未定价调用，两者不可直接相减。"""
    spec = _model()
    guard = DispatchBudget(10.0, tmp_path / "ledger.json")
    usage = Usage()

    def explode() -> httpx.Response:
        raise httpx.ConnectError("connection reset")

    class _Exploding:
        """模拟"请求已发出、结果未知"：异常发生在预留已经落盘之后。"""

        def complete(self, request: LLMRequest) -> LLMResponse:
            del request
            return guard.send(spec, {"max_tokens": OUTPUT_LIMIT}, explode)

        def close(self) -> None:
            return None

    # 统计层对同一次失败调用记 0 token、``cost_known=False``：钱可能已经花了，
    # 只是拿不到可信的 usage，不能装作没发生。
    provider = UsageTrackingProvider(_Exploding(), usage=usage, role="reviewer")
    with pytest.raises(DispatchBudgetStoppedError):
        provider.complete(_request())

    assert usage.llm_calls == 1
    assert len(guard.records) == 1
    assert guard.records[0]["status"] == "unknown_frozen"
    assert guard.frozen is True
    assert guard.outstanding() > 0.0, "预留仍在账本里：这笔钱还没结算，不能当成已花"
    assert usage.cost_unknown_calls == 1
    assert usage.cost_is_lower_bound
    assert usage.cost == 0.0
    # 冻结之后连派发都不允许再发生：既不加账本行，也不加统计调用。
    # 这一类"预扣之前就被拒"的调用没有花掉任何钱，记账会凭空放大花费。
    before = len(guard.records)
    calls_before = usage.llm_calls
    with pytest.raises(DispatchBudgetStoppedError):
        provider.complete(_request())
    assert len(guard.records) == before
    assert usage.llm_calls == calls_before
