"""Docker 沙箱的结构化阶段计时日志（Phase33 T02）。

Phase32 的最小 Docker e2e 310s 超时，日志只能定位到卡在 docker-py build 内部，
无法归因到具体阶段。本模块给沙箱各阶段挂上墙钟耗时与关键结果字段，经 structlog
输出::

    sandbox.docker.phase.done  phase=build_install_exec  duration_seconds=42.5  exit_code=0

用于把卡点精确到阶段粒度。日志写 stderr（未配置 logging 时由
codepilot.observability.logging 的 ConsoleRenderer 自动接管），
pytest -s 与 captured stderr 都能捕获。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from codepilot.observability.logging import get_logger
from codepilot.observability.redaction import redact_text

__all__ = ["timed_phase"]

_log = get_logger(__name__)


@contextmanager
def timed_phase(phase: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """为单个阶段计时并在退出时（含异常路径）输出结构化日志。

    yield 一个可变 dict，阶段体可以往里补写结果字段（exit_code、字节数等），
    退出时与构造参数合并进终态日志。异常原样上抛，只记账不吞。
    """
    extra: dict[str, Any] = {}
    started = time.perf_counter()
    error: str | None = None
    try:
        yield extra
    except BaseException as exc:  # 记录后原样重抛，不改变控制流
        error = f"{type(exc).__name__}: {redact_text(str(exc))[:400]}"
        raise
    finally:
        payload: dict[str, Any] = {
            "phase": phase,
            "duration_seconds": round(time.perf_counter() - started, 3),
            **fields,
            **extra,
        }
        if error is not None:
            payload["error"] = error
        _log.info("sandbox.docker.phase.done", **payload)
