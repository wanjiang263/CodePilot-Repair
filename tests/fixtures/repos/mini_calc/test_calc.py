"""顶层测试文件（不在 tests/ 目录里）。"""

from __future__ import annotations

import pytest

from calc.core import divide


def test_divide_returns_float() -> None:
    assert divide(6, 3) == 2.0


def test_divide_by_zero_raises_value_error() -> None:
    with pytest.raises(ValueError):
        divide(1, 0)
