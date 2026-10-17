"""归因与指标口径（Harness Hardening 修复四）。

覆盖首次真实 GLM 运行暴露的那类误读：``execution_state=completed`` +
``stop_reason=workflow_error`` + 空补丁，却被报告成 ``resolved=false``、
``environment_failures=0`` —— 模型还没产出任何东西就被当成"修复失败"。

这里逐个摆出九种结局，断言它们**归到不同类别**、分母清晰、且没有任何一种能
伪造出模型能力结论。全部离线：替身装配 + 规则化沙箱。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from codepilot.eval.assembly import InferenceOutcome
from codepilot.eval.attribution import Attribution, EvaluationState, attribute_run
from codepilot.eval.evaluation import GoldEvaluator
from codepilot.eval.metrics import TaskOutcome, summarize
from codepilot.eval.report import build_metrics, evidence_classification, render_summary
from codepilot.eval.runner import (
    BenchmarkConfig,
    BenchmarkRunner,
    ExecutionState,
    TaskRecord,
)
from codepilot.eval.synthetic import load_fixture
from codepilot.eval.synthetic_build import build_fixture
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.usage import Usage
from support.synth import demo_spec

TARGET_NODE = "tests/test_hidden.py::test_normalize_keeps_value"
P2P_NODE = "tests/test_public.py::test_documented_base_behaviour"


# ------------------------------------------------------------------ 纯归因函数


def test_attribute_run_separates_the_nine_outcomes() -> None:
    cases: list[tuple[str, dict[str, Any], Attribution]] = [
        (
            "正常解决",
            {
                "execution_state": "completed",
                "evaluation_state": EvaluationState.EVALUATED,
                "resolved": True,
                "patch_produced": True,
            },
            Attribution.RESOLVED,
        ),
        (
            "正常未解决",
            {
                "execution_state": "completed",
                "evaluation_state": EvaluationState.EVALUATED,
                "resolved": False,
                "patch_produced": True,
            },
            Attribution.MODEL_NOT_RESOLVED,
        ),
        (
            "模型非法计划",
            {
                "execution_state": "completed",
                "evaluation_state": EvaluationState.NOT_RUN,
                "resolved": None,
                "error_class": "plan_semantic_invalid",
                "error_origin": "model",
                "stop_reason": "plan_rejected",
                "status": "plan_rejected",
            },
            Attribution.MODEL_INVALID_PLAN,
        ),
        (
            "模型输出不合法",
            {
                "execution_state": "completed",
                "evaluation_state": EvaluationState.NOT_RUN,
                "resolved": None,
                "error_class": "model_output_invalid",
                "error_origin": "model",
            },
            Attribution.MODEL_OUTPUT_ERROR,
        ),
        (
            "Harness 代码异常",
            {
                "execution_state": "completed",
                "evaluation_state": EvaluationState.NOT_RUN,
                "resolved": None,
                "error_class": "harness_internal_error",
                "error_origin": "harness",
                "stop_reason": "workflow_error",
                "status": "error",
            },
            Attribution.HARNESS_ERROR,
        ),
        (
            "沙箱故障",
            {
                "execution_state": "completed",
                "evaluation_state": EvaluationState.NOT_RUN,
                "resolved": None,
                "stop_reason": "sandbox_failure",
                "status": "sandbox_error",
            },
            Attribution.SANDBOX_ERROR,
        ),
        (
            "环境错误",
            {
                "execution_state": "inference_error",
                "evaluation_state": EvaluationState.NOT_RUN,
                "resolved": None,
                "stop_reason": "env_error",
                "status": "env_error",
            },
            Attribution.ENV_ERROR,
        ),
        (
            "费用预算耗尽",
            {
                "execution_state": "completed",
                "evaluation_state": EvaluationState.NOT_RUN,
                "resolved": None,
                "stop_reason": "budget_cost",
                "status": "budget_exhausted",
            },
            Attribution.BUDGET_STOPPED,
        ),
        (
            "评测器自身错误",
            {
                "execution_state": "evaluation_error",
                "evaluation_state": EvaluationState.ERROR,
                "resolved": None,
            },
            Attribution.EVALUATION_ERROR,
        ),
        (
            "Agent 自称成功但没有独立评测",
            {
                "execution_state": "completed",
                "evaluation_state": EvaluationState.NOT_RUN,
                "resolved": None,
                "patch_produced": True,
                "claims_success": True,
                "status": "provisionally_verified",
            },
            Attribution.EVALUATION_NOT_RUN,
        ),
    ]
    labelled = {}
    for name, kwargs, expected in cases:
        got = attribute_run(**kwargs)
        assert got is expected, f"{name} 应当归因为 {expected.value}，实际 {got.value}"
        labelled[name] = got.value
    # 九种结局必须是九个不同的类别。
    assert len(set(labelled.values())) == len(cases), labelled


def test_harness_error_is_neither_env_error_nor_model_failure() -> None:
    harness = attribute_run(
        execution_state="completed",
        evaluation_state=EvaluationState.NOT_RUN,
        resolved=None,
        error_class="harness_internal_error",
        error_origin="harness",
        stop_reason="workflow_error",
        status="error",
    )
    env = attribute_run(
        execution_state="completed",
        evaluation_state=EvaluationState.NOT_RUN,
        resolved=None,
        stop_reason="sandbox_failure",
        status="sandbox_error",
    )
    model = attribute_run(
        execution_state="completed",
        evaluation_state=EvaluationState.NOT_RUN,
        resolved=None,
        error_class="plan_semantic_invalid",
        error_origin="model",
    )
    assert len({harness, env, model}) == 3


# ------------------------------------------------------------------- 指标口径


def _task(task: str, **overrides: Any) -> TaskOutcome:
    base: dict[str, object] = {"task_id": task, "seed": 0, "category": "missing_validation"}
    base.update(overrides)
    return TaskOutcome(**base)


def test_harness_crash_keeps_end_to_end_denominator_but_leaves_conditional() -> None:
    metrics = summarize(
        [
            _task(
                "harness",
                resolved=None,
                empty_patch=True,
                stop_reason="workflow_error",
                attribution=Attribution.HARNESS_ERROR,
                evaluation_state=EvaluationState.NOT_RUN,
                error_class="harness_internal_error",
                error_origin="harness",
                harness_failure=True,
            ),
            _task(
                "ok",
                resolved=True,
                attribution=Attribution.RESOLVED,
                evaluation_state=EvaluationState.EVALUATED,
            ),
        ]
    )
    attribution = metrics.attribution
    assert attribution.end_to_end_resolved.denominator == 2
    assert attribution.end_to_end_resolved.numerator == 1
    # 条件解决率只看真正评过的：分母 1，不是 2。
    assert attribution.conditional_resolved.denominator == 1
    assert attribution.conditional_resolved.numerator == 1
    assert attribution.evaluation_coverage.denominator == 2
    assert attribution.evaluation_coverage.numerator == 1
    assert attribution.harness_error_rate.numerator == 1
    assert attribution.unevaluated_tasks == 1
    # 总体指标的定义没被改动：崩溃仍留在 resolved_rate 的分母里。
    assert metrics.repair.resolved.denominator == 2
    assert metrics.repair.resolved.numerator == 1


def test_zero_evaluation_coverage_leaves_conditional_rate_undefined() -> None:
    """一个都没评成时，条件解决率是 n/a，不是 0% 也不是 100%。"""
    metrics = summarize(
        [
            _task(
                "a",
                resolved=None,
                empty_patch=True,
                attribution=Attribution.HARNESS_ERROR,
                evaluation_state=EvaluationState.NOT_RUN,
                error_class="harness_internal_error",
                error_origin="harness",
                harness_failure=True,
            )
        ]
    )
    assert metrics.attribution.conditional_resolved.value is None
    assert metrics.attribution.evaluation_coverage.value == 0.0
    assert metrics.attribution.harness_error_rate.value == 1.0
    assert metrics.repair.self_verified_precision.value is None


def test_env_and_harness_and_sandbox_error_rates_are_reported_apart() -> None:
    metrics = summarize(
        [
            _task(
                "h",
                attribution=Attribution.HARNESS_ERROR,
                error_origin="harness",
                harness_failure=True,
            ),
            _task("e", attribution=Attribution.ENV_ERROR, environment_failure=True),
            _task("s", attribution=Attribution.SANDBOX_ERROR, environment_failure=True),
            _task(
                "p",
                attribution=Attribution.MODEL_INVALID_PLAN,
                error_class="plan_semantic_invalid",
                error_origin="model",
            ),
        ]
    )
    attribution = metrics.attribution
    assert attribution.harness_error_rate.numerator == 1
    assert attribution.environment_error_rate.numerator == 1
    assert attribution.sandbox_error_rate.numerator == 1
    assert attribution.plan_rejection_rate.numerator == 1
    assert sum(attribution.counts.values()) == 4


# ------------------------------------------------------- 旧格式（v1）兼容映射


def test_legacy_record_gets_an_attribution_without_rewriting_history() -> None:
    v1 = {
        "task_id": "old",
        "seed": 0,
        "category": "missing_validation",
        "difficulty": "easy",
        "multi_file": False,
        "has_traceback": False,
        "model_identity": "x",
        "config_hash": "h",
        "code_commit": "c",
        "fixture_version": "f",
        "base_commit": "b",
        "run_id": "r",
        "identity": "i",
        "execution_state": "completed",
        "evaluation_status": "NOT_RESOLVED",
        "resolved": False,
        "first_attempt_resolved": None,
        "agent_claims_success": False,
        "empty_patch": False,
        "invalid_patch": False,
        "patch_sha256": "abc",
        "first_attempt_patch_sha256": None,
        "iterations": 1,
        "stop_reason": "tests_failed",
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
        "llm_calls": 1,
        "cost": 0.1,
        "cost_currency": "CNY",
        "runtime_seconds": 1.0,
        "verification_mode": "full",
        "simulated_llm": False,
        "sandbox_backend": "docker",
        "real_docker_evaluation": True,
        "usage_uncertain": False,
    }
    record = TaskRecord.from_dict(v1)
    assert record.record_version == 1
    assert record.attribution == Attribution.MODEL_NOT_RESOLVED.value
    assert record.evaluation_state == EvaluationState.EVALUATED.value

    # v1 里那次真实的 Harness 崩溃：completed + workflow_error + 空补丁。
    crashed = {**v1, "stop_reason": "workflow_error", "resolved": False, "empty_patch": True}
    mapped = TaskRecord.from_dict(crashed)
    assert mapped.attribution in {
        Attribution.HARNESS_ERROR.value,
        Attribution.EVALUATION_NOT_RUN.value,
    }
    assert "record_version" not in json.dumps(v1)


# ----------------------------------------------------------- runner 端到端


class _StubAssembly:
    def __init__(self, outcome: InferenceOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    def run(self, *, run_id: str, seed: int) -> InferenceOutcome:
        self.calls += 1
        return dataclasses.replace(self._outcome, run_id=run_id, seed=seed)


class _RuleSandbox:
    backend = "fake"

    def __init__(self, *, broken: bool = False) -> None:
        self._broken = broken
        self.run_count = 0

    def build_env(self, **kwargs: Any) -> EnvironmentBuildResult:
        if self._broken:
            return EnvironmentBuildResult(
                status=ExecutionStatus.ENV_ERROR, cache_key="k", error="scripted env failure"
            )
        return EnvironmentBuildResult(
            status=ExecutionStatus.PASS,
            image_ref="fake://env",
            image_id="fake-image",
            cache_key="k",
        )

    def run_tests(self, **kwargs: Any) -> ExecutionResult:
        self.run_count += 1
        outcome = TestOutcome.PASSED
        return ExecutionResult(
            status=ExecutionStatus.PASS,
            tests=[
                TestCaseResult(node_id=TARGET_NODE, outcome=outcome, duration=0.01),
                TestCaseResult(node_id=P2P_NODE, outcome=outcome, duration=0.01),
            ],
            duration_seconds=0.01,
            tests_passed=2,
            diagnostics={"execution_backend": "fake"},
        )


def _fixtures(tmp_path: Path, count: int = 1) -> list[Any]:
    out: list[Any] = []
    for index in range(count):
        directory = build_fixture(
            demo_spec(task_id=f"attr-task-{index}"),
            tmp_path / "suite",
            scratch_root=tmp_path / "scratch" / str(index),
        )
        out.append(load_fixture(directory))
    return out


def _outcome(patch: str, **overrides: Any) -> InferenceOutcome:
    usage = Usage(input_tokens=100, output_tokens=50, cost=0.5, llm_calls=4)
    base = InferenceOutcome(
        run_id="stub",
        task_id="t",
        seed=0,
        base_commit="0" * 40,
        repository_id="repo_demo",
        status="succeeded",
        stop_reason=None,
        final_patch=patch,
        final_patch_sha256=str(len(patch)),
        first_attempt_patch=patch,
        first_attempt_patch_sha256=str(len(patch)),
        iterations=1,
        attempts_saved=1,
        usage=usage,
        usage_uncertain=False,
        test_runs=1,
        runtime_seconds=1.0,
        localization_files=(),
        localization_symbols=(),
        agent_claims_success=True,
        empty_patch=not patch.strip(),
        invalid_patch=False,
        simulated_llm=True,
        sandbox_backend="fake",
        verification_mode="full",
        artifact_dir="artifacts/stub",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _runner(tmp_path: Path, fixtures: list[Any], outcome: InferenceOutcome) -> BenchmarkRunner:
    sandbox = _RuleSandbox()
    evaluator = GoldEvaluator(
        manager=WorkspaceManager(cache_root=tmp_path / "cache"),
        sandbox_factory=lambda artifacts: sandbox,
        work_root=tmp_path / "evalwork",
    )
    config = BenchmarkConfig(
        seeds=(0,), model_identity="FakeLLM", out_dir=tmp_path / "out", run_label="attr"
    )
    return BenchmarkRunner(
        config=config,
        fixtures=fixtures,
        assembly_factory=lambda fixture, seed: _StubAssembly(outcome),
        evaluator=evaluator,
        journal_directory=tmp_path / "journal",
    )


def test_harness_crash_is_not_reported_as_a_model_miss(tmp_path: Path) -> None:
    """本轮的总验收：崩溃 → HARNESS_ERROR，未评测，且不构成模型能力结论。"""
    fixtures = _fixtures(tmp_path, 1)
    crashed = _outcome(
        "",
        status="error",
        stop_reason="workflow_error",
        agent_claims_success=False,
        attempts_saved=0,
        iterations=0,
        verification_mode="none",
        error_class="harness_internal_error",
        error_origin="harness",
        failure_reasons=("plan: [plan_semantic_invalid/model] boom",),
    )
    runner = _runner(tmp_path, fixtures, crashed)
    run = runner.run()
    record = run.records[0]
    assert record.attribution == Attribution.HARNESS_ERROR.value
    assert record.evaluation_state == EvaluationState.NOT_RUN.value
    assert record.resolved is None, "没评过就不能给出 False"
    assert record.evaluation_status == "NOT_EVALUATED"
    assert record.counted is True, "runner 确实跑完了"
    assert record.effectively_evaluated is False, "但那不是有效评测"

    metrics = build_metrics(run)
    attribution = metrics["overall"]["attribution"]
    assert attribution["counts"] == {Attribution.HARNESS_ERROR.value: 1}
    assert attribution["end_to_end_resolved"]["denominator"] == 1
    assert attribution["conditional_resolved"]["denominator"] == 0
    assert attribution["conditional_resolved"]["value"] is None
    assert attribution["evaluation_coverage"]["value"] == 0.0

    evidence = evidence_classification(run)
    assert evidence["harness_errors"] == 1
    assert evidence["claims_model_capability"] is False
    summary = render_summary(run, metrics)
    assert "Harness 自身错误: **1**" in summary
    assert "## 失败归因（谁错了）" in summary


def test_model_plan_rejection_and_real_miss_are_attributed_differently(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 1)
    rejected = _outcome(
        "",
        status="plan_rejected",
        stop_reason="plan_rejected",
        agent_claims_success=False,
        attempts_saved=0,
        iterations=0,
        verification_mode="none",
        error_class="plan_semantic_invalid",
        error_origin="model",
        plan_corrections=2,
    )
    run = _runner(tmp_path, fixtures, rejected).run()
    record = run.records[0]
    assert record.attribution == Attribution.MODEL_INVALID_PLAN.value
    assert record.plan_corrections == 2
    assert record.resolved is None

    metrics = build_metrics(run)
    assert metrics["overall"]["attribution"]["plan_rejection_rate"]["numerator"] == 1
    assert metrics["overall"]["attribution"]["conditional_resolved"]["denominator"] == 0


def test_results_jsonl_carries_attribution_and_version(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 1)
    resolved = _outcome(fixtures[0].evaluation.gold_patch)
    run = _runner(tmp_path, fixtures, resolved).run()
    record = run.records[0]
    assert record.attribution == Attribution.RESOLVED.value
    assert record.evaluation_state == EvaluationState.EVALUATED.value
    assert record.resolved is True

    payload = json.loads(json.dumps(record.as_dict()))
    assert payload["record_version"] == 2
    assert payload["attribution"] == "resolved"
    # 重新读回来必须得到同一条结论（journal 与 results.jsonl 的往返一致性）。
    assert TaskRecord.from_dict(payload) == record


def test_metrics_json_declares_its_schema_version(tmp_path: Path) -> None:
    fixtures = _fixtures(tmp_path, 1)
    run = _runner(tmp_path, fixtures, _outcome(fixtures[0].evaluation.gold_patch)).run()
    metrics = build_metrics(run)
    assert metrics["schema_version"] == 2
    assert metrics["data_integrity"]["harness_errors_kept_in_end_to_end_denominator"] is True
    assert metrics["attribution_counts"][Attribution.RESOLVED.value] == 1
    assert ExecutionState.COMPLETED.value in metrics["execution_state_counts"]


def test_unknown_attribution_default_is_not_resolved() -> None:
    """缺字段的旧对象默认落到"未评测"，绝不默认成"已解决/未解决"。"""
    assert TaskOutcome(task_id="bare").attribution is Attribution.EVALUATION_NOT_RUN
    assert TaskOutcome(task_id="bare").resolved is None


def test_capability_claim_needs_real_model_and_full_evaluation(tmp_path: Path) -> None:
    """``claims_model_capability`` 的两个前提：真实模型，且每条都拿到独立评测判定。

    FakeLLM 的脚本是照着 gold 生成的，全对也只证明链路通；真实模型批次里只要有一条
    没评上，条件读数就不再是这批派发的读数。
    """
    fixtures = _fixtures(tmp_path, 1)
    good = _runner(tmp_path / "real", fixtures, _outcome(fixtures[0].evaluation.gold_patch)).run()
    real = dataclasses.replace(
        good, records=tuple(dataclasses.replace(r, simulated_llm=False) for r in good.records)
    )
    evidence = evidence_classification(real)
    assert evidence["engine"] == "真实模型 Benchmark"
    assert evidence["claims_model_capability"] is True

    crashed = _runner(
        tmp_path / "crash",
        fixtures,
        _outcome(
            "",
            status="error",
            stop_reason="workflow_error",
            agent_claims_success=False,
            attempts_saved=0,
            iterations=0,
            verification_mode="none",
            error_class="harness_internal_error",
            error_origin="harness",
        ),
    ).run()
    broken = evidence_classification(
        dataclasses.replace(
            crashed,
            records=tuple(dataclasses.replace(r, simulated_llm=False) for r in crashed.records),
        )
    )
    assert broken["engine"] == "真实模型 Benchmark"
    assert broken["claims_model_capability"] is False
    assert broken["harness_errors"] == 1
    # FakeLLM 即使全对、全覆盖，也不声称测到了模型能力。
    fake = evidence_classification(
        _runner(tmp_path / "fake", fixtures, _outcome(fixtures[0].evaluation.gold_patch)).run()
    )
    assert fake["claims_model_capability"] is False
