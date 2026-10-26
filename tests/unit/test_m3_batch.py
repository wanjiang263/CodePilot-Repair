from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import m3_batch  # noqa: E402

from codepilot.graph.stop_rules import patch_hash  # noqa: E402
from codepilot.llm.dispatch_budget import DispatchBudget  # noqa: E402
from codepilot.llm.dispatch_recovery import (  # noqa: E402
    RecoveryLedgerSource,
    build_multi_source_recovery_manifest,
    write_recovery_manifest,
)

PATCH = (
    "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    "--- a/src/pkg/core.py\n"
    "+++ b/src/pkg/core.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def normalize(value: int) -> int:\n"
    "-    return value - 1\n"
    "+    return value\n"
)
IDS = [f"owner{index}__repo{index}-{index}" for index in range(6)]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _ledger(path: Path, *, cost: float = 0.01, unknown: bool = False) -> None:
    record = (
        {"status": "unknown_frozen", "reserved_cny": 0.8114688}
        if unknown
        else {"status": "settled", "cost_cny": cost}
    )
    _write_json(
        path,
        {"currency": "CNY", "frozen": unknown, "requests": [record]},
    )


def _budget() -> dict[str, Any]:
    return {
        "max_iterations": 5,
        "max_test_runs": 18,
        "max_tool_calls_per_iteration": 30,
        "max_review_calls": 2,
        "max_runtime_seconds": 1800,
        "source": {"max_test_runs": "cli"},
    }


def _report(root: Path, batch: str, instance: str, suffix: str = "") -> str:
    run_id = f"run-{instance}{suffix}"
    candidate = patch_hash(PATCH)
    raw = hashlib.sha256(PATCH.encode()).hexdigest()
    run_dir = root / "runs" / run_id
    report = {
        "run_id": run_id,
        "task_id": instance,
        "git_diff": PATCH,
        "patch_hash": raw,
        "reproduction": {"status": "reproduced"},
        "attempts": [
            {
                "iteration": 1,
                "patch_hash": candidate,
                "validation": {"passed": True},
                "target_test_source": "planner",
                "target_tests": ["tests/test_core.py::test_normalize"],
            }
        ],
        "review": {"approved": True, "reviewed_patch_hash": candidate, "issues": []},
        "budget": _budget(),
        "regression": {"mode": "full", "unexecuted_tests": []},
        "closure": {
            "patch_present": True,
            "candidate_patch_sha256": candidate,
            "validator_executed": True,
            "validator_passed": True,
            "target_tests_executed": True,
            "target_tests_status": "PASS",
            "target_test_sha256": candidate,
            "target_test_source": "planner",
            "target_tests": ["tests/test_core.py::test_normalize"],
            "regression_executed": True,
            "regression_complete": True,
            "regression_mode": "full",
            "regression_unexecuted_tests": 0,
            "regression_new_failures": 0,
            "review_executed": True,
            "review_approved": True,
            "review_high_issues": 0,
            "review_high_issues_known": True,
            "review_bound_to_candidate": True,
        },
    }
    _write_json(run_dir / "report.json", report)
    _write_json(run_dir / "review/1-model.json", {"request": PATCH, "patch": PATCH})
    prediction_path = root / "predictions" / f"{run_id}.jsonl"
    prediction_path.parent.mkdir(parents=True, exist_ok=True)
    prediction_path.write_text(
        json.dumps(
            {
                "instance_id": instance,
                "model_name_or_path": "glm-5.3-flash-main",
                "model_patch": PATCH,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    prediction_sha = hashlib.sha256(prediction_path.read_bytes()).hexdigest()
    root_report = {
        "run_id": run_id,
        "run_label": batch,
        "status": "SMOKE_COMPLETED",
        "runtime": {"model_identity": "glm-5.3-flash-main"},
        "inference": {
            "simulated_llm": False,
            "sandbox_backend": "docker",
            "final_patch_sha256": raw,
        },
        "prediction": {
            "path": str(prediction_path),
            "predictions_sha256": prediction_sha,
            "manifest": {
                "run_id": run_id,
                "model_name_or_path": "glm-5.3-flash-main",
                "predictions_sha256": prediction_sha,
                "entries": [{"instance_id": instance, "patch_sha256": raw}],
            },
        },
    }
    _write_json(root / "reports" / f"{run_id}.json", root_report)
    return run_id


def _no_patch(root: Path, batch: str, instance: str) -> None:
    run_id = f"run-{instance}"
    empty = patch_hash("")
    report = {
        "run_id": run_id,
        "task_id": instance,
        "git_diff": "",
        "patch_hash": "",
        "reproduction": {"status": "failed", "kind": "invalid_pass"},
        "attempts": [{"iteration": 1, "patch_hash": empty, "validation": {"passed": False}}],
        "review": None,
        "budget": _budget(),
        "regression": None,
        "closure": {
            "patch_present": False,
            "candidate_patch_sha256": empty,
            "validator_executed": True,
            "validator_passed": False,
            "target_tests_executed": False,
            "regression_executed": False,
            "regression_complete": False,
            "review_executed": False,
            "review_high_issues_known": False,
        },
    }
    _write_json(root / "runs" / run_id / "report.json", report)
    _write_json(
        root / "reports" / f"{run_id}.json",
        {
            "run_id": run_id,
            "run_label": batch,
            "inference": {
                "simulated_llm": False,
                "sandbox_backend": "docker",
                "final_patch_sha256": "",
            },
        },
    )


def _rejected(root: Path, batch: str, instance: str) -> None:
    run_id = _report(root, batch, instance)
    path = root / "runs" / run_id / "report.json"
    payload = json.loads(path.read_text())
    payload["review"]["approved"] = False
    payload["closure"]["review_approved"] = False
    _write_json(path, payload)


class Fixture:
    def __init__(self, root: Path, **config_updates: Any) -> None:
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "init",
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )
        self.root = root
        self.batch = "m3-test"
        source = root / "history/source.json"
        DispatchBudget(1, source)
        self.opening = root / "history/opening.json"
        write_recovery_manifest(
            build_multi_source_recovery_manifest(
                [RecoveryLedgerSource(source, "historical__instance-0")],
                run_id="history",
                reason="test",
                repo_root=root,
            ),
            self.opening,
        )
        self.catalog = root / "catalog.json"
        _write_json(
            self.catalog,
            {
                "batch_id": self.batch,
                "instances": [
                    {
                        "instance_id": instance,
                        "repo": f"owner{index}/repo{index}",
                        "base_commit": "a" * 40,
                    }
                    for index, instance in enumerate(IDS)
                ],
            },
        )
        config = {
            "batch_id": self.batch,
            "dataset_revision": "6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2",
            "selection_seed": 20261024,
            "run_seed": 0,
            "temperature": 0.0,
            "model": "glm-5.3-flash-main",
            "llm_config": str(ROOT / "configs/llm.yaml"),
            "opening_manifest": str(self.opening),
            "historical_opening_cny": 0.0,
            "batch_new_limit_cny": 30.0,
            "main_batch_limit_cny": 24.0,
            "per_instance_new_limit_cny": 4.0,
            "max_output_tokens": 4096,
            "max_iterations": 5,
            "max_test_runs": 18,
            "max_tool_calls_per_iteration": 30,
            "max_review_calls": 2,
            "timeout_seconds": 1800,
            "harness_python": "/unused/harness",
            "official_mode": "fake",
            **config_updates,
        }
        self.config = root / "config.json"
        _write_json(self.config, config)
        self.artifact = root / "artifacts/phase20" / self.batch

    def make(
        self,
        internal: Any = None,
        official: Any = None,
    ) -> m3_batch.M3Batch:
        return m3_batch.M3Batch(
            repo_root=self.root,
            catalog_path=self.catalog,
            config_path=self.config,
            artifact_root=self.artifact,
            live=True,
            internal_runner=internal,
            official_runner=official,
            validate_source=False,
        )


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_phase22_stop_assigns_all_terminal_states(fixture: Fixture) -> None:
    def internal(request: m3_batch.InternalRequest) -> int:
        _ledger(request.ledger, unknown=True)
        return 1

    summary = fixture.make(internal).run()
    assert summary["instances"][0]["status"] == "DISPATCHED_STOPPED"
    assert summary["instances"][0]["internal"] == "PROVIDER_USAGE_UNCERTAIN"
    for entry in summary["instances"][1:]:
        assert entry["status"] == "NOT_DISPATCHED"
        assert entry["stop_reason"] == "BLOCKED_BY_FROZEN_LEDGER"
    assert all(entry["terminal"] for entry in summary["instances"])


def test_phase22_empty_ledger_is_not_dispatched(fixture: Fixture) -> None:
    def internal(request: m3_batch.InternalRequest) -> int:
        DispatchBudget(4, request.ledger)
        return 1

    batch = fixture.make(internal)
    batch.run()
    registry = json.loads(batch.registry_path.read_text(encoding="utf-8"))
    assert registry["attempts"][0]["status"] == "not_dispatched"


def test_phase22_batch_lock_blocks_duplicate(fixture: Fixture) -> None:
    batch = fixture.make()
    (batch.artifact_root / "batch.lock").write_text('{"pid": 123}')
    with pytest.raises(m3_batch.BatchStopError, match="batch lock"):
        batch.run()


def test_phase22_existing_eval_is_preserved(fixture: Fixture) -> None:
    calls: list[str] = []
    batch = fixture.make(_success_internal(calls))
    sentinel = batch.artifact_root / "instances" / IDS[0] / "eval" / "sentinel"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("preserve")
    summary = batch.run()
    assert not calls
    assert "refusing overwrite" in summary["stop_reason"]
    assert sentinel.read_text() == "preserve"


def test_phase22_original_authorization_must_match_ledger(fixture: Fixture) -> None:
    config = json.loads(fixture.config.read_text(encoding="utf-8"))
    source = fixture.root / "history/source.json"
    config.update(
        original_authorization_cny=30,
        batch_new_limit_cny=29,
        prior_authorization_sources=[
            {
                "path": "history/source.json",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ],
    )
    fixture.config.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(m3_batch.BatchStopError, match="remaining original authorization"):
        fixture.make()


def test_phase22_repaired_segment_never_redispatches_paid_instance(fixture: Fixture) -> None:
    source = fixture.root / "history/source.json"
    _ledger(source)
    manifest = build_multi_source_recovery_manifest(
        [RecoveryLedgerSource(source, IDS[0])],
        run_id="segment2",
        reason="repair",
        repo_root=fixture.root,
    )
    fixture.opening.write_text(json.dumps(manifest), encoding="utf-8")
    config = json.loads(fixture.config.read_text(encoding="utf-8"))
    config.update(
        historical_opening_cny=0.01,
        completed_segment_sources={
            IDS[0]: {
                "path": "history/source.json",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        },
    )
    fixture.config.write_text(json.dumps(config), encoding="utf-8")
    calls: list[str] = []
    summary = fixture.make(_success_internal(calls), _success_official([])).run()
    assert calls == IDS[1:]
    assert summary["instances"][0]["status"] == "DISPATCHED_STOPPED"
    assert summary["instances"][0]["terminal"]


def test_phase22_normal_report_budget_is_read_from_identity_bound_wrapper(fixture: Fixture) -> None:
    def internal(request: m3_batch.InternalRequest) -> int:
        instance = request.instance["instance_id"]
        _ledger(request.ledger)
        run_id = _report(request.root, "m3-test", instance)
        leaf_path = request.root / "runs" / run_id / "report.json"
        leaf = json.loads(leaf_path.read_text())
        del leaf["budget"]
        _write_json(leaf_path, leaf)
        wrapper_path = request.root / "reports" / f"{run_id}.json"
        wrapper = json.loads(wrapper_path.read_text())
        wrapper["budget"] = _budget()
        _write_json(wrapper_path, wrapper)
        return 0

    summary = fixture.make(internal, _success_official([])).run()
    assert summary["status"] == "completed"
    assert all(row["internal"] == "ACCEPTED" for row in summary["instances"])


def _success_internal(calls: list[str]) -> Any:
    def run(request: m3_batch.InternalRequest) -> int:
        instance = request.instance["instance_id"]
        calls.append(instance)
        _ledger(request.ledger)
        _report(request.root, "m3-test", instance)
        return 0

    return run


def _success_official(calls: list[str], verdict: str = "RESOLVED") -> Any:
    def run(request: m3_batch.OfficialRequest) -> int:
        instance = request.instance["instance_id"]
        calls.append(instance)
        _write_json(
            request.root / "official-evaluation-m3-test.json",
            {"official_verdict": verdict},
        )
        return 0

    return run


def test_m3_success_and_cost_summary(fixture: Fixture) -> None:
    internal, official = [], []
    summary = fixture.make(_success_internal(internal), _success_official(official)).run()
    assert summary["status"] == "completed"
    assert internal == official == IDS
    assert summary["metrics"]["task_resolution_rate"] == 1.0
    assert summary["cost"]["new_settled_cny"] == pytest.approx(0.06)
    assert summary["cost"]["new_authorization_remaining_cny"] == pytest.approx(29.94)


def test_external_official_pauses_and_ingests_without_a_second_paid_attempt(
    fixture: Fixture,
) -> None:
    internal: list[str] = []
    official_calls: list[str] = []

    def official(request: Any) -> int:
        official_calls.append(request.instance["instance_id"])
        return 0

    # Switch the fixture config to external mode before construction.
    config = json.loads(fixture.config.read_text(encoding="utf-8"))
    config["official_mode"] = "external"
    fixture.config.write_text(json.dumps(config), encoding="utf-8")
    batch = fixture.make(_success_internal(internal), official)
    summary = batch.run()
    assert summary["status"] == "stopped"
    assert internal == [IDS[0]]
    assert official_calls == []
    entry = batch.state["instances"][0]
    assert entry["status"] == "OFFICIAL_PENDING"
    assert entry["terminal"] is False

    official_path = fixture.artifact / "instances" / IDS[0] / "official-evaluation-m3-test.json"
    _write_json(
        official_path,
        {"instance_id": IDS[0], "run_id": entry["run_id"], "official_verdict": "RESOLVED"},
    )
    ingested = batch.ingest_official()
    assert ingested["ingested"] == 1
    assert batch.state["instances"][0]["official"] == "RESOLVED"
    assert batch.state["instances"][0]["terminal"] is True

    # The next run continues with the next instance; the already-paid instance
    # is terminal and is never dispatched again.
    summary = batch.run()
    assert internal == [IDS[0], IDS[1]]
    assert summary["status"] == "stopped"


def test_ingest_official_refuses_a_missing_record(fixture: Fixture) -> None:
    config = json.loads(fixture.config.read_text(encoding="utf-8"))
    config["official_mode"] = "external"
    fixture.config.write_text(json.dumps(config), encoding="utf-8")
    batch = fixture.make(_success_internal([]), lambda _request: 0)
    batch.run()
    with pytest.raises(m3_batch.BatchStopError, match="official record is missing"):
        batch.ingest_official()


def test_phase22_missing_official_record_is_error_not_unresolved(fixture: Fixture) -> None:
    calls: list[str] = []
    summary = fixture.make(_success_internal(calls), lambda request: 1).run()
    assert calls == [IDS[0]]
    entry = summary["instances"][0]
    assert entry["internal"] == "ACCEPTED"
    assert entry["official"] == "ERROR"
    assert entry["status"] == "OFFICIAL_ERROR"
    assert entry["terminal"]


def test_phase22_external_invalid_environment_is_terminal(fixture: Fixture) -> None:
    config = json.loads(fixture.config.read_text(encoding="utf-8"))
    config["official_mode"] = "external"
    fixture.config.write_text(json.dumps(config), encoding="utf-8")
    calls: list[str] = []
    batch = fixture.make(_success_internal(calls))
    batch.run()
    _write_json(
        fixture.artifact / "instances" / IDS[0] / "official-evaluation-m3-test.json",
        {
            "status": "INVALID_ENV",
            "official_verdict": "NOT_EVALUATED",
        },
    )
    result = batch.ingest_official()
    assert result["summary"]["status"] == "stopped"
    assert batch.state["instances"][0]["official"] == "INVALID_ENV"
    assert batch.state["instances"][0]["terminal"]
    assert calls == [IDS[0]]


def test_repo_cache_seed_uses_the_loader_source_identity(tmp_path: Path) -> None:
    from codepilot.repo.workspace import WorkspaceManager

    manager = WorkspaceManager(cache_root=tmp_path / "cache")
    catalog_style = m3_batch.canonical_repo_source("django/django")
    loader_style = "https://github.com/django/django"
    assert manager.cache_path(catalog_style).name == manager.cache_path(loader_style).name, (
        "raw owner/repo must not resolve to a different seed cache key"
    )


def test_m3_default_dry_run_dispatches_nothing(fixture: Fixture) -> None:
    def forbidden(_request: Any) -> int:
        raise AssertionError("dry-run dispatched work")

    batch = m3_batch.M3Batch(
        repo_root=fixture.root,
        catalog_path=fixture.catalog,
        config_path=fixture.config,
        artifact_root=fixture.artifact,
        live=False,
        internal_runner=forbidden,
        official_runner=forbidden,
        validate_source=False,
    )
    summary = batch.run()
    assert summary["status"] == "dry_run"
    assert all(item["status"] == "DRY_RUN" for item in summary["instances"])


def test_m3_no_patch_skips_official_and_continues(fixture: Fixture) -> None:
    official = []

    def internal(request: m3_batch.InternalRequest) -> int:
        instance = request.instance["instance_id"]
        _ledger(request.ledger)
        if instance == IDS[0]:
            _no_patch(request.root, "m3-test", instance)
        else:
            _report(request.root, "m3-test", instance)
        return 0

    summary = fixture.make(internal, _success_official(official)).run()
    assert summary["instances"][0]["status"] == "NO_PATCH"
    assert summary["instances"][0]["official"] == "NOT_EVALUATED"
    assert official == IDS[1:]


def test_m3_internal_rejection_skips_official_and_continues(fixture: Fixture) -> None:
    official = []

    def internal(request: m3_batch.InternalRequest) -> int:
        instance = request.instance["instance_id"]
        _ledger(request.ledger)
        if instance == IDS[0]:
            _rejected(request.root, "m3-test", instance)
        else:
            _report(request.root, "m3-test", instance)
        return 0

    summary = fixture.make(internal, _success_official(official)).run()
    assert summary["instances"][0]["status"] == "REJECTED"
    assert official == IDS[1:]


def test_m3_environment_failure_stops_before_official(fixture: Fixture) -> None:
    calls, official = [], []

    def internal(request: m3_batch.InternalRequest) -> int:
        calls.append(request.instance["instance_id"])
        return 1

    summary = fixture.make(internal, _success_official(official)).run()
    assert summary["status"] == "stopped"
    assert calls == [IDS[0]]
    assert official == []
    assert summary["instances"][0]["status"] == "ENV_ERROR"


def test_m3_unknown_cost_is_recorded_and_stops_batch(fixture: Fixture) -> None:
    calls = []

    def internal(request: m3_batch.InternalRequest) -> int:
        calls.append(request.instance["instance_id"])
        _ledger(request.ledger, unknown=True)
        return 1

    summary = fixture.make(internal, _success_official([])).run()
    assert summary["status"] == "stopped"
    assert calls == [IDS[0]]
    assert summary["cost"]["new_unknown_reserved_cny"] == pytest.approx(0.8114688)
    assert summary["cost"]["new_settled_cny"] == 0
    registry = json.loads((fixture.artifact / "attempts.json").read_text())
    assert registry["attempts"][0]["unknown_reserved_cny"] == pytest.approx(0.8114688)


def test_m3_registry_failure_stops_before_acceptance_and_next_instance(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fail_record(*args: Any, **kwargs: Any) -> None:
        raise OSError("registry is read-only")

    monkeypatch.setattr(m3_batch, "record_attempt_from_ledger", fail_record)
    summary = fixture.make(_success_internal(calls), _success_official([])).run()
    assert summary["status"] == "stopped"
    assert calls == [IDS[0]]
    assert not (fixture.artifact / "instances" / IDS[0] / "acceptance-m3-test.json").exists()


def test_m3_run_id_ambiguity_stops_before_official(fixture: Fixture) -> None:
    official = []

    def internal(request: m3_batch.InternalRequest) -> int:
        instance = request.instance["instance_id"]
        _ledger(request.ledger)
        _report(request.root, "m3-test", instance)
        _report(request.root, "m3-test", instance, suffix="-other")
        return 0

    summary = fixture.make(internal, _success_official(official)).run()
    assert summary["status"] == "stopped"
    assert official == []
    assert "ambiguous" in summary["stop_reason"]


def test_m3_prediction_hash_mismatch_stops_before_official(fixture: Fixture) -> None:
    official = []

    def internal(request: m3_batch.InternalRequest) -> int:
        instance = request.instance["instance_id"]
        _ledger(request.ledger)
        run_id = _report(request.root, "m3-test", instance)
        path = request.root / "predictions" / f"{run_id}.jsonl"
        payload = json.loads(path.read_text())
        payload["model_patch"] += "\n# tampered\n"
        path.write_text(json.dumps(payload) + "\n")
        root_report_path = request.root / "reports" / f"{run_id}.json"
        root_report = json.loads(root_report_path.read_text())
        raw = hashlib.sha256(path.read_bytes()).hexdigest()
        root_report["prediction"]["predictions_sha256"] = raw
        root_report["prediction"]["manifest"]["predictions_sha256"] = raw
        root_report["prediction"]["manifest"]["entries"][0]["patch_sha256"] = hashlib.sha256(
            payload["model_patch"].encode()
        ).hexdigest()
        _write_json(root_report_path, root_report)
        return 0

    summary = fixture.make(internal, _success_official(official)).run()
    assert summary["status"] == "stopped"
    assert official == []
    assert "prediction patch bytes" in summary["stop_reason"]


def test_m3_duplicate_startup_does_not_dispatch_again(fixture: Fixture) -> None:
    fixture.make(_success_internal([]), _success_official([])).run()

    def forbidden(_request: Any) -> int:
        raise AssertionError("duplicate startup dispatched")

    summary = fixture.make(forbidden, forbidden).run()
    assert summary["status"] == "completed"
    assert all(item["terminal"] for item in summary["instances"])


def test_m3_budget_shortage_stops_before_internal(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, per_instance_new_limit_cny=0.01)
    calls = []
    summary = fixture.make(_success_internal(calls), _success_official([])).run()
    assert summary["status"] == "stopped"
    assert calls == []
    assert "reserve" in summary["stop_reason"]


def test_m3_pre_dispatch_interruption_resumes_without_reusing_ledger(fixture: Fixture) -> None:
    fixture.make(_success_internal([]), _success_official([]))
    empty = fixture.artifact / "instances" / IDS[0] / "dispatch-ledger-m3-test-r1.json"
    DispatchBudget(4, empty)
    resumes = []

    def internal(request: m3_batch.InternalRequest) -> int:
        resumes.append(request.resume)
        _ledger(request.ledger)
        _report(request.root, "m3-test", request.instance["instance_id"])
        return 0

    summary = fixture.make(internal, _success_official([])).run()
    assert summary["status"] == "completed"
    assert resumes == [True, False, False, False, False, False]
    assert (fixture.artifact / "instances" / IDS[0] / "dispatch-ledger-m3-test-r2.json").is_file()


def test_m3_post_dispatch_interruption_refuses_second_paid_attempt(fixture: Fixture) -> None:
    fixture.make(_success_internal([]), _success_official([]))
    _ledger(fixture.artifact / "instances" / IDS[0] / "dispatch-ledger-m3-test-r1.json")
    calls = []
    summary = fixture.make(_success_internal(calls), _success_official([])).run()
    assert summary["status"] == "stopped"
    assert calls == []
    assert "second paid attempt" in summary["stop_reason"]


def test_m3_all_unavailable_preflight_stops_without_dispatch(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path, require_preflight=True)
    calls = []
    batch = fixture.make(_success_internal(calls), _success_official([]))
    _write_json(
        fixture.artifact / "preflight/preflight.json",
        {
            "credential_reference_present": True,
            "instances": [{"instance_id": instance, "ok": False} for instance in IDS],
        },
    )
    summary = batch.run()
    assert summary["status"] == "stopped"
    assert calls == []
    assert all(item["status"] == "ENV_ERROR" for item in summary["instances"])
    assert summary["metrics"]["environment_invalid_rate"] == 1.0


def test_m3_cancellation_stops_before_next_instance(fixture: Fixture) -> None:
    calls = []

    def internal(request: m3_batch.InternalRequest) -> int:
        calls.append(request.instance["instance_id"])
        _ledger(request.ledger)
        raise KeyboardInterrupt

    summary = fixture.make(internal, _success_official([])).run()
    assert summary["status"] == "stopped"
    assert summary["stop_reason"].startswith("cancelled")
    assert calls == [IDS[0]]
