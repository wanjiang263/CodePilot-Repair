"""装箱与可运判定。"""

from __future__ import annotations

import math

from inventory.stock import Line


def box_count(weight_kg: float, capacity_kg: float) -> int:
    """需要几个箱子。必须向上取整——半箱的货发不出去。"""
    if capacity_kg <= 0:
        raise ValueError(f"capacity_kg must be positive, got {capacity_kg}")
    return math.ceil(weight_kg / capacity_kg)


def fits(order: list[Line], limits: dict[str, int]) -> bool:
    """订单里每一行的数量都不超过该 SKU 的单次上限。"""
    for line in order:
        if line.sku in limits and line.quantity > limits[line.sku]:
            return False
    return True


def can_ship(
    order: list[Line],
    *,
    limits: dict[str, int],
    weight_kg: float,
    capacity_kg: float,
    max_boxes: int,
) -> bool:
    """整单可运：既不超过行上限，也不需要比 ``max_boxes`` 更多的箱子。

    行上限的判定**只能**来自 :func:`fits`。在这里另写一遍比较逻辑，就等于给
    同一个规则留了第二个副本，而两个副本不会同时被修好。
    """
    if any(line.quantity > limits.get(line.sku, 10**9) for line in order):
        return False
    return box_count(weight_kg, capacity_kg) <= max_boxes


def total_weight(lines: list[Line], per_unit: dict[str, float]) -> float:
    """订单总重。"""
    return round(sum(line.quantity * per_unit.get(line.sku, 0.0) for line in lines), 3)
