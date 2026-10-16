"""``textkit.stats`` 的现有测试。"""

from __future__ import annotations

from textkit.stats import mean, running_unique


def test_mean_of_sample() -> None:
    assert mean([1.0, 2.0, 3.0]) == 2.0


def test_running_unique_keeps_first_occurrence_order() -> None:
    assert running_unique(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
