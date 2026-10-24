"""Phase 21: the frozen batch budget must reach RepairState unchanged.

Phase 20 froze ``max_test_runs=18`` but the value stopped at the subprocess
boundary; the internal process used the default 12.  These tests pin the whole
typed chain: config -> CLI -> SmokeConfig -> RepairBudget -> report/source, and
the checkpoint refusal when a restored state carries a different budget.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import swebench_smoke as smoke  # noqa: E402

from codepilot.graph.checkpoint import checkpoint_budget_problem  # noqa: E402
from codepilot.schemas.budget import RepairBudget  # noqa: E402


def _config(**updates: object) -> smoke.SmokeConfig:
    return smoke.SmokeConfig(instance_id="owner__repo-1", **updates)


def test_explicit_eighteen_reaches_the_repair_budget(monkeypatch) -> None:
    monkeypatch.delenv("CODEPILOT_MAX_TEST_RUNS", raising=False)
    config = _config(
        max_iterations=5,
        max_test_runs=18,
        max_tool_calls_per_iteration=30,
        max_review_calls=2,
    )
    budget = smoke._build_budget(config)
    assert budget.max_test_runs == 18
    assert budget.max_iterations == 5
    assert budget.max_review_calls == 2
    assert config.limit_sources()["max_test_runs"] == "cli"
    assert config.config_payload()["limits"]["max_test_runs"] == 18
    smoke._validate_effective_budget(config, budget)


def test_default_and_environment_sources_are_explicit(monkeypatch) -> None:
    monkeypatch.delenv("CODEPILOT_MAX_TEST_RUNS", raising=False)
    default = _config()
    assert smoke._build_budget(default).max_test_runs == 12
    assert default.limit_sources()["max_test_runs"] == "default"

    monkeypatch.setenv("CODEPILOT_MAX_TEST_RUNS", "18")
    env_config = _config()
    assert smoke._build_budget(env_config).max_test_runs == 18
    assert env_config.limit_sources()["max_test_runs"] == "environment:CODEPILOT_MAX_TEST_RUNS"

    # CLI wins over the legacy environment override and the source says so.
    explicit = _config(max_test_runs=20)
    assert smoke._build_budget(explicit).max_test_runs == 20
    assert explicit.limit_sources()["max_test_runs"] == "cli"


def test_budget_mismatch_fails_before_any_dispatch(monkeypatch) -> None:
    monkeypatch.delenv("CODEPILOT_MAX_TEST_RUNS", raising=False)
    config = _config(max_test_runs=18)
    drifted = RepairBudget(max_test_runs=12)
    with pytest.raises(smoke.SmokeError, match="effective budget"):
        smoke._validate_effective_budget(config, drifted)


def test_config_hash_changes_with_the_typed_limits(monkeypatch) -> None:
    monkeypatch.delenv("CODEPILOT_MAX_TEST_RUNS", raising=False)
    first = _config(max_test_runs=18)
    second = _config(max_test_runs=19)
    assert first.config_hash() != second.config_hash()
    assert "max_test_runs" in smoke._budget_payload(first, smoke._build_budget(first))["source"]


def test_cli_parsing_forwards_every_limit(tmp_path: Path) -> None:
    runner = tmp_path / "runners.json"
    runner.write_text(
        '{"schema_version": 1, "instances": {}}',
        encoding="utf-8",
    )
    args = smoke._parse_args(
        [
            "--instance-id",
            "owner__repo-1",
            "--max-iterations",
            "5",
            "--max-test-runs",
            "18",
            "--max-tool-calls-per-iteration",
            "30",
            "--max-review-calls",
            "2",
            "--runner-overrides",
            str(runner),
        ]
    )
    config = smoke._config_from_args(args)
    assert config.max_test_runs == 18
    assert config.runner_overrides == runner
    assert smoke._build_budget(config).max_test_runs == 18


def test_checkpoint_budget_conflict_is_refused() -> None:
    stored = RepairBudget(max_test_runs=12)
    expected = RepairBudget(max_test_runs=18)
    problem = checkpoint_budget_problem(stored, expected)
    assert problem is not None and "max_test_runs" in problem
    assert checkpoint_budget_problem(stored, None) is None
    assert checkpoint_budget_problem(expected, expected) is None


def test_runner_identity_is_part_of_the_run_identity(tmp_path: Path) -> None:
    runner = tmp_path / "runners.json"
    runner.write_text(
        '{"schema_version": 1, "instances": {}}',
        encoding="utf-8",
    )
    with_runner = _config(runner_overrides=runner)
    without_runner = _config()
    assert with_runner.config_hash() != without_runner.config_hash()
    assert with_runner.config_payload()["runner_overrides_sha256"] is not None
