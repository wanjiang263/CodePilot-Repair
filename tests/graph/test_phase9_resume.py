import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).with_name("_phase9_resume_worker.py")


@pytest.mark.parametrize(
    "boundary", ["after-regression", "after-review", "in-flight", "after-artifact"]
)
def test_process_resume(tmp_path, boundary):
    crashed = subprocess.run(
        [sys.executable, str(WORKER), boundary, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert crashed.returncode == 7, crashed.stdout + crashed.stderr
    resumed = subprocess.run(
        [sys.executable, str(WORKER), "resume", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    state = json.loads((tmp_path / "resumed.json").read_text())
    if boundary in {"in-flight", "after-artifact"}:
        assert state["status"] == "error"
        assert "resume refused" in str(state["failure_reasons"])
    else:
        assert state["status"] == "succeeded", state["failure_reasons"]
        assert state["test_runs"] == 5
        assert len(state["attempts"]) == 1
        assert state["regression"]["complete"]
        if boundary == "after-review":
            usage = json.loads((tmp_path / "usage-before-crash.json").read_text())
            assert state["usage"]["llm_calls"] == usage["calls"]
            assert state["test_runs"] == usage["tests"]


def test_corrupt_evidence_refuses_process_resume(tmp_path):
    crashed = subprocess.run(
        [sys.executable, str(WORKER), "after-regression", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert crashed.returncode == 7, crashed.stderr
    journal = tmp_path / "artifacts/runs/phase8-run/operations.sqlite"
    with sqlite3.connect(journal) as db:
        db.execute("UPDATE operations SET hash='corrupt'")
    resumed = subprocess.run(
        [sys.executable, str(WORKER), "resume", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert resumed.returncode == 0, resumed.stderr
    state = json.loads((tmp_path / "resumed.json").read_text())
    assert state["status"] == "error"
    assert state["review"] is None
