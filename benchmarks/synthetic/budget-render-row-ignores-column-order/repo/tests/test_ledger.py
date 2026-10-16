"""``budget.ledger`` 的现有测试。"""

from __future__ import annotations

from budget.ledger import Entry, Ledger


def test_add_records_an_entry() -> None:
    ledger = Ledger()
    entry = ledger.add(20.0, "Coffee")
    assert ledger.entries == [entry]
    assert entry.category == "food"


def test_total_sums_entries() -> None:
    ledger = Ledger()
    ledger.add(20.0, "Coffee")
    ledger.add(30.5, "Lunch")
    assert ledger.total() == 50.5


def test_by_category_groups() -> None:
    ledger = Ledger()
    ledger.add(20.0, "Coffee")
    ledger.add(5.0, "Coffee")
    assert ledger.by_category() == {"food": 25.0}


def test_remove_pops_the_entry() -> None:
    ledger = Ledger()
    ledger.add(1.0, "Coffee")
    ledger.add(2.0, "Lunch")
    assert ledger.remove(0).amount == 1.0
    assert len(ledger.entries) == 1


def test_refund_adds_an_entry() -> None:
    ledger = Ledger()
    ledger.add(50.0, "Lunch")
    ledger.refund(0, 10.0)
    assert isinstance(ledger.entries[-1], Entry)
