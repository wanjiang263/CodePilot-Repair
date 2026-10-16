"""定价。"""

from __future__ import annotations

TAX_RATE = 0.05
BULK_THRESHOLD = 10
BULK_DISCOUNT = 0.08


def subtotal(quantity: int, unit_price: float) -> float:
    """小计（不含税）。数量达到门槛时打批量折扣。"""
    raw = quantity * unit_price
    if quantity >= BULK_THRESHOLD:
        raw = raw * (1 - BULK_DISCOUNT)
    return round(raw, 2)


def with_tax(amount: float) -> float:
    """含税价。"""
    return round(amount * (1 + TAX_RATE), 2)


def price_for(quantity: int, unit_price: float) -> float:
    """一行商品的含税总价。"""
    return with_tax(subtotal(quantity, unit_price))
