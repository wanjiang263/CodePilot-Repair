"""``inventory.shipping`` 的现有测试。"""

from __future__ import annotations

import pytest

from inventory.shipping import box_count, fits, total_weight
from inventory.stock import Line


def test_box_count_exact_division() -> None:
    assert box_count(10.0, 5.0) == 2


def test_box_count_rejects_zero_capacity() -> None:
    with pytest.raises(ValueError):
        box_count(10.0, 0.0)


def test_fits_within_limits() -> None:
    assert fits([Line("A-1", 2)], {"A-1": 5}) is True


def test_fits_rejects_over_limit() -> None:
    assert fits([Line("A-1", 9)], {"A-1": 5}) is False


def test_total_weight() -> None:
    lines = [Line("A-1", 2), Line("B-2", 3)]
    assert total_weight(lines, {"A-1": 1.5, "B-2": 0.5}) == 4.5
