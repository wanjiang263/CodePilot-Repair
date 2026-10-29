"""Local-machine execution gates, reusing worker execution paths.

OS locks also coordinate worker processes. Lock files are retained, never used as
ownership/PID claims; the kernel releases ownership on process exit. No service,
model retry, download cancellation or budget change is introduced.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, BinaryIO

_guard = threading.Lock()
_locks: dict[str, tuple[Any, int]] = {}
_held: ContextVar[frozenset[tuple[int, int, str]]] = ContextVar(
    "resource_ownership", default=frozenset()
)


class ResourceCancelledError(RuntimeError):
    """Cancellation before dispatch; no external request or uncertain usage."""


@contextmanager
def resource_slot(
    resource: str,
    *,
    workspace: Path | None = None,
    cancel: Callable[[], bool] | None = None,
    observe: Callable[..., Any] | None = None,
) -> Iterator[None]:
    # Production graph I/O runs in a dedicated worker. A direct synchronous
    # caller on an event-loop thread may take an immediately free slot only;
    # it must offload rather than blocking that loop on contention.
    can_wait = True
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        from codepilot.observability.dispatch import _current

        scope = _current.get()
        can_wait = bool(
            scope
            and scope.synchronous_worker
            and scope.worker_task_id == id(asyncio.current_task())
        )
    from codepilot.observability.dispatch import check_dispatch

    check_dispatch(resource)
    key = "pressure" if resource in {"test", "heavy"} else resource
    if workspace is not None:
        key = "workspace:" + os.path.normcase(str(workspace.resolve()))
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    owner = (threading.get_ident(), id(task))
    held = _held.get()
    claim = (*owner, key)
    owned = {k for thread, task_id, k in held if (thread, task_id) == owner}
    if claim in held:
        yield
        return
    if (key == "pressure" and any(k.startswith("workspace:") for k in owned)) or (
        key.startswith("workspace:") and any(k.startswith("workspace:") and k > key for k in owned)
    ):
        raise RuntimeError("resource lock order: pressure before sorted workspace paths")
    with _guard:
        lock, users = _locks.get(key, (threading.Lock(), 0))
        _locks[key] = (lock, users + 1)
    started = time.monotonic()
    acquired = False
    execution_started = started
    handle: BinaryIO | None = None
    os_acquired = False
    dispatched = False
    try:
        if observe:
            observe("resource_wait", resource=resource)
        while not acquired:
            check_dispatch(resource)
            if cancel and cancel():
                raise ResourceCancelledError(f"cancelled waiting for {resource}; not dispatched")
            acquired = lock.acquire(timeout=0.1 if can_wait else 0)
            if not acquired and not can_wait:
                raise RuntimeError(
                    "contended synchronous resource gate must run in a worker thread"
                )
        if cancel and cancel():
            raise ResourceCancelledError(f"cancelled before {resource} dispatch")
        folder = Path(tempfile.gettempdir()) / "codepilot-resource-locks"
        folder.mkdir(parents=True, exist_ok=True)
        handle = (folder / (hashlib.sha256(key.encode()).hexdigest() + ".lock")).open("a+b")
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        while not os_acquired:
            check_dispatch(resource)
            if cancel and cancel():
                raise ResourceCancelledError(f"cancelled waiting for {resource}; not dispatched")
            os_acquired = _os_lock(handle, acquire=True)
            if not os_acquired:
                if not can_wait:
                    raise RuntimeError("contended OS resource gate must run in a worker thread")
                time.sleep(0.1)
        check_dispatch(resource)
        if cancel and cancel():
            raise ResourceCancelledError(f"cancelled before {resource} dispatch")
        execution_started = time.monotonic()
        dispatched = True
        _held.set(held | {claim})
        if observe:
            observe(
                "resource_acquired", resource=resource, queued_seconds=execution_started - started
            )
        yield
    finally:
        try:
            if handle is not None:
                handle.close()  # OS ownership is released even after a process exits.
        finally:
            if acquired:
                _held.set(held)
                lock.release()
            with _guard:
                _, users = _locks[key]
                if users == 1:
                    del _locks[key]
                else:
                    _locks[key] = (lock, users - 1)
            if observe and dispatched:
                observe(
                    "resource_released",
                    resource=resource,
                    execution_seconds=time.monotonic() - execution_started,
                )


def _os_lock(handle: BinaryIO, *, acquire: bool) -> bool:
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(
                handle.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN
            )
    except OSError as exc:
        if acquire and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


@contextmanager
def preparation_stage(
    stage: str, lifecycle: Any, cancel: Callable[[], bool] | None, *, gate: bool = True
) -> Iterator[None]:
    from datetime import UTC, datetime

    started = time.monotonic()
    runtime = {
        "stage": stage,
        "stage_started_at": datetime.now(UTC).isoformat(),
        "stage_elapsed_seconds": 0.0,
        "activity": "executing",
        "revision": 0,
    }
    lifecycle.emit("stage_start", runtime=runtime)
    try:
        if gate:
            with resource_slot("heavy", cancel=cancel, observe=lifecycle.emit):
                yield
        else:
            yield
    except Exception as exc:
        lifecycle.emit("preparation_error", stage=stage, error=str(exc))
        raise
    finally:
        lifecycle.emit(
            "stage_end",
            runtime={
                **runtime,
                "activity": "idle",
                "stage_elapsed_seconds": time.monotonic() - started,
            },
        )
