"""成本计算（02 §5.4）。

三件事：

1. 缓存命中的输入 token 按缓存价算，未命中的按输入价算——把两者混在一起
   会系统性高估费用，而高估的费用会让人在预算没耗尽时提前停止。
2. 各家币种不同，统一换算到 ``budget_currency`` 再比较。不换算的话，
   "预算 50 元"这句话在不同 provider 下含义完全不同。
3. 单价缺失时**不返回 0**，返回 ``None``，让调用方显式记成 ``cost_known=False``。
   把"没配价格"和"这次免费"混为一谈，是费用报表最容易被污染的方式。
"""

from __future__ import annotations

from dataclasses import dataclass

from codepilot.llm.config import ModelPrice

__all__ = ["CostBreakdown", "compute_cost", "convert"]

_MTOK = 1_000_000


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """一次调用的费用明细。保留分项是为了能回答"贵在输入还是输出"。"""

    amount: float
    currency: str
    known: bool
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0


def convert(
    amount: float, *, from_currency: str, to_currency: str, rates: dict[str, float]
) -> float:
    """按汇率换算。

    缺汇率时按 1.0 处理——这在同币种场景下是正确的，跨币种时会低估或高估，
    所以调用方要在报告里标注缺少汇率的币种。
    """
    if from_currency == to_currency:
        return amount
    rate = rates.get(from_currency, 1.0)
    return amount * rate


def compute_cost(
    *,
    price: ModelPrice | None,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    target_currency: str = "CNY",
    exchange_rates: dict[str, float] | None = None,
) -> CostBreakdown:
    """按单价与 token 数计算费用。

    ``cached_input_tokens`` 是 ``input_tokens`` 中命中缓存的那一部分，
    不是额外增加的输入。缓存价未配置时按输入价计算（保守，偏高）。
    """
    if price is None or not price.is_complete:
        return CostBreakdown(
            amount=0.0,
            currency=target_currency,
            known=False,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )

    assert price.input_per_mtok is not None  # is_complete 已保证
    assert price.output_per_mtok is not None

    # 缓存命中的部分必须从全价输入里扣掉，否则会被计两次。
    cached = min(max(cached_input_tokens, 0), input_tokens)
    fresh_input = input_tokens - cached
    cached_price = (
        price.cached_input_per_mtok
        if price.cached_input_per_mtok is not None
        else price.input_per_mtok
    )

    amount = (
        fresh_input * price.input_per_mtok
        + cached * cached_price
        + output_tokens * price.output_per_mtok
    ) / _MTOK

    converted = convert(
        amount,
        from_currency=price.currency,
        to_currency=target_currency,
        rates=exchange_rates or {},
    )

    return CostBreakdown(
        amount=converted,
        currency=target_currency,
        known=True,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )
