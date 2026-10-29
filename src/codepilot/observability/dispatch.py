"""Task-local dispatch deadline; wall-clock recovery anchors once, waits are monotonic."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


class DispatchStoppedError(RuntimeError):
    """No operation dispatched: budget/deadline exhausted."""


@dataclass
class Deadline:
    expires: float
    clock: Callable[[], float] = time.monotonic

    def remaining(self, reserve: float = 0) -> float:
        return max(0.0, self.expires - self.clock() - reserve)

    def timeout(self, limit: float, reserve: float = 0) -> int:
        value = int(min(limit, self.remaining(reserve)))
        if value < 1:
            raise DispatchStoppedError("budget_time: deadline before dispatch")
        return value


@dataclass
class DispatchScope:
    deadline: Deadline
    reserve: float = 0
    cancel: Callable[[], bool] | None = None
    observe: Callable[..., Any] | None = None
    admit: Callable[[str], None] | None = None
    synchronous_worker: bool = False
    worker_task_id: int | None = None
    model_dispatches: int = 0


_current: ContextVar[DispatchScope | None] = ContextVar("dispatch_scope", default=None)


@contextmanager
def dispatch_scope(scope: DispatchScope) -> Iterator[None]:
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


def check_dispatch(resource: str = "operation") -> None:
    scope = _current.get()
    if scope is None:
        return
    if scope.cancel and scope.cancel():
        from codepilot.observability.resources import ResourceCancelledError

        raise ResourceCancelledError(f"cancelled before {resource} dispatch")
    scope.deadline.timeout(float("inf"), scope.reserve)
    if scope.admit:
        scope.admit(resource)


def dispatch_timeout(limit: float) -> int:
    check_dispatch()
    scope = _current.get()
    return scope.deadline.timeout(limit, scope.reserve) if scope else max(1, int(limit))


def observe_dispatch(event: str, **fields: Any) -> None:
    scope = _current.get()
    if scope and scope.observe:
        try:
            scope.observe(event, **fields)
        except Exception:
            from codepilot.observability.logging import get_logger

            get_logger(__name__).exception("dispatch.observer_unavailable")


def model_dispatch_count() -> int:
    scope = _current.get()
    return scope.model_dispatches if scope else 0


def mark_model_dispatch() -> None:
    scope = _current.get()
    if scope:
        scope.model_dispatches += 1


def deadline_diagnostics() -> dict[str, float | None]:
    scope = _current.get()
    return {
        "remaining_seconds": scope.deadline.remaining() if scope else None,
        "reserve_seconds": scope.reserve if scope else 0.0,
    }
