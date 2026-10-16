"""支出分类与折扣规则。"""

from __future__ import annotations

RECURRING_KEYWORDS = ("rent", "subscription", "insurance")
MEMBER_DISCOUNT = 0.10
LARGE_EXPENSE = 500.0


def categorise(amount: float, note: str) -> str:
    """把一笔支出归到固定类别里。

    先看备注关键词，再看金额门槛，最后兜底到 ``general``。演示库的约定，不是
    现实账务规则。
    """
    lowered = note.casefold()
    if "insurance" in lowered:
        return "housing"
    if "coffee" in lowered or "lunch" in lowered:
        return "food"
    if amount > LARGE_EXPENSE:
        return "large"
    return "general"


def is_recurring(note: str) -> bool:
    """备注是否指向一项周期性支出。"""
    lowered = note.casefold()
    return any(keyword in lowered for keyword in RECURRING_KEYWORDS)


def apply_discount(total: float, *, member: bool) -> float:
    """会员打折，非会员原价。"""
    if member:
        return round(total * (1 - MEMBER_DISCOUNT), 2)
    return total
