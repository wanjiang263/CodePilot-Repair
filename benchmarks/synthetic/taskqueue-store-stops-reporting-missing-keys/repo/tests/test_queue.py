"""``taskqueue.queue`` 的现有测试。"""

from __future__ import annotations

from taskqueue.queue import Task, TaskQueue


def test_push_then_pop_is_fifo() -> None:
    queue = TaskQueue()
    queue.push(Task("a"))
    queue.push(Task("b"))
    assert queue.pop() is not None
    assert queue.pop() is not None
    assert len(queue) == 0


def test_pop_on_empty_returns_none() -> None:
    assert TaskQueue().pop() is None


def test_peek_does_not_remove() -> None:
    queue = TaskQueue([Task("only")])
    assert queue.peek() is not None
    assert len(queue) == 1


def test_drain_returns_every_task() -> None:
    queue = TaskQueue([Task("a"), Task("b")])
    assert len(queue.drain()) == 2


def test_history_records_operations() -> None:
    queue = TaskQueue()
    queue.push(Task("x"))
    queue.pop()
    assert queue.history == ["push:x", "pop:x"]
