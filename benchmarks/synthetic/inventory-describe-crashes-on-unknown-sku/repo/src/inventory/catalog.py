"""商品目录与描述。"""

from __future__ import annotations

from collections.abc import Mapping

CATALOG: dict[str, str] = {"A-1": "Widget", "B-2": "Gadget", "C-3": "Sprocket"}
UNKNOWN_NAME = "unknown"


def describe(sku: str, *, catalog: Mapping[str, str] | None = None) -> str:
    """返回 ``"SKU - 名称"``。未知 SKU 用占位名，调用方不必自己挡 KeyError。"""
    table = catalog or CATALOG
    name = table[sku]
    return f"{sku} - {name}"


def skus(catalog: Mapping[str, str] | None = None) -> list[str]:
    """排序后的 SKU 列表。"""
    table = catalog or CATALOG
    return sorted(table)
