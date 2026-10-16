"""把账本渲染成 CSV 文本。"""

from __future__ import annotations

from budget.csv_writer import headers, render_row
from budget.ledger import Ledger

EXPORT_CURRENCY = "CNY"


def build_rows(ledger: Ledger) -> list[dict[str, object]]:
    """把账本转成"列名 -> 值"的记录列表，键集合与 :func:`headers` 一致。"""
    rows: list[dict[str, object]] = []
    for entry in ledger.entries:
        rows.append(
            {
                "category": entry.category,
                "note": entry.note,
                "currency": EXPORT_CURRENCY,
                "amount": entry.amount,
            }
        )
    return rows


def render(ledger: Ledger) -> str:
    """整个导出文件的内容，含表头行。"""
    lines = [",".join(headers())]
    lines.extend(render_row(row) for row in build_rows(ledger))
    return "\n".join(lines) + "\n"
