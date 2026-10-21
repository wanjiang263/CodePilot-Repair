"""Fixtures for the Phase 13 API, worker and replay tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from api_support import make_settings
from fastapi.testclient import TestClient

from codepilot.api.app import create_app
from codepilot.api.database import ApiDatabase
from codepilot.api.worker import ApiWorker
from codepilot.config import Settings


@pytest.fixture
def api_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def api_db(api_settings: Settings) -> ApiDatabase:
    return ApiDatabase(
        api_settings.resolved_api_queue_path, artifacts_dir=api_settings.artifacts_dir
    )


@pytest.fixture
def client(api_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(api_settings)) as test_client:
        yield test_client


@pytest.fixture
def worker(api_settings: Settings, api_db: ApiDatabase) -> ApiWorker:
    return ApiWorker(settings=api_settings, database=api_db)


@pytest.fixture
def history_package(tmp_path: Path) -> dict[str, object]:
    """A deterministic archived run with a real official/internal discrepancy."""
    package = tmp_path / "history-pkg"
    run_dir = package / "run-artifacts"
    run_dir.mkdir(parents=True)
    run_id = "history-run-001"
    task_id = "instance-001"
    patch = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b - 1\n"
        "+    return a + b\n"
    )
    patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    state = {
        "run_id": run_id,
        "task_id": task_id,
        "status": "budget_exhausted",
        "stop_reason": "budget_test_runs",
        "current_patch": patch,
        "base_commit": "deadbeef",
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 80,
            "cached_input_tokens": 300,
            "cost": 0.0123,
            "currency": "CNY",
            "llm_calls": 6,
            "cost_unknown_calls": 0,
        },
        "budget": {
            "max_iterations": 5,
            "max_test_runs": 12,
            "max_cost": 1.0,
            "budget_currency": "CNY",
        },
        "failure_reasons": [],
    }
    closure = {
        "patch_present": True,
        "candidate_patch_sha256": "f" * 64,
        "frozen_by_coding_stage": True,
        "coding_stop_reason": None,
        "validator_executed": True,
        "validator_passed": True,
        "target_tests_executed": True,
        "target_tests_status": "PASS",
        "target_test_sha256": "e" * 64,
        "regression_executed": True,
        "regression_complete": False,
        "regression_levels": ["L1_target", "L2_impact", "L3_full"],
        "regression_new_failures": 0,
        "review_executed": False,
        "review_approved": None,
        "review_high_issues": 0,
        "review_bound_to_candidate": False,
        "post_review_repair": False,
        "review_calls": 0,
        "unexecuted_stages": ["regression_incomplete", "review:no result"],
        "budget_usage": {
            "iterations": "1/5",
            "test_runs": "12/12",
            "tool_calls": "8/30",
            "review_calls": "0/2",
            "tokens": "1280/1000000",
            "cost": "0.0123/1",
            "time": "42/1800",
        },
        "cost_hard_cap": 1.0,
        "cost_hard_cap_hit": False,
    }
    report = {
        "run_id": run_id,
        "task_id": task_id,
        "status": "budget_exhausted",
        "stop_reason": "budget_test_runs",
        "verification_level": "budget_exhausted",
        "repository": "https://github.com/example/calc",
        "base_commit": "deadbeef",
        "issue_summary": "add() is off by one",
        "usage": state["usage"],
        "test_runs": 12,
        "simulated_llm": False,
        "simulated_sandbox": False,
        "closure": closure,
        "attempts": [
            {
                "iteration": 1,
                "patch_hash": "e" * 64,
                "patch_diff": patch,
                "validation": {"passed": True},
                "execution": {"status": "PASS", "tests_failed": 0},
                "failure_signatures": [],
                "review": {"approved": True},
                "diagnosis": None,
            }
        ],
        "tests": {},
        "review": None,
        "regression": {"complete": False, "levels_run": ["L1_target", "L2_impact"]},
        "failure_reasons": [],
        "localization": {"files": [{"path": "src/calc.py"}]},
        "repair_plan": {"root_cause_hypothesis": "off by one"},
        "test_execution": {"status": "PASS", "tests_failed": 0, "tests_passed": 1},
        "localization_files": ["src/calc.py"],
        "git_diff": patch,
    }
    official = {
        "run_id": run_id,
        "status": "SMOKE_COMPLETED_RESOLVED",
        "prediction": {"patch_sha256": patch_hash},
        "harness": {
            "decision": "EXECUTED",
            "exit_code": 0,
            "verdicts": [{"verdict": "RESOLVED", "resolved": True}],
        },
        "m2_vs_official": {
            "official": "RESOLVED",
            "internal_m2": "M2_DOES_NOT_CLAIM_SUCCESS",
            "official_authoritative": True,
            "note": "Official and internal are independent.",
        },
    }
    events = [
        {
            "run_id": run_id,
            "task_id": task_id,
            "seq": seq,
            "node": node,
            "type": event_type,
            "timestamp": f"2026-10-20T13:31:5{seq}Z",
            "status": status,
            "summary": summary,
        }
        for seq, node, event_type, status, summary in (
            (1, "understand_issue", "node_start", "started", None),
            (2, "understand_issue", "node_end", "ok", "issue analyzed"),
            (3, "finalize", "node_end", "budget_test_runs", "stopped on test budget"),
        )
    ]
    (run_dir / "state_final.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    (run_dir / "report.md").write_text("# archived report\n", encoding="utf-8")
    (run_dir / "patch_final.diff").write_text(patch, encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    reports = run_dir / "reports"
    reports.mkdir()
    (reports / f"{run_id}.json").write_text(
        json.dumps(official, ensure_ascii=False), encoding="utf-8"
    )
    catalog = tmp_path / "history.yaml"
    catalog.write_text(
        yaml.safe_dump(
            {
                "benchmarks": [
                    {
                        "id": "history-fixture",
                        "label": "History fixture",
                        "package": str(package),
                        "run_artifacts": "run-artifacts",
                        "instance_id": task_id,
                        "repository": "https://github.com/example/calc",
                        "model": "fixture-model",
                        "recorded_at": "2026-10-20T13:31:52Z",
                        "note": "read-only fixture",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return {
        "package": package,
        "run_dir": run_dir,
        "catalog": catalog,
        "run_id": run_id,
        "patch": patch,
        "patch_hash": patch_hash,
    }
