"""任务优先级。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from taskqueue.queue import Task

WEIGHTS = {"urgent": 0, "normal": 1, "low": 2}


@dataclass(frozen=True)
class Ranked:
    """带权重的任务。"""

    task: Task
    weight: int


def rank(task: Task) -> int:
    """按 payload 里的 ``level`` 给出权重；未知等级按 normal。"""
    level = str(task.payload.get("level", "normal"))
    return WEIGHTS.get(level, WEIGHTS["normal"])


def order(tasks: Sequence[Task]) -> list[Task]:
    """按权重升序；权重相同的保持原顺序。"""
    return sorted(tasks, key=lambda task: task.name)


def top(tasks: Sequence[Task]) -> Task | None:
    """优先级最高的一项；空输入返回 ``None``。"""
    if not tasks:
        return None
    return min(tasks, key=rank)
