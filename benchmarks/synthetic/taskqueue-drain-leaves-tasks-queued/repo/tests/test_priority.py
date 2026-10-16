"""``taskqueue.priority`` 的现有测试。"""

from __future__ import annotations

from taskqueue.priority import order, rank, top
from taskqueue.queue import Task


def test_rank_defaults_to_normal() -> None:
    assert rank(Task("x")) == 1


def test_rank_reads_payload() -> None:
    assert rank(Task("x", payload={"level": "urgent"})) == 0
    assert rank(Task("x", payload={"level": "bogus"})) == 1


def test_top_of_empty_is_none() -> None:
    assert top([]) is None


def test_top_picks_urgent() -> None:
    tasks = [Task("n"), Task("u", payload={"level": "urgent"})]
    assert top(tasks) is not None
    assert top(tasks).name == "u"
