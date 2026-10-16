"""``budget.report`` / ``budget.csv_writer`` 的现有测试。

这些断言刻意**不把列名写死**：加一列是本库允许的演进（见 cross-module 任务），
写死列名的可见测试会在修复后变红，于是那个修复被自己的测试挡住了。
"""

from __future__ import annotations

from budget.csv_writer import headers
from budget.ledger import Ledger
from budget.report import build_rows, render


def _ledger() -> Ledger:
    ledger = Ledger()
    ledger.add(20.0, "Coffee")
    return ledger


def test_headers_is_a_tuple_of_names() -> None:
    names = headers()
    assert isinstance(names, tuple) and len(names) >= 3
    assert all(isinstance(name, str) for name in names)


def test_build_rows_matches_headers() -> None:
    rows = build_rows(_ledger())
    assert set(rows[0]) == set(headers())


def test_render_starts_with_header_line() -> None:
    text = render(_ledger())
    assert text.splitlines()[0] == ",".join(headers())
