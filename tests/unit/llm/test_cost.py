"""L-06 的证明：缓存命中按缓存价、跨币种换算、无单价时不假装是 0。"""

from __future__ import annotations

import pytest

from codepilot.llm.config import ModelPrice
from codepilot.llm.cost import compute_cost, convert

PRICE = ModelPrice(
    currency="CNY", input_per_mtok=1.0, output_per_mtok=2.0, cached_input_per_mtok=0.1
)


def test_basic_cost() -> None:
    """100 万输入 @1 元 + 100 万输出 @2 元 = 3 元。"""
    result = compute_cost(price=PRICE, input_tokens=1_000_000, output_tokens=1_000_000)
    assert result.amount == pytest.approx(3.0)
    assert result.known is True


def test_cached_tokens_use_cached_price() -> None:
    """缓存命中的部分不能按全价算，也不能被计两次。"""
    result = compute_cost(
        price=PRICE,
        input_tokens=1_000_000,
        cached_input_tokens=1_000_000,
        output_tokens=0,
    )
    assert result.amount == pytest.approx(0.1)


def test_partial_cache_hit() -> None:
    result = compute_cost(
        price=PRICE,
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        output_tokens=0,
    )
    # 600k 全价 + 400k 缓存价
    assert result.amount == pytest.approx(0.6 * 1.0 + 0.4 * 0.1)


def test_cached_greater_than_input_is_clamped() -> None:
    """服务商偶尔会报出比 prompt_tokens 还大的 cached_tokens。

    不夹紧的话这部分会变成负的全价输入，费用被算少，属于"看起来省钱"的错误。
    """
    result = compute_cost(
        price=PRICE,
        input_tokens=100,
        cached_input_tokens=999,
        output_tokens=0,
    )
    assert result.amount == pytest.approx(100 * 0.1 / 1_000_000)


def test_missing_cached_price_falls_back_to_input_price() -> None:
    price = ModelPrice(currency="CNY", input_per_mtok=1.0, output_per_mtok=2.0)
    result = compute_cost(
        price=price, input_tokens=1_000_000, cached_input_tokens=1_000_000, output_tokens=0
    )
    assert result.amount == pytest.approx(1.0)


def test_no_price_reports_unknown_not_zero() -> None:
    """这是本模块最重要的一条：未知费用与零费用不能长得一样。"""
    result = compute_cost(price=None, input_tokens=1000, output_tokens=100)
    assert result.known is False
    assert result.amount == 0.0
    assert result.input_tokens == 1000


def test_incomplete_price_reports_unknown() -> None:
    result = compute_cost(
        price=ModelPrice(currency="CNY", input_per_mtok=1.0),
        input_tokens=1000,
        output_tokens=100,
    )
    assert result.known is False


def test_currency_conversion() -> None:
    price = ModelPrice(currency="USD", input_per_mtok=1.0, output_per_mtok=0.0)
    result = compute_cost(
        price=price,
        input_tokens=1_000_000,
        output_tokens=0,
        target_currency="CNY",
        exchange_rates={"USD": 7.1},
    )
    assert result.amount == pytest.approx(7.1)
    assert result.currency == "CNY"


def test_same_currency_needs_no_rate() -> None:
    result = compute_cost(
        price=PRICE,
        input_tokens=1_000_000,
        output_tokens=0,
        target_currency="CNY",
        exchange_rates={},
    )
    assert result.amount == pytest.approx(1.0)


def test_missing_rate_falls_back_to_one() -> None:
    """缺汇率按 1.0 处理是保守的近似，调用方负责在报告里标注。"""
    price = ModelPrice(currency="USD", input_per_mtok=1.0, output_per_mtok=0.0)
    result = compute_cost(
        price=price, input_tokens=1_000_000, output_tokens=0, target_currency="CNY"
    )
    assert result.amount == pytest.approx(1.0)


def test_convert_same_currency_is_identity() -> None:
    assert convert(5.0, from_currency="CNY", to_currency="CNY", rates={}) == 5.0


def test_zero_tokens_cost_nothing() -> None:
    result = compute_cost(price=PRICE, input_tokens=0, output_tokens=0)
    assert result.amount == 0.0
    assert result.known is True
