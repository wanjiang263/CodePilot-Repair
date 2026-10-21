from pathlib import Path

import httpx
import pytest

from codepilot.llm.config import ModelPrice, ModelSpec
from codepilot.llm.dispatch_budget import DispatchBudget, DispatchBudgetStoppedError


def model() -> ModelSpec:
    return ModelSpec(
        alias="test",
        provider="test",
        model="test",
        context_window=1000000,
        price=ModelPrice(input_per_mtok=0.8, output_per_mtok=2.8),
    )


def response(usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://example.test"),
        json={"usage": usage} if usage else {},
    )


def test_reservation_prevents_http(tmp_path: Path) -> None:
    guard = DispatchBudget(0.1, tmp_path / "ledger.json")
    calls = []
    with pytest.raises(DispatchBudgetStoppedError):
        guard.send(model(), {"max_tokens": 4096}, lambda: calls.append(1))
    assert not calls


def test_settle_and_share_budget_across_roles(tmp_path: Path) -> None:
    guard = DispatchBudget(1, tmp_path / "ledger.json")
    for _ in range(2):
        guard.send(
            model(),
            {"max_tokens": 4096},
            lambda: response({"prompt_tokens": 100, "completion_tokens": 10}),
        )
    assert guard.total == pytest.approx(0.000216)
    assert len(guard.records) == 2


@pytest.mark.parametrize("failure", ["missing", "timeout", "invalid"])
def test_uncertain_cost_freezes_all_future_dispatch(tmp_path: Path, failure: str) -> None:
    guard = DispatchBudget(20, tmp_path / "ledger.json")
    calls = []

    def send() -> httpx.Response:
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout")
        if failure == "invalid":
            return response({"prompt_tokens": 1000001, "completion_tokens": 10})
        return response()

    for _ in range(2):
        with pytest.raises(DispatchBudgetStoppedError):
            guard.send(model(), {"max_tokens": 4096}, send)
    assert len(calls) == 1
    assert guard.frozen
    assert guard.total == pytest.approx(0.8114688)
    assert guard.records[0]["cost_cny"] is None


def test_existing_ledger_cannot_be_reset(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    DispatchBudget(20, path)
    with pytest.raises(FileExistsError):
        DispatchBudget(20, path)


@pytest.mark.parametrize(
    "price",
    [
        None,
        ModelPrice(input_per_mtok=0.8),
        ModelPrice(input_per_mtok=0.8, output_per_mtok=2.8, currency="USD"),
    ],
    ids=["no-price", "incomplete", "foreign-currency"],
)
def test_unpriceable_or_foreign_currency_never_reaches_the_network(
    tmp_path: Path, price: ModelPrice | None
) -> None:
    """费用无法确定（缺价、单价不全、币种不是账本币种）时一律 fail closed。

    派发层不做汇率换算：`USD 单价 + CNY 上限` 直接算会低估约七倍，而这种错误读起来
    像"费用还在预算内"。宁可一次请求都不发。
    """
    guard = DispatchBudget(20, tmp_path / "ledger.json")
    spec = model().model_copy(update={"price": price})
    calls: list[int] = []
    with pytest.raises(DispatchBudgetStoppedError):
        guard.send(spec, {"max_tokens": 4096}, lambda: calls.append(1))
    assert not calls
    assert guard.records == []
    assert not guard.frozen  # 从未派发，也就没有需要冻结的不确定支出
