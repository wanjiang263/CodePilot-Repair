"""A-02/A-04/A-08: real M2 graph in the worker, SSE replay and recovery."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from typing import Any

from api_support import DEMO_ISSUE, HEADERS, KEY, sse_payloads
from fastapi.testclient import TestClient

from codepilot.api.database import ApiDatabase
from codepilot.api.worker import ApiWorker
from codepilot.config import Settings
from codepilot.schemas.budget import RepairBudget


def create_demo(client: TestClient, issue: str = DEMO_ISSUE) -> dict[str, Any]:
    response = client.post(
        "/api/v1/tasks",
        headers=HEADERS,
        json={"repository": {"kind": "demo", "value": "mini_calc"}, "issue": issue},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_worker_runs_the_real_m2_graph_end_to_end(
    client: TestClient, worker: ApiWorker, api_db: ApiDatabase, api_settings: Settings
) -> None:
    created = create_demo(client)
    task_id = created["task_id"]
    run_id = created["run_id"]

    claimed = worker.run_once()
    assert claimed is not None
    task = client.get(f"/api/v1/tasks/{task_id}", headers=HEADERS).json()
    assert task["status"] == "succeeded", task
    assert task["ui_status"] == "completed"
    assert task["verification_level"] == "m2_verified"
    assert task["patch_available"] is True
    assert task["report_available"] is True
    assert task["events_count"] > 20
    assert task["simulated_llm"] is True
    assert task["simulated_sandbox"] is True

    report = client.get(f"/api/v1/runs/{run_id}/report", headers=HEADERS)
    assert report.status_code == 200
    payload = report.json()
    assert payload["readonly"] is False
    assert payload["official"]["recorded"] is False
    assert payload["internal"]["verification_level"] == "m2_verified"
    assert payload["tests"]["target_tests_status"] == "PASS"
    assert payload["tests"]["regression"]["complete"] is True
    assert payload["review"]["executed"] is True
    assert payload["review"]["approved"] is True
    assert payload["summary"]["simulated_llm"] is True
    admission = payload["internal"]["closure"]["closure_admission"]
    if admission is not None:
        assert admission["remaining"] == max(admission["allowance"] - admission["runs_spent"], 0)

    patch = client.get(f"/api/v1/runs/{run_id}/patch", headers=HEADERS)
    assert patch.status_code == 200
    patch_payload = patch.json()
    assert patch_payload["files_count"] == 1
    assert patch_payload["files"][0]["path"] == "src/demo_calc/calc.py"
    assert patch_payload["identity_verified"] is True
    raw = patch_payload["diff"]
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == patch_payload["sha256"]

    run_dir = api_settings.artifacts_dir / "runs" / run_id
    assert (run_dir / "checkpoints.sqlite").is_file()
    assert (run_dir / "checkpoint_manifest.json").is_file()
    state = json.loads((run_dir / "state_final.json").read_text(encoding="utf-8"))
    assert (
        hashlib.sha256(raw.encode("utf-8")).hexdigest()
        == hashlib.sha256(state["current_patch"].encode("utf-8")).hexdigest()
    )

    events = api_db.events_after(run_id, 0)
    assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)
    assert len({event["seq"] for event in events}) == len(events)
    assert any(event["type"] == "node_start" for event in events)
    assert any(event["node"] == "plan" for event in events)
    assert any(event["node"] == "review" for event in events)
    assert events[-1]["type"] == "worker_finished"

    # No API key, absolute artifact path or provider credential may be persisted.
    for path in api_settings.artifacts_dir.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
    with api_db.connect() as connection:
        rows = connection.execute("SELECT payload_json FROM api_events").fetchall()
    assert all(KEY not in row[0] for row in rows)


def test_worker_identity_isolation_between_tasks(
    client: TestClient, worker: ApiWorker, api_db: ApiDatabase
) -> None:
    first = create_demo(client)
    second = create_demo(client, DEMO_ISSUE + " A second task.")
    assert first["task_id"] != second["task_id"]
    assert first["run_id"] != second["run_id"]
    assert worker.run_once() is not None
    assert worker.run_once() is not None
    first_events = api_db.events_after(first["run_id"], 0)
    second_events = api_db.events_after(second["run_id"], 0)
    assert first_events and second_events
    assert {event["run_id"] for event in first_events} == {first["run_id"]}
    assert {event["run_id"] for event in second_events} == {second["run_id"]}
    assert {event["task_id"] for event in first_events} == {first["task_id"]}
    assert {event["task_id"] for event in second_events} == {second["task_id"]}
    assert first_events[-1]["type"] == "worker_finished"
    assert second_events[-1]["type"] == "worker_finished"


def test_sse_after_seq_is_monotonic_and_does_not_duplicate(
    client: TestClient, worker: ApiWorker, api_db: ApiDatabase
) -> None:
    created = create_demo(client)
    worker.run_once()
    run_id = created["run_id"]
    all_events = api_db.events_after(run_id, 0)
    assert len(all_events) > 5
    cursor = all_events[4]["seq"]

    with client.stream(
        "GET",
        f"/api/v1/runs/{run_id}/events?after_seq={cursor}",
        headers=HEADERS,
    ) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    payloads = sse_payloads(text)
    repair_events = [payload for payload in payloads if payload.get("_event") == "repair"]
    assert repair_events
    seqs = [event["seq"] for event in repair_events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    assert min(seqs) > cursor
    assert seqs == [event["seq"] for event in all_events if event["seq"] > cursor]
    assert any(payload.get("type") == "stream_end" for payload in payloads)

    with client.stream(
        "GET",
        f"/api/v1/runs/{run_id}/events",
        headers={**HEADERS, "Last-Event-ID": str(cursor)},
    ) as response:
        text = "".join(response.iter_text())
    header_payloads = [
        payload for payload in sse_payloads(text) if payload.get("_event") == "repair"
    ]
    assert [event["seq"] for event in header_payloads] == seqs


def test_sse_streams_a_live_worker_until_the_terminal_event(
    client: TestClient, worker: ApiWorker
) -> None:
    created = create_demo(client)
    run_id = created["run_id"]
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    try:
        with client.stream("GET", f"/api/v1/runs/{run_id}/events", headers=HEADERS) as response:
            assert response.status_code == 200
            text = "".join(response.iter_text())
    finally:
        thread.join(timeout=60)
    assert not thread.is_alive()
    payloads = sse_payloads(text)
    repair = [payload for payload in payloads if payload.get("_event") == "repair"]
    assert repair
    assert any(payload.get("type") == "stream_end" for payload in payloads)
    terminal = next(payload for payload in payloads if payload.get("type") == "stream_end")
    assert terminal["ui_status"] in {"completed", "stopped", "failed", "cancelled", "error"}
    assert [event["seq"] for event in repair] == sorted(event["seq"] for event in repair)


def _init_db_task(
    db: ApiDatabase,
    *,
    task_id: str,
    run_id: str,
) -> dict[str, Any]:
    return db.create_task(
        task_id=task_id,
        run_id=run_id,
        repository_kind="demo",
        repository_value="mini_calc",
        repository_label="demo/mini_calc",
        base_commit="HEAD",
        issue=DEMO_ISSUE,
        mode="user",
        execution_mode="offline_fake",
        budget=RepairBudget().model_dump(mode="json"),
    )


def test_worker_recovery_distinguishes_terminal_resume_and_uncertain(
    api_db: ApiDatabase, api_settings: Settings
) -> None:
    terminal = _init_db_task(api_db, task_id="task_terminal", run_id="run_terminal")
    resume = _init_db_task(api_db, task_id="task_resume", run_id="run_resume")
    unknown = _init_db_task(api_db, task_id="task_unknown", run_id="run_unknown")
    for _ in range(3):
        assert api_db.claim_next(worker_id="worker_crash", lease_seconds=1) is not None

    terminal_dir = api_settings.artifacts_dir / "runs" / terminal.run_id
    terminal_dir.mkdir(parents=True)
    (terminal_dir / "state_final.json").write_text(
        json.dumps(
            {
                "run_id": terminal.run_id,
                "task_id": terminal.task_id,
                "status": "succeeded",
                "stop_reason": None,
                "usage": {},
            }
        ),
        encoding="utf-8",
    )
    resume_dir = api_settings.artifacts_dir / "runs" / resume.run_id
    (resume_dir / "workspace").mkdir(parents=True)
    (resume_dir / "checkpoints.sqlite").write_bytes(b"")

    with api_db.connect() as connection:
        connection.execute(
            "UPDATE api_tasks SET lease_expires_at = ? WHERE worker_id = 'worker_crash'",
            ("2000-01-01T00:00:00+00:00",),
        )
        connection.commit()
    actions = api_db.recover_stale(now=datetime.now(UTC))
    assert {item["outcome"] for item in actions} == {
        "recovered",
        "pending_resume",
        "uncertain",
    }

    recovered = api_db.get_by_run(terminal.run_id)
    assert recovered is not None
    assert recovered.status == "succeeded"
    assert recovered.recovery_state == "recovered_terminal_state"

    queued = api_db.get_by_run(resume.run_id)
    assert queued is not None
    assert queued.status == "queued"
    assert queued.recovery_state == "pending_resume"
    assert queued.resume_requested == 1

    uncertain = api_db.get_by_run(unknown.run_id)
    assert uncertain is not None
    assert uncertain.status == "uncertain"
    assert uncertain.ui_status == "uncertain"
    assert uncertain.stop_reason == "worker_lost"


def test_worker_marks_failed_and_uncertain_outcomes(api_db: ApiDatabase, worker: ApiWorker) -> None:
    failed_row = _init_db_task(api_db, task_id="task_failed", run_id="run_failed")
    assert api_db.claim_next(worker_id=worker.worker_id, lease_seconds=30) is not None
    worker._fail(failed_row, "worker_exception", "boom", uncertain=False)
    failed = api_db.get_by_run("run_failed")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.stop_reason == "workflow_error"
    assert failed.error and "boom" in failed.error

    unknown_row = _init_db_task(api_db, task_id="task_unknown_fail", run_id="run_unknown_fail")
    assert api_db.claim_next(worker_id=worker.worker_id, lease_seconds=30) is not None
    worker._fail(unknown_row, "worker_exception", "unsafe", uncertain=True)
    unknown = api_db.get_by_run("run_unknown_fail")
    assert unknown is not None
    assert unknown.status == "uncertain"
    assert unknown.stop_reason == "worker_lost"
    assert unknown.recovery_state == "result_unknown"


def test_worker_classifies_side_effects_as_uncertain(
    api_db: ApiDatabase, api_settings: Settings, worker: ApiWorker
) -> None:
    row = _init_db_task(api_db, task_id="task_crash", run_id="run_crash")
    claimed = api_db.claim_next(worker_id=worker.worker_id, lease_seconds=30)
    assert claimed is not None and claimed.run_id == row.run_id
    run_dir = api_settings.artifacts_dir / "runs" / row.run_id
    run_dir.mkdir(parents=True)
    uncertain, _detail = worker._classify_exception(row, RuntimeError("boom"))
    assert uncertain is False
    (run_dir / "checkpoints.sqlite").write_bytes(b"")
    uncertain, detail = worker._classify_exception(row, RuntimeError("boom"))
    assert uncertain is True
    assert "boom" in detail
