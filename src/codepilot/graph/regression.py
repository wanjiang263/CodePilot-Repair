"""Baseline-relative L2/L3 regression with bounded flaky retries."""

from __future__ import annotations

import math
import random
from pathlib import PurePosixPath

from codepilot.graph.budget import REVIEW_TIME_RESERVE_SECONDS, Activity
from codepilot.graph.deps import Deps
from codepilot.graph.operations import digest, execute_once
from codepilot.graph.routing import pending_review_calls
from codepilot.graph.stop_rules import patch_hash
from codepilot.patch.diff import generate_git_diff
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestBaseline
from codepilot.schemas.regression import RegressionLevel, RegressionMode, RegressionResult
from codepilot.schemas.state import RepairState
from codepilot.tools.write_tools import validate_test_target

__all__ = [
    "MAX_FULL_REGRESSION_RUNS",
    "MAX_NEW_FAILURE_RETRIES",
    "RegressionRunner",
    "full_regression_runs",
    "new_failures",
    "retry_allowance",
    "usable_baseline",
]

#: L3 全量回归在一个 run 里最多跑几次。超过之后 :class:`RegressionRunner` 直接判定
#: "L3 full-run limit reached"（``complete=False``），**不再**多花一次容器执行。
MAX_FULL_REGRESSION_RUNS = 2

#: 一次回归检查里所有新失败共享的 flaky 重试预算。每个新失败各重试两次会把
#: ``max_test_runs`` 耗尽（真实 pytest run 里 9 个新失败 × 2 = 18 次），修复循环
#: 反而没有额度执行；预留量与 closure admission 的 ``retry_reserve`` 一致。
MAX_NEW_FAILURE_RETRIES = 2


def usable_baseline(state: RepairState) -> TestBaseline | None:
    """回归可以依据的基线；不完整、与当前 base commit 不一致或为空时返回 ``None``。

    闭环准入也要问同一个问题（"这一版候选还会不会真的跑 L2/L3"）。这条判据抄第二份
    就会与回归实现漂移，于是"准入以为要跑、实际不跑"会凭空多留一次测试额度。
    """
    baseline = state.baseline
    if (
        baseline is None
        or not baseline.complete
        or baseline.base_commit != state.base_commit
        or not baseline.outcomes
    ):
        return None
    return baseline


def full_regression_runs(state: RepairState) -> int:
    """历史轮次（不含当前轮）里已经跑过多少次 L3 全量回归。

    闭环准入必须问**同一个问题**：它算的是"下一版候选还要不要跑 L3"。两处各写一遍
    迟早会漂移——准入以为要跑、实际不跑，就会凭空多留一次额度。
    """
    return sum(
        RegressionLevel.FULL in attempt.regression.levels_run
        for attempt in state.attempts[:-1]
        if attempt.regression
    )


def retry_allowance(state: RepairState, deps: Deps, *, full_level_expected: bool = True) -> int:
    """Flaky re-run budget that never spends the deterministic closure's runs.

    A candidate's mandatory closure is target tests plus the configured
    regression levels.  Flaky classification is *optional* evidence: it may
    only consume surplus runs after those mandatory runs are reserved.  When
    fewer than two re-runs are affordable the classification is impossible, so
    the node is conservatively kept as a new failure instead of spending the
    last run and leaving L3 (and the Reviewer) without budget.
    """
    remaining = max(state.budget.max_test_runs - deps.test_runs, 0)
    return max(remaining - (1 if full_level_expected else 0), 0)


def new_failures(baseline: TestBaseline, after: ExecutionResult) -> list[str]:
    """Exact parametrized NodeIDs only; missing baseline is never assumed PASS."""
    if not baseline.complete or after.is_environment_problem:
        return []
    return sorted(
        {
            t.node_id
            for t in after.failed_tests
            if baseline.outcomes.get(t.node_id) is TestOutcome.PASSED
            and t.node_id not in baseline.flaky
        }
    )


class RegressionRunner:
    def __init__(self, *, seed: int = 42) -> None:
        self.seed = seed

    def run(self, state: RepairState, deps: Deps) -> RegressionResult:
        baseline = usable_baseline(state)
        result = RegressionResult()
        if baseline is None:
            result.failure_reason = "missing, incomplete or mismatched baseline"
            return result
        result.baseline_commit = baseline.base_commit
        result.baseline_hash = digest(baseline.model_dump(mode="json"))
        result.flaky = set(baseline.flaky)
        # Re-sorting node ids reorders session-scoped fixtures: a test that
        # moves a shared directory can run before the tests that read it, and
        # those failures are not regressions at all. ``outcomes`` is sorted for
        # stable storage; the first run keeps pytest's collection order.
        source_order = baseline.run_outcomes[0] if baseline.run_outcomes else baseline.outcomes
        authorized = list(source_order)
        seen = set(authorized)
        # The first run gives pytest's collection order. Later runs can
        # discover a node the first run did not reach; dropping those would make
        # "complete" mean "the first run's subset", not the full baseline.
        for mapping in baseline.run_outcomes[1:]:
            for node in mapping:
                if node not in seen:
                    seen.add(node)
                    authorized.append(node)
        for node in baseline.outcomes:
            if node not in seen:
                seen.add(node)
                authorized.append(node)
        result.unexecuted_tests = list(authorized)
        if state.execution is None or state.execution.status is not ExecutionStatus.PASS:
            result.failure_reason = "L1 has not passed"
            return result
        for node in authorized:
            validate_test_target(deps.validator.policy, node)
        result.executions["L1"] = state.execution
        result.levels_run.append(RegressionLevel.TARGET)
        related = state.impact.related_tests if state.impact else []
        patch = generate_git_diff(deps.workspace, state.base_commit, policy=deps.validator.policy)
        parents = {PurePosixPath(name).parent for name in patch.files}
        affected = [
            n
            for n in authorized
            if n in related
            or n.split("::")[0] in related
            or PurePosixPath(n.split("::")[0]).parent in parents
        ]

        def run_scope(key: str, scope: list[str]) -> ExecutionResult:
            def action() -> ExecutionResult:
                live = state.model_copy(update={"test_runs": deps.test_runs, "usage": deps.usage})
                stop = deps.budget.check(live, Activity.TEST)
                pending = pending_review_calls(
                    state,
                    candidate_digest=(
                        patch_hash(state.current_patch) if state.current_patch else ""
                    ),
                )
                remaining = (
                    state.budget.max_runtime_seconds
                    - deps.budget.elapsed_seconds(state)
                    - pending * REVIEW_TIME_RESERVE_SECONDS
                )
                if stop or remaining < 1:
                    return ExecutionResult(
                        status=ExecutionStatus.TIMEOUT, error_reason=str(stop or "budget_time")
                    )
                spec = deps.env_spec.model_copy(
                    update={
                        "timeout_seconds": min(
                            deps.env_spec.timeout_seconds, max(1, int(remaining))
                        )
                    }
                )
                files = list(dict.fromkeys(node.split("::", 1)[0] for node in scope))
                all_file_nodes = {node for node in authorized if node.split("::", 1)[0] in files}
                file_scope = (
                    key in {"L2", "L3"}
                    and deps.full_regression_file_scope
                    and set(scope) == all_file_nodes
                )
                dispatch = (
                    list(dict.fromkeys(node.split("::", 1)[0] for node in scope))
                    if file_scope
                    else scope
                )
                execution = deps.sandbox.run_tests(
                    workspace=deps.workspace,
                    environment=deps.environment,
                    env_spec=spec,
                    tests=dispatch,
                    artifact_iteration=1000 + deps.test_runs,
                )
                return execution.model_copy(
                    update={
                        "diagnostics": {
                            **execution.diagnostics,
                            "regression_dispatch": "full_files" if file_scope else "nodes",
                            "regression_dispatch_targets": dispatch,
                            "authorized_node_count": len(scope),
                        }
                    }
                )

            execution = execute_once(
                state,
                deps,
                f"{key}-{digest(sorted(scope))[:12]}",
                ExecutionResult,
                action,
            )
            deps.artifacts.write_json(
                f"regression/{state.iteration}/{key}.json",
                execution.model_dump(mode="json"),
                redact_values=True,
            )
            return execution

        def inspect(key: str, scope: list[str]) -> bool:
            execution = run_scope(key, scope)
            result.executions[key] = execution
            observed = {t.node_id: t.outcome for t in execution.tests}
            result.executed_tests = sorted(
                set(result.executed_tests) | (set(observed) & set(authorized))
            )
            result.unexecuted_tests = sorted(set(authorized) - set(result.executed_tests))
            if (execution.status is ExecutionStatus.PASS and execution.failed_tests) or (
                execution.status is ExecutionStatus.FAIL and not execution.failed_tests
            ):
                result.failure_reason = "execution status contradicts per-test outcomes"
                return False
            if execution.status not in {ExecutionStatus.PASS, ExecutionStatus.FAIL} or set(
                observed
            ) != set(scope):
                result.status = execution.status.value
                result.failure_reason = "execution failed or test scope mismatch"
                return False
            result.status_changes.update(
                {
                    node: (baseline.outcomes[node].value, outcome.value)
                    for node, outcome in observed.items()
                    if node in baseline.outcomes and baseline.outcomes[node] != outcome
                }
            )
            # A file-level L1 target only includes baseline PASS nodes. Tests
            # that failed on base and now pass are still independent evidence
            # that the issue was fixed, even though they cannot be an L1 target.
            result.fixed_tests = sorted(
                {
                    node
                    for node, (before, after) in result.status_changes.items()
                    if before in {TestOutcome.FAILED.value, TestOutcome.ERROR.value}
                    and after == TestOutcome.PASSED.value
                }
            )
            result.existing_failures = sorted(
                set(result.existing_failures)
                | {
                    t.node_id
                    for t in execution.failed_tests
                    if baseline.outcomes.get(t.node_id) in {TestOutcome.FAILED, TestOutcome.ERROR}
                }
            )
            # L3 is a mandatory run unless L2 itself covered the whole
            # authorized baseline (in which case its evidence *is* L3).  The
            # retry budget is only what remains after that reservation.
            l3_owed = full_regression_runs(state) < MAX_FULL_REGRESSION_RUNS and not (
                authorized and set(authorized) <= set(affected)
            )
            allowance = retry_allowance(state, deps, full_level_expected=l3_owed)
            for node in new_failures(baseline, execution):
                if node in result.flaky:
                    continue
                if allowance < 2:
                    result.new_failures.append(node)
                    result.retry_notes.append(
                        f"{node}: flaky classification declined; only {allowance} run(s) "
                        "left after reserving the deterministic closure"
                    )
                    continue
                retries = []
                for attempt in range(MAX_NEW_FAILURE_RETRIES):
                    if allowance <= 0:
                        break
                    retry = run_scope(f"{key}-retry-{digest(node)[:16]}-{attempt}", [node])
                    allowance -= 1
                    retries.append(retry)
                    result.retries[node] = retries
                    if retry.status not in {ExecutionStatus.PASS, ExecutionStatus.FAIL} or [
                        t.node_id for t in retry.tests
                    ] != [node]:
                        result.status = retry.status.value
                        result.failure_reason = "flaky retry execution failed"
                        return False
                if len(retries) < 2:
                    result.new_failures.append(node)
                    result.retry_notes.append(
                        f"{node}: only {len(retries)} of 2 re-runs were affordable; "
                        "kept as a new failure"
                    )
                    continue
                if any(r.tests[0].outcome is TestOutcome.PASSED for r in retries):
                    result.flaky.add(node)
                elif all(
                    r.tests[0].outcome in {TestOutcome.FAILED, TestOutcome.ERROR} for r in retries
                ):
                    result.new_failures.append(node)
                else:
                    result.failure_reason = "flaky retry skipped"
                    return False
            return not result.has_regression

        if affected:
            result.levels_run.append(RegressionLevel.IMPACT)
            if not inspect("L2", affected):
                return result
        full_runs = full_regression_runs(state)
        if affected and set(authorized) <= set(affected):
            # L2 already executed the entire baseline scope. A second "full"
            # pass would re-run the same tests against the same patch and
            # produce the same evidence — the Sphinx Phase 20 run spent runs
            # exactly this way. Completion still requires (a) L2 actually ran,
            # (b) it passed, (c) every authorized node got a result, and (d) the
            # authorized list had no duplicate that a set comparison would hide.
            l2 = result.executions.get("L2")
            covered = set(result.executed_tests) == set(authorized)
            if (
                l2 is None
                or l2.status not in {ExecutionStatus.PASS, ExecutionStatus.FAIL}
                or result.unexecuted_tests
                or not covered
                or len(authorized) != len(set(authorized))
            ):
                result.failure_reason = (
                    "L2 covered every authorized test but its evidence is incomplete"
                )
                return result
            if full_runs >= MAX_FULL_REGRESSION_RUNS:
                result.failure_reason = "L3 full-run limit reached"
                return result
            result.levels_run.append(RegressionLevel.FULL)
            result.complete = True
            result.status = "completed"
            return result
        if full_runs >= MAX_FULL_REGRESSION_RUNS:
            result.failure_reason = "L3 full-run limit reached"
            return result
        remaining = state.budget.max_runtime_seconds - deps.budget.elapsed_seconds(state)
        estimate = (
            baseline.duration_seconds
            if baseline.duration_seconds is not None
            else deps.env_spec.timeout_seconds
        )
        scope = authorized
        key = "L3"
        if estimate > remaining:
            candidates = sorted(set(authorized) - set(affected))
            scope = sorted(
                random.Random(self.seed).sample(candidates, math.ceil(len(candidates) * 0.2))
            )
            result.mode = RegressionMode.SAMPLED
            result.sampled_ratio = len(scope) / len(candidates) if candidates else 0
            result.seed = self.seed
            key = "L3-sampled"
        if not scope:
            result.failure_reason = "no regression scope executed"
            return result
        if key == "L3":
            result.levels_run.append(RegressionLevel.FULL)
        if not inspect(key, scope):
            return result
        if key == "L3" and result.unexecuted_tests:
            result.failure_reason = "L3 did not cover every authorized baseline node"
            return result
        result.complete = True
        result.status = "completed"
        return result
