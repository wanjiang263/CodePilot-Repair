"""F-10 的证明：seq 单调；JSONL 与表内容一致；含 key 的 payload 落盘后被脱敏。

"github 与 DB 两条路都要写"这一条在这里被反复验证：只查数据库会漏掉 JSONL
根本没写的情况，而 Dashboard 回放读的正是 JSONL。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codepilot.observability.events import EventSink, EventType, EventUsage
from codepilot.observability.redaction import REDACTED
from codepilot.storage.repository import Stores


@pytest.fixture
def sink(stores: Stores, tmp_path: Path) -> Iterator[EventSink]:
    stores.repositories.upsert(repository_id="r1", url="u", base_commit="abc")
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")

    event_sink = EventSink(
        run_id="run1",
        task_id="t1",
        jsonl_path=tmp_path / "events.jsonl",
        store=stores.events,
    )
    yield event_sink
    event_sink.close()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_seq_starts_at_one_and_is_monotonic(sink: EventSink) -> None:
    seqs = [
        sink.emit(node="retrieve", type=EventType.NODE_START).seq,
        sink.emit(node="retrieve", type=EventType.NODE_END).seq,
        sink.emit(node="plan", type=EventType.NODE_START).seq,
    ]
    assert seqs == [1, 2, 3]
    assert sink.last_seq == 3


def test_jsonl_and_table_agree(sink: EventSink, stores: Stores) -> None:
    """两条写入路径的内容必须一致——不一致时排查会得到互相矛盾的证据。"""
    sink.emit(node="diagnose", type=EventType.NODE_END, summary="AttributeError")

    from_file = _read_jsonl(sink.path)
    from_db = stores.events.list_after("run1")

    assert len(from_file) == len(from_db) == 1
    assert from_file[0]["seq"] == from_db[0].seq
    assert from_file[0]["node"] == from_db[0].node == "diagnose"
    assert from_db[0].payload["summary"] == "AttributeError"


def test_event_envelope_matches_design_doc(sink: EventSink) -> None:
    """字段名对齐 02 §18.2 的 JSON 示例。"""
    sink.emit(
        node="diagnose",
        type=EventType.NODE_END,
        duration_ms=1820,
        usage=EventUsage(model="deepseek-chat", input_tokens=9200, output_tokens=610, cost=0.021),
        status="ok",
        summary="AttributeError in auth/service.py:42, strategy=refine",
    )
    record = _read_jsonl(sink.path)[0]
    assert set(record) >= {
        "run_id",
        "task_id",
        "seq",
        "node",
        "type",
        "timestamp",
        "duration_ms",
        "usage",
        "tool_calls",
        "status",
        "error",
        "summary",
    }
    assert record["run_id"] == "run1"
    assert record["task_id"] == "t1"
    assert record["type"] == "node_end"
    assert record["usage"]["input_tokens"] == 9200  # type: ignore[index]
    assert record["error"] is None


def test_secrets_in_payload_are_redacted_before_persisting(sink: EventSink, stores: Stores) -> None:
    """LLM 调用的错误体里经常原样带回请求头。"""
    sink.emit(
        node="plan",
        type=EventType.LLM_CALL,
        error="401 from provider: {'Authorization': 'Bearer sk-abcdefghijklmn'}",
        api_key="sk-abcdefghijklmn",
    )

    raw = sink.path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmn" not in raw
    assert REDACTED in raw

    # 数据库里也不能留明文。
    from_db = stores.events.list_after("run1")
    assert "sk-abcdefghijklmn" not in json.dumps(from_db[0].payload)


def test_tool_calls_are_recorded_as_a_list(sink: EventSink) -> None:
    sink.emit(
        node="generate_patch",
        type=EventType.TOOL_CALL,
        tool_calls=[{"name": "edit_file", "args": {"path": "src/a.py"}}],
    )
    record = _read_jsonl(sink.path)[0]
    assert record["tool_calls"] == [{"name": "edit_file", "args": {"path": "src/a.py"}}]


def test_extra_kwargs_land_in_extra(sink: EventSink) -> None:
    """``sandbox_run`` 需要带上容器 id，它不属于信封的固定字段。"""
    sink.emit(
        node="run_target_tests",
        type=EventType.SANDBOX_RUN,
        container_id="c0ffee",
        exit_code=1,
    )
    record = _read_jsonl(sink.path)[0]
    assert record["extra"] == {"container_id": "c0ffee", "exit_code": 1}


def test_all_seven_event_types_are_writable(sink: EventSink) -> None:
    for event_type in EventType:
        sink.emit(node="n", type=event_type)
    assert sink.last_seq == len(list(EventType)) == 7


def test_jsonl_is_append_mode_across_sinks(stores: Stores, tmp_path: Path) -> None:
    """进程被杀后重启会新建 sink；它必须接着写，而不是把前面的事件清空。"""
    stores.repositories.upsert(repository_id="r1", url="u", base_commit="abc")
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")
    path = tmp_path / "events.jsonl"

    with EventSink(run_id="run1", task_id="t1", jsonl_path=path, store=stores.events) as s:
        s.emit(node="a", type=EventType.NODE_START)
    with EventSink(run_id="run1", task_id="t1", jsonl_path=path, store=stores.events) as s:
        s.emit(node="b", type=EventType.NODE_START)

    assert [r["node"] for r in _read_jsonl(path)] == ["a", "b"]


def test_second_sink_continues_seq_from_database(stores: Stores, tmp_path: Path) -> None:
    """重启后 seq 必须接着表里的最大值，否则唯一约束会直接报错。"""
    stores.repositories.upsert(repository_id="r1", url="u", base_commit="abc")
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")
    path = tmp_path / "events.jsonl"

    with EventSink(run_id="run1", task_id="t1", jsonl_path=path, store=stores.events) as s:
        s.emit(node="a", type=EventType.NODE_START)
        s.emit(node="a", type=EventType.NODE_END)

    # 不传 start_seq：有 store 时序号必须自动接上，否则恢复路径必崩。
    resumed = EventSink(run_id="run1", task_id="t1", jsonl_path=path, store=stores.events)
    try:
        assert resumed.emit(node="b", type=EventType.NODE_START).seq == 3
    finally:
        resumed.close()


def test_emit_after_close_raises(stores: Stores, tmp_path: Path) -> None:
    stores.repositories.upsert(repository_id="r1", url="u", base_commit="abc")
    stores.tasks.upsert(task_id="t1", repository_id="r1", issue="x", mode="user")
    stores.runs.create(run_id="run1", task_id="t1")
    sink = EventSink(
        run_id="run1", task_id="t1", jsonl_path=tmp_path / "e.jsonl", store=stores.events
    )
    sink.close()
    with pytest.raises(RuntimeError):
        sink.emit(node="n", type=EventType.NODE_START)


def test_sink_without_store_writes_jsonl_only(tmp_path: Path) -> None:
    """脚本场景（无数据库）仍然要能出事件流。"""
    sink = EventSink(run_id="run1", task_id="t1", jsonl_path=tmp_path / "e.jsonl")
    try:
        sink.emit(node="n", type=EventType.NODE_START)
        assert len(_read_jsonl(sink.path)) == 1
    finally:
        sink.close()


def test_jsonl_creates_parent_directory(tmp_path: Path) -> None:
    sink = EventSink(
        run_id="run1", task_id="t1", jsonl_path=tmp_path / "deep" / "nested" / "e.jsonl"
    )
    try:
        sink.emit(node="n", type=EventType.NODE_START)
    finally:
        sink.close()
    assert sink.path.exists()
