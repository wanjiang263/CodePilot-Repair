"""时间来源。

单独成模块是为了打断 ``storage.db`` ↔ ``storage.models`` 的循环导入，
同时留一个可替换的时间源：benchmark 里判断超时全部走这里，
测试可以 monkeypatch 它而不必真的等。
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["utcnow"]


def utcnow() -> datetime:
    """带时区的当前 UTC 时间。

    全部时间戳都用带时区的 UTC，避免"两台时区不同的机器上耗时算出负数"。
    """
    return datetime.now(UTC)
