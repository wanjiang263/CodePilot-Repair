"""Truthful Phase 5 JSON + Markdown run reports (P-21)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from codepilot.artifacts import REPORT_JSON, REPORT_MD, STATE_FINAL, ArtifactStore
from codepilot.clock import utcnow
from codepilot.observability.redaction import redact, redact_text
from codepilot.schemas.execution import ExecutionResult, TestBaseline
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.localization import LocalizationResult
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.regression import RegressionResult
from codepilot.schemas.reproduction import ReproductionResult
from codepilot.schemas.review import ReviewResult
from codepilot.schemas.state import Attempt, RepairState
from codepilot.schemas.usage import Usage
from codepilot.schemas.validation import ValidationResult

__all__ = ["RunReport", "write_run_report"]

_BOUNDARY = (
    "provisionally_verified means only that one validator-approved patch passed the selected "
    "authorized tests. It does not establish full regression safety or final issue resolution. "
    "m2_verified covers the authorized baseline suite with review and no confirmed new failures; "
    "existing failures and flaky tests remain disclosed. sampled_verified covers only the listed "
    "sample and impact tests. Neither level establishes external issue resolution or real-model quality."
)


class RunReport(BaseModel):
    baseline: TestBaseline | None = None
    reproduction: ReproductionResult | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    regression: RegressionResult | None = None
    review: ReviewResult | None = None
    verification_level: str = "provisionally_verified"
    runtime_seconds: float = 0
    usage_uncertain: bool = False
    regression_mode: str | None = None
    added_tests: dict[str, str] = Field(default_factory=dict)
    excluded_tests: list[str] = Field(default_factory=list)
    run_id: str
    task_id: str
    repository: str
    base_commit: str
    issue_summary: str
    issue_analysis: IssueAnalysis | None = None
    localization: LocalizationResult | None = None
    retrieval_snapshot_id: str | None = None
    impact: ImpactAnalysis | None = None
    repair_plan: RepairPlan | None = None
    modified_files: list[str] = Field(default_factory=list)
    git_diff: str = ""
    patch_hash: str = ""
    patch_validation: ValidationResult | None = None
    test_execution: ExecutionResult | None = None
    usage: Usage = Field(default_factory=Usage)
    test_runs: int = 0
    status: str
    stop_reason: str | None = None
    failure_reasons: list[str] = Field(default_factory=list)
    error_class: str | None = None
    """终态归因大类；``None`` = 没有失败（或这次运行早于归因机制）。"""

    error_origin: str | None = None
    """责任侧：model / provider / tool / workspace / harness。"""

    plan_corrections: int = 0
    """Planner 被重新问的次数。与服务商 HTTP 重试分开计。"""

    plan_tool_audit: dict[str, Any] = Field(default_factory=dict)
    """Planner 工具审计摘要。空 dict = **没采集到**，不等于"模型没调用工具"。"""

    llm_requests: dict[str, Any] = Field(default_factory=dict)
    """逐请求记录的按角色聚合（由装配层注入；纯图运行可以为空）。"""

    artifacts: dict[str, str] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    simulated_llm: bool = False
    simulated_sandbox: bool = False
    verification_boundary: str = _BOUNDARY

    @classmethod
    def from_state(
        cls,
        state: RepairState,
        *,
        repository: str,
        git_diff: str,
        modified_files: list[str],
        simulated_llm: bool,
        artifacts: dict[str, str] | None = None,
    ) -> RunReport:
        validation = state.current_attempt.validation if state.current_attempt else None
        execution = state.execution or (
            state.current_attempt.execution if state.current_attempt else None
        )
        level = state.status.value
        if level == "succeeded":
            target_verified = bool(
                validation and validation.passed and execution and execution.status.value == "PASS"
            )
            regression_verified = bool(
                state.regression
                and state.regression.complete
                and not state.regression.has_regression
            )
            review_verified = bool(state.review and not state.review.has_high_issues)
            if target_verified and regression_verified and review_verified:
                level = (
                    "sampled_verified"
                    if state.regression and state.regression.mode.value == "sampled"
                    else "m2_verified"
                )
            else:
                level = "provisionally_verified" if target_verified else "unverified"
        artifact_map = artifacts or state.report_artifacts
        if execution is not None and execution.artifact_dir:
            execution = execution.model_copy(
                update={"artifact_dir": artifact_map.get("execution", "attempts/01")}
            )
        simulated_sandbox = bool(
            execution and execution.diagnostics.get("execution_backend") == "fake"
        )
        # 工具审计必须是"记录"而不是"计数"：把 state.tool_calls 的长度当成
        # "模型调用了几次工具"，等于把采集缺失读成零调用。
        evidence = [{"type": "tool_call", **item} for item in state.tool_calls]
        if state.plan_tools is not None:
            evidence.append({"type": "planner_tool_audit", **state.plan_tools.summary()})
        evidence += [{"type": "event", **item} for item in state.events]
        return cls(
            usage_uncertain=state.usage_uncertain,
            regression_mode=state.regression.mode.value if state.regression else None,
            baseline=state.baseline,
            reproduction=state.reproduction,
            attempts=state.attempts,
            regression=state.regression,
            review=state.review,
            verification_level=level,
            runtime_seconds=max(0, (utcnow() - state.started_at).total_seconds()),
            run_id=state.run_id,
            task_id=state.task_id,
            repository=repository,
            base_commit=state.base_commit,
            issue_summary=(state.issue_analysis.summary if state.issue_analysis else state.issue),
            issue_analysis=state.issue_analysis,
            localization=state.localization,
            retrieval_snapshot_id=(
                state.localization.retrieval_snapshot_id if state.localization else None
            ),
            impact=state.impact,
            repair_plan=state.plan,
            modified_files=modified_files,
            git_diff=git_diff,
            patch_hash=hashlib.sha256(git_diff.encode()).hexdigest(),
            patch_validation=validation,
            test_execution=execution,
            usage=state.usage,
            test_runs=state.test_runs,
            status=state.status.value,
            stop_reason=state.stop_reason.value if state.stop_reason else None,
            failure_reasons=state.failure_reasons,
            error_class=state.error_class,
            error_origin=state.error_origin,
            plan_corrections=state.plan_corrections,
            plan_tool_audit=(state.plan_tools.summary() if state.plan_tools is not None else {}),
            artifacts=artifact_map,
            evidence=evidence,
            simulated_llm=simulated_llm,
            simulated_sandbox=simulated_sandbox,
        )

    def render_markdown(self) -> str:
        validation = (
            "not run" if self.patch_validation is None else str(self.patch_validation.passed)
        )
        execution = "not run" if self.test_execution is None else self.test_execution.status.value
        failures = "\n".join(f"- {item}" for item in self.failure_reasons) or "- None recorded"
        files = "\n".join(f"- `{item}`" for item in self.modified_files) or "- None"
        simulation = (
            f"FakeLLM={self.simulated_llm}; FakeSandbox={self.simulated_sandbox}. "
            "Simulation evidence is not real-model repair evidence."
        )
        attribution = (
            f"- Failure attribution: `{self.error_class or 'none'}` / "
            f"`{self.error_origin or 'none'}`\n"
            f"- Planner corrections: `{self.plan_corrections}`; "
            f"planner tool audit: "
            f"`{self.plan_tool_audit or 'NOT COLLECTED (not the same as zero calls)'}`\n"
        )
        return (
            f"# CodePilot Repair Run `{self.run_id}`\n\n"
            f"- Status: `{self.status}`\n"
            f"- Stop reason: `{self.stop_reason or 'none'}`\n"
            + attribution
            + f"- Repository: `{self.repository}`\n"
            f"- Base commit: `{self.base_commit}`\n"
            f"- Patch validation: `{validation}`\n"
            f"- Test execution: `{execution}`\n"
            f"- Test runs: `{self.test_runs}`\n"
            f"- Usage: {self.usage.total_tokens} tokens, {self.usage.cost:.6f} cost units\n\n"
            f"## Issue\n\n{self.issue_summary}\n\n"
            f"## Modified files\n\n{files}\n\n"
            f"## Failures\n\n{failures}\n\n"
            f"## Evidence boundary\n\n{simulation}\n\n{self.verification_boundary}\n\n"
            f"## Regression and Review\n\nVerification: {self.verification_level}\n\n"
            f"```json\n{self.regression.model_dump_json(indent=2) if self.regression else 'NOT RUN'}\n```\n\n"
            f"```json\n{self.review.model_dump_json(indent=2) if self.review else 'NOT RUN'}\n```\n\n"
            f"## Git diff\n\n```diff\n{self.git_diff}```\n"
        )


def write_run_report(
    report: RunReport, artifacts: ArtifactStore, state: RepairState
) -> dict[str, str]:
    payload = redact(report.model_dump(mode="json"))
    json_path = artifacts.write_text(REPORT_JSON, json.dumps(payload, ensure_ascii=False, indent=2))
    markdown_path = artifacts.write_text(REPORT_MD, redact_text(report.render_markdown()))
    state_path = artifacts.write_json(STATE_FINAL, redact(state.model_dump(mode="json")))
    return {
        "report_json": artifacts.relpath(json_path),
        "report_markdown": artifacts.relpath(markdown_path),
        "state_final": artifacts.relpath(state_path),
    }
