"""事件流（02 §18.2）。

一份事件同时写两个地方：

- ``events.jsonl``：流式追加，Dashboard 回放和事后排查直接读它，不需要数据库；
- ``events`` 表：支持 ``?after_seq=`` 这类按序查询和跨 run 统计。

冗余是刻意的。只写 JSONL 就没法查"所有 no_progress 的 run"；只写表就没法在
数据库损坏时保住现场。

``seq`` 由本类分配并保证每 run 单调递增。不要改成数据库自增主键：
事务回滚会跳号，而 SSE 断线续传是拿 seq 做严格比较的（``> after_seq``），
跳号会让客户端永远卡在一个等不到的序号上。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from pydantic import BaseModel, Field

from codepilot.clock import utcnow
from codepilot.observability.redaction import redact

__all__ = ["EventRecord", "EventSink", "EventStoreProtocol", "EventType", "EventUsage"]


class EventStoreProtocol(Protocol):
    """The two operations :class:`EventSink` needs from a durable event store.

    The API worker uses this seam to mirror real events into the queue database
    without importing SQLAlchemy models into the observability layer.
    """

    def next_seq(self, run_id: str) -> int: ...

    def append(
        self,
        *,
        run_id: str,
        node: str,
        type: str,
        payload: dict[str, Any] | None = None,
        seq: int | None = None,
        timestamp: datetime | None = None,
    ) -> object: ...


class EventType(StrEnum):
    """02 §18.2 列出的七种事件类型。"""

    NODE_START = "node_start"
    NODE_END = "node_end"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    SANDBOX_RUN = "sandbox_run"
    BUDGET_CHECK = "budget_check"
    STOP = "stop"
    LIFECYCLE = "lifecycle"


class EventUsage(BaseModel):
    """事件里的单次调用用量。注意 ``currency`` 随事件一起记录：

    不同 provider 的币种可能不同，事后回溯时若不知道当时的币种，
    费用数字就没有意义。

    ``cost_known`` 必须显式存在：``cost=0`` 既可能是"真的免费"，也可能是
    "这个模型没配单价"。前者是事实，后者是没测到——把它们写成同一个数，
    总账就会被当成确定值。
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = True
    currency: str = "CNY"


class EventRecord(BaseModel):
    """事件信封，字段与 02 §18.2 的 JSON 示例一一对应。"""

    run_id: str
    task_id: str
    seq: int
    node: str
    type: EventType
    timestamp: datetime = Field(default_factory=utcnow)

    duration_ms: int | None = None
    usage: EventUsage | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

    status: str | None = None
    error: str | None = None
    summary: str | None = None

    extra: dict[str, Any] = Field(default_factory=dict)
    """类型特有字段（例如 ``sandbox_run`` 的容器 id）。用 ``**kwargs`` 传入。"""


class EventSink:
    """按 run 写事件。

    既可以用作上下文管理器，也可以手动 ``close()``。无论哪条路径，写入前
    都会经过 ``redact()``：一次 ``--record`` 运行会把请求体和错误响应原样
    落到 artifacts，那里的 key 会跟着目录一起被人复制走。
    """

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        jsonl_path: Path,
        store: EventStoreProtocol | None = None,
        start_seq: int | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._run_id = run_id
        self._task_id = task_id
        self._path = jsonl_path
        self._store = store
        self._clock = clock
        # seq 分配、JSONL 追加、数据库镜像和 runtime 投影必须是一个原子动作：
        # 主循环写 NODE_START、节点工作线程写 NODE_END 和节点内 lifecycle 事件，
        # 两条线程同时进来会拿到重复 seq，或让 JSONL 的写顺序和 seq 顺序相反。
        self._lock = threading.RLock()
        self._closed = False
        self._runtime_projection: dict[str, Any] = {}

        # 中断恢复时新进程要接着已有事件的序号写，从 1 重新开始会直接撞上
        # events 表的 (run_id, seq) 唯一约束。默认值取自数据库而不是写死 0，
        # 因为"忘了传 start_seq"是个很容易犯、且只在恢复路径上才暴露的错误。
        if start_seq is None:
            start_seq = store.next_seq(run_id) - 1 if store is not None else 0
        self._seq = start_seq

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8", newline="\n")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def emit(
        self,
        *,
        node: str,
        type: EventType,
        status: str | None = None,
        duration_ms: int | None = None,
        usage: EventUsage | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        error: str | None = None,
        summary: str | None = None,
        **extra: Any,
    ) -> EventRecord:
        """写入一条事件并返回它。

        线程安全：``seq`` 分配、JSONL 追加、``events`` 表镜像和 runtime 投影在同一把
        锁里完成。跨线程调用（主循环的 NODE_START 与节点工作线程的 NODE_END）既不会
        拿到重复序号，也不会出现文件里后写的行序号反而更小。
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("EventSink 已关闭，不能再写事件")

            self._seq += 1
            record = EventRecord(
                run_id=self._run_id,
                task_id=self._task_id,
                seq=self._seq,
                node=node,
                type=type,
                timestamp=self._clock(),
                duration_ms=duration_ms,
                usage=usage,
                tool_calls=list(tool_calls or []),
                status=status,
                error=error,
                summary=summary,
                extra=extra,
            )

            if type is EventType.LIFECYCLE:
                from codepilot.observability.runtime import project_event

                self._runtime_projection = project_event(
                    self._runtime_projection, record.model_dump(mode="json")
                )
                record.extra["runtime_projection"] = dict(self._runtime_projection)
            payload = redact(record.model_dump(mode="json"))
            self._handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._handle.flush()

            if self._store is not None:
                self._store.append(
                    run_id=self._run_id,
                    node=node,
                    type=type.value,
                    payload=payload,
                    seq=self._seq,
                    timestamp=record.timestamp,
                )

            return record

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._handle.close()

    def __enter__(self) -> EventSink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
