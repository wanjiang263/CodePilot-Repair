"""声明式基类。

单独成模块：``db`` 需要 ``Base`` 来建表，``models`` 需要 ``db`` 的引擎类型，
放在一起会形成循环导入。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase

__all__ = ["Base"]


class Base(DeclarativeBase):
    pass
