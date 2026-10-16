"""``inventory.pricing`` 的现有测试。"""

from __future__ import annotations

from inventory.pricing import price_for, subtotal, with_tax


def test_subtotal_below_threshold() -> None:
    assert subtotal(5, 10.0) == 50.0


def test_subtotal_at_threshold_gets_discount() -> None:
    assert subtotal(10, 10.0) == 92.0


def test_tax_rounds_to_cents() -> None:
    assert with_tax(100.0) == 105.0


def test_price_for_combines_both() -> None:
    assert price_for(5, 10.0) == 52.5
