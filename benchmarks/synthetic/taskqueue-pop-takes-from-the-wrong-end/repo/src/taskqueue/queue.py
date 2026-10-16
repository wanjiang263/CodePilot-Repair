"""FIFO 任务队列。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Task:
    """一项任务。"""

    name: str
    payload: dict[str, object] = field(default_factory=dict)


class TaskQueue:
    """一个可观察长度的队列。"""

    def __init__(self, tasks: list[Task] | None = None) -> None:
        self._items: deque[Task] = deque(tasks or [])
        self.history: list[str] = []

    def push(self, task: Task) -> None:
        """入队，并记录一次操作。"""
        self._items.append(task)
        self.history.append(f"push:{task.name}")

    def pop(self) -> Task | None:
        """出队；空队列返回 ``None`` 而不是抛异常。"""
        if not self._items:
            return None
        task = self._items.pop()
        self.history.append(f"pop:{task.name}")
        return task

    def peek(self) -> Task | None:
        """看队首但不取出。"""
        return self._items[0] if self._items else None

    def drain(self) -> list[Task]:
        """取出全部任务，并且把队列清空。"""
        items = list(self._items)
        self._items.clear()
        return items

    def __len__(self) -> int:
        return len(self._items)
