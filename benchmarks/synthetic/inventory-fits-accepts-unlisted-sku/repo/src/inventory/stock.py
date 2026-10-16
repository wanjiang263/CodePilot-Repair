"""库存：持有数量状态。"""

from __future__ import annotations

from dataclasses import dataclass, field


class OutOfStock(Exception):
    """库存不足。"""


class InvalidQuantity(Exception):
    """数量不合法。"""


@dataclass
class Line:
    """订单里的一行。"""

    sku: str
    quantity: int


@dataclass
class Stock:
    """若干 SKU 的库存台账。"""

    quantities: dict[str, int] = field(default_factory=dict)

    def restock(self, sku: str, amount: int) -> None:
        """入库。数量必须为正。"""
        if amount <= 0:
            raise InvalidQuantity(f"restock amount must be positive, got {amount}")
        self.quantities[sku] = self.quantities.get(sku, 0) + amount

    def reserve(self, line: Line) -> None:
        """预留：扣减可用数量。不足时抛 :class:`OutOfStock`。"""
        available = self.quantities.get(line.sku, 0)
        if line.quantity > available:
            raise OutOfStock(f"{line.sku}: need {line.quantity}, have {available}")
        self.quantities[line.sku] = available - line.quantity

    def available(self, sku: str) -> int:
        """某 SKU 的可用数量。"""
        return self.quantities.get(sku, 0)

    def value_at(self, prices: dict[str, float]) -> float:
        """按单价表给全部库存估价。"""
        total = 0.0
        for sku, quantity in sorted(self.quantities.items()):
            total += quantity * prices.get(sku, 0.0)
        return round(total, 2)
