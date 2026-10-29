"""Phase 26 风险 B：线程与事件循环所有权的真实调用链验证。

这里验证的不是"某个身份函数对不对"，而是节点在工作线程里跑的那条链：

- 连续的多个节点在 ``to_thread`` 的一次性工作线程里读写同一个 SQLite 索引；
- 主循环写 NODE_START、工作线程写 NODE_END，事件序号必须连续且唯一；
- 两个独立任务各自持有自己的循环、引擎和事件文件，互不串号；
- 模型等待期间取消能收尾（不遗留线程），已发生的用量和原因留在状态里；
- 取消后的 checkpoint 能被读回，恢复不会把已完成的调用再打一遍。
"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from harness import FINISH, build_harness, passed_result

from codepilot.graph.checkpoint import (
    checkpoint_state,
    open_checkpointer,
    resume_run,
    run_with_checkpoint,
)
from codepilot.graph.routing import Node
from codepilot.observability.events import EventSink, EventType
from codepilot.schemas.enums import RunStatus, StopReason

FIX = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 1",
        "replace": "    return value",
    },
}
ROUND = [FIX, FINISH]


class _MemoryStore:
    """``EventStoreProtocol`` 的最小内存实现，用来暴露跨线程镜像的重复序号。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def next_seq(self, run_id: str) -> int:
        with self._lock:
            return len([row for row in self.rows if row["run_id"] == run_id]) + 1

    def append(
        self,
        *,
        run_id: str,
        node: str,
        type: str,
        payload: dict[str, Any],
        seq: int,
        timestamp: Any,
    ) -> None:
        with self._lock:
            self.rows.append(
                {"run_id": run_id, "node": node, "type": type, "payload": payload, "seq": seq}
            )


def _event_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_event_sink_keeps_one_sequence_across_threads(tmp_path: Path) -> None:
    store = _MemoryStore()
    path = tmp_path / "events.jsonl"
    sink = EventSink(run_id="r1", task_id="t", jsonl_path=path, store=store)

    start = threading.Barrier(8)

    def writer(worker: int) -> None:
        start.wait(10)
        for index in range(25):
            sink.emit(node=f"node-{worker}", type=EventType.NODE_START, status=str(index))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    sink.close()

    assert sink.last_seq == 200
    lines = _event_lines(path)
    assert [line["seq"] for line in lines] == list(range(1, 201))
    assert sorted(row["seq"] for row in store.rows) == list(range(1, 201))


def _run_with_checkpoint(tmp_path: Path, run_id: str) -> tuple[Any, Any]:
    harness = build_harness(tmp_path, results=[passed_result()], coder_rounds=[ROUND])
    path = tmp_path / "artifacts" / "runs" / run_id / "checkpoints.sqlite"

    async def go() -> Any:
        async with await open_checkpointer(path, run_id) as handle:
            return await run_with_checkpoint(harness.initial, harness.deps, handle)

    return harness, asyncio.run(go())


def test_consecutive_nodes_share_the_index_and_emit_ordered_events(tmp_path: Path) -> None:
    harness, state = _run_with_checkpoint(tmp_path, "phase26-ownership")

    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.model_dump_json(indent=2)
    lines = _event_lines(harness.events_path)
    assert [line["seq"] for line in lines] == list(range(1, len(lines) + 1))
    nodes = [line["node"] for line in lines if line["type"] == EventType.NODE_START.value]
    for expected in (
        Node.UNDERSTAND_ISSUE.value,
        Node.GENERATE_PATCH.value,
        Node.RUN_TARGET_TESTS.value,
        Node.REVIEW.value,
    ):
        assert expected in nodes, nodes
    # 索引由节点线程查询：跑完了才有补丁与目标测试结果。
    assert state.execution is not None and state.execution.status.value == "PASS"
    assert len(state.attempts) == 1


def test_two_independent_tasks_run_in_parallel_without_sharing_state(tmp_path: Path) -> None:
    left_dir = tmp_path / "left"
    right_dir = tmp_path / "right"
    left_dir.mkdir()
    right_dir.mkdir()

    def one(directory: Path) -> tuple[Any, Any]:
        harness = build_harness(directory, results=[passed_result()], coder_rounds=[ROUND])
        state = harness.run()
        return harness, state

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=180)
            for future in (
                pool.submit(one, left_dir),
                pool.submit(one, right_dir),
            )
        ]

    for harness, state in results:
        assert state.status is RunStatus.PROVISIONALLY_VERIFIED
        lines = _event_lines(harness.events_path)
        assert [line["seq"] for line in lines] == list(range(1, len(lines) + 1))
        assert len(harness.llm.calls) == len(results[0][0].llm.calls)
    assert results[0][0].events_path != results[1][0].events_path


def _gate_model(harness: Any, entered: threading.Event, release: threading.Event) -> None:
    original = harness.llm.complete

    def gated(request: Any) -> Any:
        entered.set()
        assert release.wait(30), "测试没有放行模型调用"
        return original(request)

    harness.llm.complete = gated


def test_cancel_during_a_model_wait_stops_at_the_node_boundary(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, results=[passed_result()], coder_rounds=[ROUND])
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    _gate_model(harness, entered, release)
    harness.deps.cancel_check = lambda: cancelled.is_set()

    holder: list[Any] = []

    def go() -> None:
        holder.append(harness.run())

    before = set(threading.enumerate())
    runner = threading.Thread(target=go, name="phase26-runner")
    runner.start()
    assert entered.wait(30), "模型调用没进入等待"
    cancelled.set()
    release.set()
    runner.join(60)
    assert not runner.is_alive(), "取消后运行线程没有收尾"

    state = holder[0]
    assert state.status is RunStatus.CANCELLED, state.model_dump_json(indent=2)
    assert state.stop_reason is StopReason.CANCELLED
    # 已经打出去的那次调用留在账上，不因为取消就抹掉。
    assert len(harness.llm.calls) >= 1
    assert state.usage is not None and state.usage.llm_calls >= 1
    assert any("cancel" in reason for reason in state.failure_reasons)

    extra = [t for t in threading.enumerate() if t not in before]
    assert extra == [], [t.name for t in extra]


def test_checkpoint_after_cancel_is_readable_and_resume_does_not_replay_calls(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path, results=[passed_result()], coder_rounds=[ROUND])
    run_id = harness.deps.run_id
    path = tmp_path / "artifacts" / "runs" / run_id / "checkpoints.sqlite"
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    _gate_model(harness, entered, release)
    harness.deps.cancel_check = lambda: cancelled.is_set()

    holder: list[Any] = []

    def go() -> None:
        async def inner() -> Any:
            async with await open_checkpointer(path, run_id) as handle:
                return await run_with_checkpoint(harness.initial, harness.deps, handle)

        holder.append(asyncio.run(inner()))

    runner = threading.Thread(target=go, name="phase26-cancel")
    runner.start()
    assert entered.wait(30)
    cancelled.set()
    release.set()
    runner.join(120)
    assert not runner.is_alive()

    state = holder[0]
    assert state.status is RunStatus.CANCELLED
    calls_before = len(harness.llm.calls)

    async def read_and_resume() -> Any:
        async with await open_checkpointer(path, run_id) as handle:
            stored = await checkpoint_state(handle.saver, run_id)
            resumed = await resume_run(harness.deps, handle, run_id)
            return stored, resumed

    stored, resumed = asyncio.run(read_and_resume())
    assert stored is not None
    assert stored.status is RunStatus.CANCELLED
    assert stored.stop_reason is StopReason.CANCELLED
    assert resumed.status is RunStatus.CANCELLED
    # 终态 run 不会被恢复重跑，已完成的外部调用不会再打一遍。
    assert len(harness.llm.calls) == calls_before
