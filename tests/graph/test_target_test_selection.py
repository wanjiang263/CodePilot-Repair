"""P2: deterministic target-test authorization and failure attribution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from harness import (
    FINISH,
    TARGET,
    build_harness,
    edit,
    passed_result,
)

from codepilot.agents.reviewer import PatchReviewer
from codepilot.errors import ErrorOrigin, FailureClass, classify_exception
from codepilot.graph import nodes
from codepilot.graph.regression import RegressionRunner
from codepilot.graph.routing import Node, route_after_target_tests
from codepilot.graph.stop_rules import patch_hash
from codepilot.llm.dispatch_budget import DispatchBudgetStoppedError
from codepilot.llm.fake import FakeLLM
from codepilot.sandbox.fake import scripted_result
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason, TestOutcome
from codepilot.schemas.execution import TestBaseline
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.state import Attempt
from codepilot.schemas.validation import ValidationResult

PATCH = (
    "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    "--- a/src/pkg/core.py\n"
    "+++ b/src/pkg/core.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def normalize(value: int) -> int:\n"
    "-    return value - 1\n"
    "+    return value\n"
)
ROUND = [edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH]


def _state(
    harness: Any,
    *,
    tests_to_run: list[str],
    baseline: TestBaseline | None,
) -> Any:
    attempt = Attempt(
        iteration=1,
        patch_diff=PATCH,
        patch_hash=patch_hash(PATCH),
        validation=ValidationResult(passed=True),
    )
    return harness.initial.model_copy(
        update={
            "current_patch": PATCH,
            "attempts": [attempt],
            "plan": RepairPlan(
                root_cause_hypothesis="normalize is off by one",
                evidence=["src/pkg/core.py"],
                changes=[
                    {
                        "file": "src/pkg/core.py",
                        "symbol": "pkg.core.normalize",
                        "action": "modify",
                        "description": "return the input unchanged",
                    }
                ],
                tests_to_run=tests_to_run,
                risk="low",
                alternatives=[],
            ),
            "baseline": baseline,
            "impact": ImpactAnalysis(),
            "status": RunStatus.RUNNING,
        }
    )


def _baseline(harness: Any, outcomes: dict[str, TestOutcome]) -> TestBaseline:
    return TestBaseline(base_commit=harness.base_commit, outcomes=outcomes)


def _report(harness: Any, state: Any) -> dict[str, Any]:
    path = harness.deps.artifacts.run_dir / state.report_artifacts["report_json"]
    return json.loads(path.read_text(encoding="utf-8"))


def test_empty_planner_falls_back_to_impact_or_related_and_reaches_review(
    tmp_path: Path,
) -> None:
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[ROUND],
        tests=(),
    )
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(
        FakeLLM([{"approved": True, "summary": "target, regression and review ran"}])
    )
    try:
        state = harness.run()
    finally:
        harness.close()

    assert state.status is RunStatus.SUCCEEDED
    assert state.current_attempt is not None
    assert state.current_attempt.target_test_source in {"impact", "baseline", "related"}
    assert state.current_attempt.target_tests
    assert state.execution is not None and state.execution.status is ExecutionStatus.PASS
    assert state.regression is not None and state.regression.complete
    assert state.review is not None and state.review.approved
    report = _report(harness, state)
    assert report["closure"]["target_test_source"] == state.current_attempt.target_test_source
    assert report["closure"]["target_tests"] == state.current_attempt.target_tests


def test_no_legal_target_is_target_test_unavailable_not_workflow_error(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path, results=[], coder_rounds=[ROUND], baseline_results=[])
    state = _state(harness, tests_to_run=[], baseline=_baseline(harness, {}))
    try:
        update = asyncio.run(nodes.run_target_tests(state, harness.deps))
    finally:
        harness.close()

    assert update["status"] is RunStatus.FAILED
    assert update["stop_reason"] is StopReason.TESTS_FAILED
    assert update["error_class"] == FailureClass.TARGET_TEST_UNAVAILABLE.value
    assert update["error_origin"] == ErrorOrigin.MODEL.value
    attempt = update["attempts"][-1]
    assert attempt.target_test_source == "none"
    assert attempt.target_tests == []
    assert "regression" not in update and "review" not in update
    updated = state.model_copy(update=update)
    assert route_after_target_tests(updated) is Node.FINALIZE


def test_collection_error_from_missing_third_party_is_infrastructure(
    tmp_path: Path,
) -> None:
    collection = scripted_result(ExecutionStatus.COLLECTION_ERROR).model_copy(
        update={
            "stdout_tail": (
                "ERROR collecting tests/test_core.py\nModuleNotFoundError: No module named 'tables'"
            )
        }
    )
    harness = build_harness(
        tmp_path, results=[collection], coder_rounds=[ROUND], baseline_results=[]
    )
    state = _state(
        harness,
        tests_to_run=[TARGET],
        baseline=_baseline(harness, {f"{TARGET}::test_normalize": TestOutcome.PASSED}),
    )
    try:
        update = asyncio.run(nodes.run_target_tests(state, harness.deps))
    finally:
        harness.close()

    assert update["status"] is RunStatus.SANDBOX_ERROR
    assert update["stop_reason"] is StopReason.SANDBOX_FAILURE
    assert update["error_class"] == FailureClass.WORKSPACE_ERROR.value
    assert update["error_origin"] == ErrorOrigin.WORKSPACE.value
    updated = state.model_copy(update=update)
    assert route_after_target_tests(updated) is Node.FINALIZE


def test_collection_error_from_patched_syntax_enters_diagnosis(tmp_path: Path) -> None:
    collection = scripted_result(ExecutionStatus.COLLECTION_ERROR).model_copy(
        update={
            "stderr_tail": (
                '  File "/testbed/src/pkg/core.py", line 2\n'
                "    return value -\n"
                "SyntaxError: invalid syntax"
            )
        }
    )
    harness = build_harness(
        tmp_path, results=[collection], coder_rounds=[ROUND], baseline_results=[]
    )
    state = _state(
        harness,
        tests_to_run=[TARGET],
        baseline=_baseline(harness, {f"{TARGET}::test_normalize": TestOutcome.PASSED}),
    )
    try:
        update = asyncio.run(nodes.run_target_tests(state, harness.deps))
    finally:
        harness.close()

    assert "status" not in update
    assert update["error_class"] == FailureClass.MODEL_OUTPUT_INVALID.value
    assert update["error_origin"] == ErrorOrigin.MODEL.value
    assert update["attempts"][-1].target_test_source == "planner"
    updated = state.model_copy(update=update)
    assert route_after_target_tests(updated) is Node.DIAGNOSE


def test_known_provider_timeouts_are_not_harness_errors() -> None:
    import httpx

    stopped = classify_exception(DispatchBudgetStoppedError("budget_stopped: frozen ledger"))
    timeout = classify_exception(httpx.ReadTimeout("read timed out"))

    for failure in (stopped, timeout):
        assert failure.failure_class is FailureClass.LLM_REQUEST_UNCERTAIN
        assert failure.origin is ErrorOrigin.PROVIDER
        assert failure.failure_class is not FailureClass.HARNESS_INTERNAL_ERROR
