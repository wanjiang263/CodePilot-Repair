"""``textkit.slices`` 的现有测试。"""

from __future__ import annotations

from textkit.slices import first_n


def test_first_n_with_short_input() -> None:
    assert first_n([1, 2], 5) == [1, 2]


def test_first_n_with_two_of_four() -> None:
    assert first_n([1, 2, 3, 4], 2) == [1, 2]
