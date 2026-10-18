"""账本：持有分录列表并派生汇总。"""

from __future__ import annotations

from dataclasses import dataclass, field

from budget.rules import categorise


class InvalidAmount(Exception):
    """金额不合法。"""


@dataclass
class Entry:
    """一笔分录。"""

    amount: float
    note: str
    category: str


@dataclass
class Ledger:
    """一个可变的账本。"""

    entries: list[Entry] = field(default_factory=list)

    def add(self, amount: float, note: str) -> Entry:
        """记一笔。金额必须为正——退款走 :meth:`refund`，不混在这里。"""
        if amount <= 0:
            raise InvalidAmount(f"amount must be positive, got {amount}")
        entry = Entry(amount=amount, note=note, category=categorise(amount, note))
        self.entries.append(entry)
        return entry

    def refund(self, index: int, amount: float) -> Entry:
        """对第 ``index`` 笔做部分退款，记一条负分录。"""
        if amount <= 0:
            raise InvalidAmount(f"refund amount must be positive, got {amount}")
        original = self.entries[index]
        entry = Entry(amount=-amount, note=f"refund of {original.note}", category=original.category)
        self.entries.append(entry)
        return entry

    def remove(self, index: int) -> Entry:
        """删除第 ``index`` 笔并返回它。"""
        return self.entries.pop(index)

    def total(self) -> float:
        """所有分录之和。"""
        return round(sum(entry.amount for entry in self.entries), 2)

    def by_category(self) -> dict[str, float]:
        """按类别汇总；十进制金额最终取两位，半分取偶，类别独立。"""
        totals: dict[str, float] = {}
        for entry in self.entries:
            totals[entry.category] = totals.get(entry.category, 0.0) + entry.amount
        return dict(sorted(totals.items()))

    def snapshot(self) -> list[Entry]:
        """当前分录的**副本**列表，供调用方安全持有。"""
        return list(self.entries)
