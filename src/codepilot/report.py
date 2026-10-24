"""Truthful Phase 5 JSON + Markdown run reports (P-21)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from codepilot.artifacts import REPORT_JSON, REPORT_MD, STATE_FINAL, ArtifactStore
from codepilot.clock import utcnow
from codepilot.observability.redaction import redact, redact_text
from codepilot.schemas.budget import ClosureAdmission
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

__all__ = ["RunReport", "VerificationClosure", "verification_level", "write_run_report"]

_BOUNDARY = (
    "provisionally_verified means only that one validator-approved patch passed the selected "
    "authorized tests. It does not establish full regression safety or final issue resolution. "
    "m2_verified covers the authorized baseline suite with review and no confirmed new failures; "
    "existing failures and flaky tests remain disclosed. sampled_verified covers only the listed "
    "sample and impact tests. Neither level establishes external issue resolution or real-model quality."
)


def _stage(executed: bool, outcome: object) -> str:
    """把"执行了吗 + 结果"写成一句不断档的话。

    ``NOT EXECUTED`` 必须与 ``executed, result=False`` 长得就不一样：报告是给人读的，
    把没跑过的阶段写成 ``False`` 会被读成"跑了，失败了"，而那正是需要被区分出来的事。
    """
    if not executed:
        return "NOT EXECUTED"
    return f"executed, result={outcome}"


class VerificationClosure(BaseModel):
    """Agent 内部自验证闭环的逐阶段真相（Phase 11 §六）。

    存在的理由是一句话覆盖不了四种不同的事实：**有补丁**、**补丁被验证过**、
    **验证过的是这一版补丁**、**审查过的是这一版补丁**。把未执行的阶段留空等于
    让读者自己补"大概是过了"，而首次真实运行暴露的正是这种误读。

    ``official_verdict`` 刻意**不**由本模型推断：官方 SWE-bench 判分由外部 Harness
    决定，内部闭环级别由本模型决定，两者各自记录、互不覆盖。
    """

    patch_present: bool
    candidate_patch_sha256: str
    frozen_by_coding_stage: bool
    coding_stop_reason: str | None
    """编码阶段自己的局部停止原因。非 ``None`` 只说明"这一轮不能再改代码了"。"""

    validator_executed: bool
    validator_passed: bool | None
    target_tests_executed: bool
    target_tests_status: str | None
    target_test_sha256: str
    """目标测试执行时所在补丁版本（Attempt 上记录的 SHA-256）。"""

    target_test_source: str | None = None
    """目标测试来源：reproduction/planner/impact/baseline/related/none。"""

    target_tests: list[str] = Field(default_factory=list)
    """实际交给 Sandbox 的目标测试；空列表不等于通过。"""

    target_test_baseline_failures: list[str] = Field(default_factory=list)
    """文件目标中 base commit 本来就失败的 NodeID（不进入 L1，但必须披露）。"""

    regression_executed: bool
    regression_complete: bool
    regression_mode: str | None = None
    """``full`` / ``sampled``。``sampled`` 不能当作全量回归通过。"""

    regression_levels: list[str]
    regression_new_failures: int
    regression_fixed_tests: list[str] = Field(default_factory=list)
    """base 上失败、patch 后通过的测试：独立于 L1 目标选择的 Issue 修复证据。"""

    regression_unexecuted_tests: int = 0
    """授权基线里没有拿到执行结果的 NodeID 数；全量完成时必须为 0。"""

    review_executed: bool
    review_approved: bool | None
    review_high_issues: int
    review_high_issues_known: bool = True
    """``review_executed=False`` 时高危问题数是未知，不是 0。"""

    review_bound_to_candidate: bool
    post_review_repair: bool
    review_calls: int
    unexecuted_stages: list[str]
    closure_admission: ClosureAdmission | None = None
    """最近一次编码派发前的闭环测试额度准入读数（DA-024）。

    ``None`` = 这条运行里没有做过该判定（例如 Phase 5 单次图），**不等于**"额度充足"。
    ``feasible=False`` 那一行必须同时出现在 :attr:`unexecuted_stages` 里：被拒的修复轮
    同样是"没有执行的阶段"，只是它是**故意**不执行的。
    """

    budget_usage: dict[str, str]
    cost_hard_cap: float | None
    cost_hard_cap_hit: bool

    @classmethod
    def from_state(cls, state: RepairState) -> VerificationClosure:
        # 延迟导入：``codepilot.graph`` 的包初始化会反过来 import 本模块
        # （graph/minimal.py:from codepilot.report import ...），模块级导入会成环。
        from codepilot.graph.budget import BudgetManager, Category
        from codepilot.graph.stop_rules import patch_hash

        attempt = state.current_attempt
        candidate = patch_hash(state.current_patch)
        execution = attempt.execution if attempt is not None else None
        regression = state.regression
        review = state.review
        unexecuted: list[str] = []
        if attempt is None or not attempt.validation.passed:
            unexecuted.append("patch_validation")
        if execution is None:
            unexecuted.append("target_tests")
        if regression is None:
            unexecuted.append("regression")
        elif not regression.complete:
            unexecuted.append("regression_incomplete")
        if review is None:
            unexecuted.append(
                f"review:{state.review_skip_reason or 'the reviewer produced no result'}"
            )
        admission = state.closure_admission
        if admission is not None and not admission.feasible:
            unexecuted.append(
                f"repair_round:{admission.trigger}:the verification closure needs "
                f"{admission.required} test runs but {admission.remaining} remained"
            )
        readings = BudgetManager().readings(state.budget, state)
        usage = {
            category.value: (
                f"{used:g}/unlimited" if limit == float("inf") else f"{used:g}/{limit:g}"
            )
            for category, (used, limit) in readings.items()
        }
        cost_used, cost_limit = readings[Category.COST]
        return cls(
            patch_present=bool(state.current_patch.strip()),
            candidate_patch_sha256=candidate,
            frozen_by_coding_stage=bool(state.frozen_patch_hash),
            coding_stop_reason=state.coding_stop_reason.value if state.coding_stop_reason else None,
            validator_executed=attempt is not None,
            validator_passed=attempt.validation.passed if attempt is not None else None,
            target_tests_executed=execution is not None,
            target_tests_status=execution.status.value if execution is not None else None,
            target_test_sha256=attempt.patch_hash if attempt is not None else "",
            target_test_source=attempt.target_test_source if attempt is not None else None,
            target_tests=attempt.target_tests if attempt is not None else [],
            target_test_baseline_failures=(
                attempt.target_test_baseline_failures if attempt is not None else []
            ),
            regression_executed=regression is not None,
            regression_complete=bool(regression and regression.complete),
            regression_mode=regression.mode.value if regression is not None else None,
            regression_levels=[
                level.value for level in (regression.levels_run if regression else ())
            ],
            regression_new_failures=len(regression.new_failures) if regression else 0,
            regression_fixed_tests=list(regression.fixed_tests) if regression else [],
            regression_unexecuted_tests=(
                len(regression.unexecuted_tests) if regression is not None else 0
            ),
            review_executed=review is not None,
            review_approved=review.approved if review is not None else None,
            review_high_issues=(
                sum(1 for issue in review.issues if issue.severity.value == "high")
                if review is not None
                else 0
            ),
            review_high_issues_known=review is not None,
            review_bound_to_candidate=bool(review and review.reviewed_patch_hash == candidate),
            post_review_repair=state.review_repairs > 0,
            review_calls=state.review_calls,
            unexecuted_stages=unexecuted,
            closure_admission=admission,
            budget_usage=usage,
            cost_hard_cap=None if cost_limit == float("inf") else cost_limit,
            cost_hard_cap_hit=bool(state.budget.max_cost is not None and cost_used >= cost_limit),
        )


def verification_level(state: RepairState) -> str:
    """内部验证级别（``m2_verified`` / ``sampled_verified`` / …）的唯一口径。

    刻意抽成函数而不是埋在 :meth:`RunReport.from_state` 里：评测装配层也要回答同一个
    问题，两处各算一遍迟早会算出两个答案，而"成功率"的定义容不下两套口径。

    相对状态标签更严：``status == succeeded`` 只是必要条件。审查结论还必须绑定
    **当前候选补丁**——审过 v1 不给 v2 背书（``reviewed_patch_hash`` 为空是
    Phase 9 之前的历史记录，按"未盖章"处理，不当作已核对）。
    """
    from codepilot.graph.stop_rules import patch_hash

    level = state.status.value
    if level != "succeeded":
        return level
    attempt = state.current_attempt
    validation = attempt.validation if attempt is not None else None
    execution = state.execution or (attempt.execution if attempt is not None else None)
    target_verified = bool(
        validation and validation.passed and execution and execution.status.value == "PASS"
    )
    regression_verified = bool(
        state.regression and state.regression.complete and not state.regression.has_regression
    )
    review = state.review
    # 空 ``reviewed_patch_hash`` 是"没有绑定"，不是"历史版本所以默认通过"。
    # 当前候选必须被当前审查结论明确盖章；否则审过 v1 会给 v2 背书。
    review_verified = bool(
        review
        and review.approved
        and not review.has_high_issues
        and review.reviewed_patch_hash == patch_hash(state.current_patch)
    )
    if target_verified and regression_verified and review_verified:
        return (
            "sampled_verified"
            if state.regression and state.regression.mode.value == "sampled"
            else "m2_verified"
        )
    return "provisionally_verified" if target_verified else "unverified"


class RunReport(BaseModel):
    baseline: TestBaseline | None = None
    reproduction: ReproductionResult | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    regression: RegressionResult | None = None
    review: ReviewResult | None = None
    verification_level: str = "provisionally_verified"
    closure: VerificationClosure | None = None
    """逐阶段的自验证真相。``verification_level`` 是结论，这里是**依据**。

    ``None`` = 这条运行没有走完整闭环图（Phase 5 单次图）；不等于"闭环全部执行"。
    """

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
    """交付补丁原始字节的 SHA-256（与 official prediction 的哈希口径一致）。"""

    delivered_patch_identity_sha256: str = ""
    """交付补丁规范化 diff 的 SHA-256（与候选/审查身份口径一致）。"""

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

    plan_semantic_rejections: int = 0
    """被语义校验拒绝的计划次数（每次都有 artifact）。"""

    plan_schema_rejections: int = 0
    """未通过 JSON Schema / Pydantic 的次数。"""

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
        from codepilot.graph.stop_rules import patch_hash as delivered_patch_hash

        validation = state.current_attempt.validation if state.current_attempt else None
        execution = state.execution or (
            state.current_attempt.execution if state.current_attempt else None
        )
        level = verification_level(state)
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
            closure=VerificationClosure.from_state(state),
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
            delivered_patch_identity_sha256=(
                delivered_patch_hash(git_diff) if git_diff else ""
            ),
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
            plan_semantic_rejections=state.plan_semantic_rejections,
            plan_schema_rejections=state.plan_schema_rejections,
            plan_tool_audit=(state.plan_tools.summary() if state.plan_tools is not None else {}),
            artifacts=artifact_map,
            evidence=evidence,
            simulated_llm=simulated_llm,
            simulated_sandbox=simulated_sandbox,
        )

    def _closure_markdown(self) -> str:
        if self.closure is None:
            return (
                "## Self-verification closure\n\n"
                "NOT APPLICABLE: this run did not go through the full iterative graph, so no "
                "per-stage closure evidence was recorded. Absent evidence is not passed evidence.\n"
            )
        closure = self.closure
        admission = closure.closure_admission
        admission_text = (
            "NOT EVALUATED (no coding dispatch recorded a test-run plan)"
            if admission is None
            else (
                f"trigger={admission.trigger}, closure requires {admission.required} run(s), "
                f"{admission.remaining} of {admission.allowance} remained, "
                f"coding-stage share={admission.coder_share}, "
                f"flaky re-run reserve={admission.retry_reserve} → "
                + ("allowed" if admission.feasible else "REFUSED before dispatch")
            )
        )
        rows = [
            ("Valid patch", f"{closure.patch_present} (`{closure.candidate_patch_sha256[:16]}`)"),
            (
                "Patch frozen by the coding stage",
                str(closure.frozen_by_coding_stage)
                + (
                    f" — coding stage stopped on `{closure.coding_stop_reason}`"
                    if closure.coding_stop_reason
                    else ""
                ),
            ),
            ("PatchValidator", _stage(closure.validator_executed, closure.validator_passed)),
            (
                "Target tests (L1)",
                _stage(closure.target_tests_executed, closure.target_tests_status)
                + f" on `{closure.target_test_sha256[:16]}`",
            ),
            (
                "Regression (L2/L3)",
                (
                    f"executed, complete={closure.regression_complete}, "
                    f"levels={closure.regression_levels or 'none'}, "
                    f"new failures={closure.regression_new_failures}"
                    if closure.regression_executed
                    else "NOT EXECUTED"
                ),
            ),
            (
                "PatchReviewer",
                (
                    f"executed, approved={closure.review_approved}, "
                    f"high issues={closure.review_high_issues}, "
                    f"bound to candidate={closure.review_bound_to_candidate}"
                    if closure.review_executed
                    else f"NOT EXECUTED ({'; '.join(closure.unexecuted_stages) or 'no result'})"
                ),
            ),
            (
                "Post-review repair",
                f"{closure.post_review_repair}; review calls="
                f"{closure.review_calls}/{self.closure.budget_usage.get('review_calls', '?')}",
            ),
            ("Budget usage", ", ".join(f"{k}={v}" for k, v in closure.budget_usage.items())),
            (
                "Closure admission (test runs)",
                admission_text,
            ),
            (
                "Cost hard cap",
                (
                    "not set"
                    if closure.cost_hard_cap is None
                    else f"{closure.cost_hard_cap:g}"
                    + (" (REACHED)" if closure.cost_hard_cap_hit else "")
                ),
            ),
            ("Internal verification level", f"`{self.verification_level}`"),
        ]
        body = "\n".join(f"- {name}: {value}" for name, value in rows)
        return (
            "## Self-verification closure\n\n"
            f"{body}\n\n"
            "Official SWE-bench resolution is decided by the external harness and recorded "
            "separately; it neither raises nor lowers the internal level above.\n"
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
            f"- Planner corrections: `{self.plan_corrections}` "
            f"(semantic rejections `{self.plan_semantic_rejections}`, "
            f"schema rejections `{self.plan_schema_rejections}`); "
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
            + self._closure_markdown()
            + f"## Evidence boundary\n\n{simulation}\n\n{self.verification_boundary}\n\n"
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
