"""Truthful Phase 5 JSON + Markdown run reports (P-21)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from codepilot.artifacts import REPORT_JSON, REPORT_MD, STATE_FINAL, ArtifactStore
from codepilot.observability.redaction import redact, redact_text
from codepilot.schemas.execution import ExecutionResult
from codepilot.schemas.impact import ImpactAnalysis
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.localization import LocalizationResult
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.state import RepairState
from codepilot.schemas.usage import Usage
from codepilot.schemas.validation import ValidationResult

__all__ = ["RunReport", "write_run_report"]

_BOUNDARY = (
    "provisionally_verified means only that one validator-approved patch passed the selected "
    "authorized tests. It does not establish full regression safety or final issue resolution."
)


class RunReport(BaseModel):
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
    patch_validation: ValidationResult | None = None
    test_execution: ExecutionResult | None = None
    usage: Usage = Field(default_factory=Usage)
    test_runs: int = 0
    status: str
    stop_reason: str | None = None
    failure_reasons: list[str] = Field(default_factory=list)
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
        artifact_map = artifacts or state.report_artifacts
        if execution is not None and execution.artifact_dir:
            execution = execution.model_copy(
                update={"artifact_dir": artifact_map.get("execution", "attempts/01")}
            )
        simulated_sandbox = bool(
            execution and execution.diagnostics.get("execution_backend") == "fake"
        )
        evidence = [{"type": "tool_call", **item} for item in state.tool_calls] + [
            {"type": "event", **item} for item in state.events
        ]
        return cls(
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
            patch_validation=validation,
            test_execution=execution,
            usage=state.usage,
            test_runs=state.test_runs,
            status=state.status.value,
            stop_reason=state.stop_reason.value if state.stop_reason else None,
            failure_reasons=state.failure_reasons,
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
        return (
            f"# CodePilot Repair Run `{self.run_id}`\n\n"
            f"- Status: `{self.status}`\n"
            f"- Stop reason: `{self.stop_reason or 'none'}`\n"
            f"- Repository: `{self.repository}`\n"
            f"- Base commit: `{self.base_commit}`\n"
            f"- Patch validation: `{validation}`\n"
            f"- Test execution: `{execution}`\n"
            f"- Test runs: `{self.test_runs}`\n"
            f"- Usage: {self.usage.total_tokens} tokens, {self.usage.cost:.6f} cost units\n\n"
            f"## Issue\n\n{self.issue_summary}\n\n"
            f"## Modified files\n\n{files}\n\n"
            f"## Failures\n\n{failures}\n\n"
            f"## Evidence boundary\n\n{simulation}\n\n{self.verification_boundary}\n\n"
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
