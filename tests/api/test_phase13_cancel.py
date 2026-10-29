"""A-06: idempotent cancellation at queue and safe-node boundaries."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from api_support import DEMO_ISSUE, HEADERS
from fastapi.testclient import TestClient

from codepilot.api.database import ApiDatabase
from codepilot.api.demo import DeterministicSandbox
from codepilot.api.worker import ApiWorker
from codepilot.graph.budget import ClosureReservation
from codepilot.graph.builder import _guard
from codepilot.graph.routing import Node
from codepilot.observability.dispatch import Deadline
from codepilot.observability.events import EventSink
from codepilot.observability.lifecycle import Lifecycle
from codepilot.schemas.enums import RunStatus, StopReason
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage


def _guard_deps(
    sink: EventSink,
    *,
    cancel_check: Any,
    usage: Usage | None = None,
    test_runs: int = 0,
) -> Any:
    """_guard 现在需要完整的 Deps 派发面（lifecycle/deadline/budget 预留）。"""
    return SimpleNamespace(
        events=sink,
        bind_llm_context=lambda *_args: None,
        usage=usage if usage is not None else Usage(),
        test_runs=test_runs,
        cancel_check=cancel_check,
        lifecycle=Lifecycle(sink),
        dispatch_deadline=lambda state: Deadline(time.monotonic() + 120),
        budget=SimpleNamespace(
            closure_reservation=lambda *_a, **_k: ClosureReservation(
                tokens=0, cost=0.0, review_calls=0, estimated=True, basis="no-reviewer"
            ),
            check=lambda *_a, **_k: None,
        ),
        reviewer=None,
        llm_config=None,
        simulated_llm=False,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        harness_started=True,
    )


def create_demo(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": DEMO_ISSUE},
    )
    assert response.status_code == 201
    return response.json()


def test_queued_cancel_is_immediate_and_idempotent(client: TestClient, worker: ApiWorker) -> None:
    created = create_demo(client)
    run_id = created["run_id"]
    first = client.post(f"/api/v1/runs/{run_id}/cancel", headers=HEADERS)
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "cancelled"
    assert body["ui_status"] == "cancelled"
    assert body["already_finished"] is False
    task = client.get(f"/api/v1/tasks/{created['task_id']}", headers=HEADERS).json()
    assert task["verification_level"] == "cancelled"
    second = client.post(f"/api/v1/runs/{run_id}/cancel", headers=HEADERS)
    assert second.status_code == 200
    assert second.json()["already_finished"] is True
    assert second.json()["status"] == "cancelled"
    assert worker.run_once() is None

    missing = client.post("/api/v1/runs/run_missing/cancel", headers=HEADERS)
    assert missing.status_code == 404


def test_running_cancel_stops_before_agent_dispatch(
    client: TestClient, api_db: ApiDatabase, worker: ApiWorker
) -> None:
    created = create_demo(client)
    run_id = created["run_id"]
    claimed = api_db.claim_next(worker_id=worker.worker_id, lease_seconds=120)
    assert claimed is not None
    response = client.post(f"/api/v1/runs/{run_id}/cancel", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True
    assert response.json()["ui_status"] == "running"
    worker.execute(api_db.get_by_run(run_id))
    task = client.get(f"/api/v1/tasks/{created['task_id']}", headers=HEADERS).json()
    assert task["status"] == "cancelled"
    assert task["ui_status"] == "cancelled"
    assert task["stop_reason"] == "cancelled"
    assert task["verification_level"] == "cancelled"
    assert task["patch_available"] is False
    assert task["report_available"] is False
    events = api_db.events_after(run_id, 0)
    assert any(event["type"] == "cancel_requested" for event in events)
    assert any(event["type"] == "cancelled" for event in events)


def test_cancel_after_completion_does_not_rewrite_a_verified_result(
    client: TestClient, worker: ApiWorker
) -> None:
    created = create_demo(client)
    worker.run_once()
    before = client.get(f"/api/v1/tasks/{created['task_id']}", headers=HEADERS).json()
    assert before["status"] == "succeeded"
    response = client.post(f"/api/v1/runs/{created['run_id']}/cancel", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["already_finished"] is True
    after = client.get(f"/api/v1/tasks/{created['task_id']}", headers=HEADERS).json()
    assert after["status"] == "succeeded"
    assert after["verification_level"] == "m2_verified"
    assert after["patch_available"] is True


def test_cancel_one_task_does_not_touch_another(client: TestClient, api_db: ApiDatabase) -> None:
    first = create_demo(client)
    second = create_demo(client)
    response = client.post(f"/api/v1/runs/{first['run_id']}/cancel", headers=HEADERS)
    assert response.status_code == 200
    other = client.get(f"/api/v1/tasks/{second['task_id']}", headers=HEADERS).json()
    assert other["status"] == "queued"
    assert other["ui_status"] == "queued"
    assert other["cancel_requested"] is False


def test_cancellation_during_a_graph_run_finalizes_as_cancelled(
    client: TestClient, worker: ApiWorker, api_db: ApiDatabase, monkeypatch: Any
) -> None:
    original = DeterministicSandbox.run_tests

    def slow_run(self: DeterministicSandbox, **kwargs: Any) -> Any:
        time.sleep(0.15)
        return original(self, **kwargs)

    monkeypatch.setattr(DeterministicSandbox, "run_tests", slow_run)
    created = create_demo(client)
    run_id = created["run_id"]
    import threading

    thread = threading.Thread(target=worker.run_once)
    thread.start()
    cancelled = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and thread.is_alive():
        events = api_db.events_after(run_id, 0)
        if any(event["node"] == "localize" for event in events):
            response = client.post(f"/api/v1/runs/{run_id}/cancel", headers=HEADERS)
            assert response.status_code == 200
            cancelled = True
            break
        time.sleep(0.02)
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert cancelled, "the graph finished before the test could request cancellation"
    task = client.get(f"/api/v1/tasks/{created['task_id']}", headers=HEADERS).json()
    assert task["status"] == "cancelled", task
    assert task["ui_status"] == "cancelled"
    assert task["stop_reason"] == "cancelled"
    events = api_db.events_after(run_id, 0)
    assert any(event["type"] == "stop" and event["status"] == "cancelled" for event in events)
    assert any(event["type"] == "worker_finished" for event in events)
    report = client.get(f"/api/v1/runs/{run_id}/report", headers=HEADERS)
    assert report.status_code == 200
    assert report.json()["status"] == "cancelled"
    assert report.json()["verification_level"] == "cancelled"


def test_guard_cancels_at_a_node_boundary_and_keeps_partial_updates(tmp_path: Path) -> None:
    sink = EventSink(
        run_id="run_guard",
        task_id="task_guard",
        jsonl_path=tmp_path / "events.jsonl",
    )
    flag = [False]
    seen: list[str] = []

    async def node(state: RepairState, deps: Any) -> dict[str, Any]:
        seen.append(state.run_id)
        flag[0] = True
        return {"test_runs": 4, "usage": deps.usage}

    deps = _guard_deps(sink, cancel_check=lambda: flag[0], test_runs=3)
    state = RepairState(
        task_id="task_guard",
        run_id="run_guard",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    update = asyncio.run(_guard(Node.PLAN, node, deps)(state))  # type: ignore[arg-type]
    sink.close()
    assert seen == ["run_guard"]
    assert update["status"] is RunStatus.CANCELLED
    assert update["stop_reason"] is StopReason.CANCELLED
    assert update["test_runs"] == 4
    assert any(
        "cancelled at a safe node boundary" in reason for reason in update["failure_reasons"]
    )
    assert update["events"][-1]["status"] == "cancelled"


def test_guard_checks_cancellation_before_dispatching(tmp_path: Path) -> None:
    sink = EventSink(
        run_id="run_guard_before",
        task_id="task_guard_before",
        jsonl_path=tmp_path / "events.jsonl",
    )
    called = False

    async def node(state: RepairState, deps: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    deps = _guard_deps(sink, cancel_check=lambda: True)
    state = RepairState(
        task_id="task_guard_before",
        run_id="run_guard_before",
        repository_id="repo",
        base_commit="abc",
        issue="x",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    update = asyncio.run(_guard(Node.RETRIEVE, node, deps)(state))  # type: ignore[arg-type]
    sink.close()
    assert called is False
    assert update["status"] is RunStatus.CANCELLED
    assert update["stop_reason"] is StopReason.CANCELLED
