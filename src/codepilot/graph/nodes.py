"""完整迭代修复状态机的节点（02 §15.2、G-01、G-05、G-09、G-11）。

约定（03 §5）：

* 每个节点都是 ``async def node(state: RepairState, deps: Deps) -> dict``，
  **只返回它更新的字段**。返回整份 State 会让"哪个节点改了哪个字段"失去归属，
  checkpoint 的差异也无从审计。
* 节点不读全局对象，一切来自 ``deps``。
* 大字段（完整日志、完整检索结果）只存 Artifact 引用，不进 State。
* 异常一律在节点边界被分类成结构化终态，**不会**被转换成假成功。

## Attempt 的追加与"重放"

历史只追加、不覆盖（G-11）。但中断恢复时同一个节点可能被重新执行一次（崩溃发生
在节点内部、Checkpoint 尚未写入），所以追加必须可重放：

``validate_patch`` 只在"当前补丁还没有对应的未完成 Attempt"时追加；
``run_target_tests`` 只在"当前补丁没有已确认的测试证据"时执行。

这不是锦上添花的幂等性设计，而是 G-13 的验收点本身：恢复后重复追加同一轮、
重复烧一次 Sandbox 都要能被测试抓到。
"""

from __future__ import annotations

from typing import Any

from codepilot.artifacts import DIAGNOSIS_JSON, EXECUTION_JSON, PATCH_DIFF, VALIDATION_JSON
from codepilot.failure.compressor import compress_failures
from codepilot.failure.signature import execution_signatures
from codepilot.graph.budget import Activity
from codepilot.graph.deps import Deps, DepsConfigurationError
from codepilot.graph.progress import build_progress_snapshot
from codepilot.graph.repair_policy import RepairPolicy
from codepilot.graph.routing import (
    Node,
    reproduction_target_tests,
    selected_target_tests,
)
from codepilot.graph.stop_rules import patch_hash
from codepilot.observability.events import EventType
from codepilot.observability.redaction import redact_text
from codepilot.patch.diff import generate_git_diff
from codepilot.schemas.enums import (
    ExecutionStatus,
    RepairStrategy,
    RunStatus,
    StopReason,
)
from codepilot.schemas.execution import ExecutionResult, TestBaseline
from codepilot.schemas.localization import (
    FileDecision,
    LocalizationResult,
    SymbolDecision,
)
from codepilot.schemas.reproduction import ReproductionResult, ReproductionStatus
from codepilot.schemas.retrieval import RankedFile, RankedSymbol
from codepilot.schemas.state import Attempt, RepairState
from codepilot.tools.write_tools import validate_test_target

__all__ = [
    "analyze_impact",
    "build_env",
    "check_budget",
    "diagnose",
    "finalize",
    "generate_patch",
    "localize",
    "plan",
    "repair_patch",
    "reproduce",
    "retrieve",
    "review",
    "run_regression",
    "run_target_tests",
    "understand_issue",
    "validate_patch",
]

_DEFERRED = "DEFERRED:phase9-not-implemented"

_policy = RepairPolicy()


# --------------------------------------------------------------------- 公共件


_RESERVED_EVENT_KEYS = frozenset(
    {"node", "type", "status", "duration_ms", "usage", "tool_calls", "error", "summary"}
)


def _emit(
    state: RepairState,
    deps: Deps,
    node: str,
    status: str,
    *,
    type: EventType = EventType.NODE_END,
    error: str | None = None,
    **extra: Any,
) -> list[dict[str, Any]]:
    """写事件：JSONL/DB 走 EventSink，State 里留一条精简轨迹。

    两边都写是有意的：State 里的事件让报告与 checkpoint 自解释，而 EventSink
    带 seq / 时间戳 / 用量，是恢复和 API SSE 的数据源。只留一边要么让报告失真，
    要么让运行期观测不到。

    ``extra`` 里禁止出现 :data:`_RESERVED_EVENT_KEYS`：那些名字是 ``emit`` 自己的
    参数，同名传进来会**静默顶掉**参数而不是落到 ``extra``——第一次踩到时表现为
    ``tool_calls=len(...)`` 把事件的工具列表变成一个整数。
    """
    clash = _RESERVED_EVENT_KEYS.intersection(extra)
    if clash:
        raise TypeError(f"reserved EventSink kwargs used as extras: {sorted(clash)}")
    deps.events.emit(node=node, type=type, status=status, error=error, **extra)
    entry: dict[str, Any] = {
        "seq": len(state.events) + 1,
        "node": node,
        "status": status,
        "error": error,
    }
    if extra:
        entry["extra"] = redact_text(str(sorted(extra.items())))
    return [*state.events, entry]


def _terminal(
    state: RepairState,
    deps: Deps,
    node: str,
    status: RunStatus,
    stop_reason: StopReason,
    reasons: list[str],
    **fields: Any,
) -> dict[str, Any]:
    """把节点写成一个可审计的终态：状态 + 停止原因 + 失败说明 + 事件。"""
    return {
        **fields,
        "status": status,
        "stop_reason": stop_reason,
        "failure_reasons": [*state.failure_reasons, *reasons],
        "events": _emit(state, deps, node, f"{status.value}:{stop_reason.value}"),
    }


def _budget_guard(
    state: RepairState,
    deps: Deps,
    activity: Activity,
    node: Node,
) -> dict[str, Any] | None:
    """节点入口预算检查（G-05）。耗尽时返回终态更新，节点立即 return。

    放在**每个**可能产生 LLM 调用、Sandbox 执行、Workspace 修改或补丁生成的节点
    入口，而不只放在修复循环里（02 §16："Check the budget before every node"）。
    """
    evaluation = deps.budget.evaluate(state, activity)
    deps.events.emit(
        node=node.value,
        type=EventType.BUDGET_CHECK,
        status="exhausted" if evaluation.is_exhausted else "ok",
        summary=str(evaluation.summary()),
        activity=activity.value,
    )
    if evaluation.primary is None:
        return None
    reason = evaluation.primary
    status = RunStatus.BUDGET_EXHAUSTED
    message = f"{node.value}: budget exhausted ({reason.value}); usage={evaluation.summary()}"
    return {
        "status": status,
        "stop_reason": reason,
        "failure_reasons": [*state.failure_reasons, message],
        "usage": deps.usage,
        "test_runs": deps.test_runs,
        "events": _emit(state, deps, node.value, f"blocked:{reason.value}"),
    }


def _identity(state: RepairState, deps: Deps) -> None:
    """节点入口的身份核对。

    并发 benchmark 里拿错 Deps（A 任务的 LLM 计数跑进 B 任务的 run）会写出混合
    状态的 checkpoint，而且几乎无法事后诊断，所以在节点入口直接抛错。
    """
    if state.run_id != deps.run_id:
        raise DepsConfigurationError(
            f"Deps 绑定 run_id={deps.run_id!r}，但状态属于 run_id={state.run_id!r}"
        )


def _next_iteration(state: RepairState) -> int:
    return len(state.attempts) + 1


def _workspace_patch(deps: Deps, state: RepairState) -> str:
    """Workspace 相对 base commit 的规范补丁（复现测试已在 diff 层剔除）。"""
    return generate_git_diff(
        deps.require_workspace(),
        state.base_commit,
        policy=deps.validator.policy,
    ).text


def _replayed_attempt(state: RepairState, current_hash: str) -> Attempt | None:
    """重放判定：当前补丁已有一条"还没拿到测试证据"的 Attempt 时复用它。"""
    last = state.current_attempt
    if last is not None and last.patch_hash == current_hash and last.execution is None:
        return last
    return None


def _append(state: RepairState, attempt: Attempt) -> list[Attempt]:
    """只追加；同一轮次绝不重复写入（G-11 第 7 条）。"""
    if any(item.iteration == attempt.iteration for item in state.attempts):
        return list(state.attempts)
    return [*state.attempts, attempt]


def _update_last(state: RepairState, attempt: Attempt) -> list[Attempt]:
    """更新**当前**（最后一条）Attempt。历史轮次由 ``_append`` 保护，永不出现在这里。"""
    return [*state.attempts[:-1], attempt]


# --------------------------------------------------------------------- 分析链


async def understand_issue(state: RepairState, deps: Deps) -> dict[str, Any]:
    """Issue → 结构化 ``IssueAnalysis``（Phase 3 C-03）。"""
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.LLM, Node.UNDERSTAND_ISSUE)
    if blocked is not None:
        return blocked
    files = [item.path for item in deps.index.files()]
    analysis = deps.issue_analyzer.analyze(
        state.issue, repo_files=files, repo_root=deps.require_workspace()
    )
    return {
        "issue_analysis": analysis,
        "usage": deps.usage,
        "events": _emit(state, deps, Node.UNDERSTAND_ISSUE.value, "ok"),
    }


async def retrieve(state: RepairState, deps: Deps) -> dict[str, Any]:
    """多通道检索。完整候选只进 Snapshot，State 里只留引用与摘要。"""
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.LLM, Node.RETRIEVE)
    if blocked is not None:
        return blocked
    result = deps.retriever(state.issue)
    return {
        "retrieval": result,
        "usage": deps.usage,
        "events": _emit(state, deps, Node.RETRIEVE.value, "ok", candidates=len(result.symbols)),
    }


async def localize(state: RepairState, deps: Deps) -> dict[str, Any]:
    """文件级 → symbol 级定位（Phase 3 C-09～C-11）。"""
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.LLM, Node.LOCALIZE)
    if blocked is not None:
        return blocked
    if state.issue_analysis is None or state.retrieval is None:
        return _terminal(
            state,
            deps,
            Node.LOCALIZE.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["localize requires issue analysis and retrieval"],
        )
    result = deps.localizer.localize(state.issue_analysis, state.retrieval)
    return {
        "localization": result,
        "usage": deps.usage,
        "events": _emit(
            state,
            deps,
            Node.LOCALIZE.value,
            "ok",
            files=len(result.files),
            symbols=len(result.symbols),
        ),
    }


async def analyze_impact(state: RepairState, deps: Deps) -> dict[str, Any]:
    """影响面分析（纯规则，不产出补丁也不执行代码）。"""
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.LLM, Node.ANALYZE_IMPACT)
    if blocked is not None:
        return blocked
    if state.localization is None:
        return _terminal(
            state,
            deps,
            Node.ANALYZE_IMPACT.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["analyze_impact requires a localization result"],
        )
    impact = deps.impact_analyzer.analyze(state.localization)
    return {
        "impact": impact,
        "events": _emit(
            state, deps, Node.ANALYZE_IMPACT.value, "ok", impacted=len(impact.changed_symbols)
        ),
    }


# ------------------------------------------------------------------ 环境与复现


async def build_env(state: RepairState, deps: Deps) -> dict[str, Any]:
    """环境镜像（命中缓存或构建）+ base commit 上的两次全量测试基线。

    基线是回归判定的参照物（03 §14），因此它的两次执行**计入** ``max_test_runs``：
    不算的话 12 次的预算实际会被执行 14 次。
    """
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.TEST, Node.BUILD_ENV)
    if blocked is not None:
        return blocked
    if not deps.environment.succeeded:
        return {
            "status": RunStatus.SANDBOX_ERROR,
            "stop_reason": StopReason.SANDBOX_FAILURE,
            "failure_reasons": [
                f"environment build failed: {deps.environment.error or deps.environment.status.value}"
            ],
            "test_runs": deps.test_runs,
            "events": _emit(state, deps, Node.BUILD_ENV.value, "environment_error"),
        }
    if state.budget.max_test_runs - deps.test_runs < 2:
        return _terminal(
            state,
            deps,
            Node.BUILD_ENV.value,
            RunStatus.BUDGET_EXHAUSTED,
            StopReason.BUDGET_TEST_RUNS,
            ["baseline requires two test runs"],
        )
    baseline: TestBaseline = _build_baseline(deps, state)
    return {
        "baseline": baseline,
        "test_runs": deps.test_runs,
        "events": _emit(
            state,
            deps,
            Node.BUILD_ENV.value,
            "ok",
            complete=baseline.complete,
            tests=len(baseline.outcomes),
            flaky=len(baseline.flaky),
        ),
    }


def _build_baseline(deps: Deps, state: RepairState) -> TestBaseline:
    from codepilot.sandbox.baseline import build_test_baseline
    from codepilot.sandbox.env_spec import EnvSpec

    def before_run() -> EnvSpec | ExecutionResult:
        live = state.model_copy(update={"test_runs": deps.test_runs, "usage": deps.usage})
        stop = deps.budget.check(live, Activity.TEST)
        remaining = state.budget.max_runtime_seconds - deps.budget.elapsed_seconds(state)
        if stop is not None or remaining < 1:
            return ExecutionResult(
                status=ExecutionStatus.TIMEOUT, error_reason=str(stop or StopReason.BUDGET_TIME)
            )
        return deps.env_spec.model_copy(
            update={"timeout_seconds": min(deps.env_spec.timeout_seconds, max(1, int(remaining)))}
        )

    return build_test_baseline(
        deps.sandbox,
        base_commit=state.base_commit,
        workspace=deps.require_workspace(),
        environment=deps.environment,
        env_spec=deps.env_spec,
        before_run=before_run,
    )


async def reproduce(state: RepairState, deps: Deps) -> dict[str, Any]:
    """复现测试生成与校验（Phase 6）。复现失败降级到已有测试，不终止修复。

    ``ENV_ERROR`` 是例外：环境坏了的时候重试只会重复拿到同一个坏结果，
    因此按既有 Sandbox 故障路由直接终止（02 §10.5）。
    """
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.LLM, Node.REPRODUCE)
    if blocked is not None:
        return blocked
    if deps.reproducer is None:
        # 没有复现能力是**如实记录**，不是伪造：SKIPPED 表示"未尝试"。
        return {
            "reproduction": ReproductionResult(status=ReproductionStatus.SKIPPED),
            "test_runs": deps.test_runs,
            "events": _emit(state, deps, Node.REPRODUCE.value, "skipped:no-reproducer"),
        }
    if state.issue_analysis is None:
        return _terminal(
            state,
            deps,
            Node.REPRODUCE.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["reproduce requires an issue analysis"],
        )
    fallback = tuple(state.plan.tests_to_run) if state.plan else ()
    outcome = deps.reproducer.reproduce(
        issue_text=state.issue,
        analysis=state.issue_analysis,
        localization=state.localization,
        retrieval=state.retrieval,
        fallback_tests=fallback,
    )
    result = outcome.result
    if result.status is ReproductionStatus.ENV_ERROR:
        return {
            "reproduction": result,
            "status": RunStatus.SANDBOX_ERROR,
            "stop_reason": StopReason.SANDBOX_FAILURE,
            "failure_reasons": [
                f"reproduce environment failure: {result.fail_reason or result.kind}"
            ],
            "test_runs": deps.test_runs,
            "usage": deps.usage,
            "events": _emit(state, deps, Node.REPRODUCE.value, "environment_error"),
        }
    return {
        "reproduction": result,
        "test_runs": deps.test_runs,
        "usage": deps.usage,
        "events": _emit(
            state,
            deps,
            Node.REPRODUCE.value,
            result.status.value,
            attempts=result.attempts,
            degraded=result.degraded,
        ),
    }


async def plan(state: RepairState, deps: Deps) -> dict[str, Any]:
    """规划（Planner 只读，不改文件 —— 03 §10）。"""
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.LLM, Node.PLAN)
    if blocked is not None:
        return blocked
    if state.issue_analysis is None or state.localization is None or state.impact is None:
        return _terminal(
            state,
            deps,
            Node.PLAN.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["plan requires issue analysis, localization and impact"],
        )
    repair_plan = deps.planner.plan(
        issue_text=state.issue,
        issue_analysis=state.issue_analysis,
        localization=state.localization,
        impact=state.impact,
        retrieval=state.retrieval,
    )
    return {
        "plan": repair_plan,
        "usage": deps.usage,
        "events": _emit(
            state,
            deps,
            Node.PLAN.value,
            "ok",
            changes=len(repair_plan.changes),
            tests=len(repair_plan.tests_to_run),
        ),
    }


# --------------------------------------------------------------- 补丁生成与校验


def _authorized_targets(deps: Deps, state: RepairState) -> list[str]:
    """把目标测试名过一遍 Workspace 路径策略，越界的直接拒。"""
    targets: list[str] = []
    for raw in selected_target_tests(state):
        try:
            if (
                state.baseline is not None
                and not reproduction_target_tests(state)
                and not any(n == raw or n.startswith(raw + "::") for n in state.baseline.outcomes)
            ):
                continue
            targets.append(validate_test_target(deps.validator.policy, raw))
        except ValueError:
            continue
    return targets


def _run_coder(
    state: RepairState,
    deps: Deps,
    round_plan: Any,
    node: str,
) -> dict[str, Any]:
    """执行一次 Coding Agent 工具循环并把补丁从 Workspace 读回来。

    真实文件修改只发生在 ``deps.workspace``（授权 Workspace），由工具层的
    :class:`WorkspacePathPolicy` 约束；原始仓库从不被写。
    """
    if state.issue_analysis is None:
        raise ValueError(f"{node} requires an issue analysis")
    result = deps.coder.run(
        issue_text=state.issue,
        issue_analysis=state.issue_analysis,
        plan=round_plan,
        retrieval=state.retrieval,
    )
    patch = _workspace_patch(deps, state)
    calls = [item.model_dump(mode="json") for item in result.tool_calls]
    update: dict[str, Any] = {
        "current_patch": patch,
        "tool_calls": calls,
        "usage": deps.usage,
        "test_runs": deps.test_runs,
        "events": _emit(state, deps, node, result.stop_reason, tool_call_count=len(calls)),
    }
    # 工具/ token 上限由 Coding Agent 内部触发，必须映射成权威 StopReason，
    # 不能只留一个字符串标签就继续跑（G-08 的"不能静默忽略"）。
    if result.stop_reason == "tool_limit":
        return {
            **update,
            "status": RunStatus.BUDGET_EXHAUSTED,
            "stop_reason": StopReason.BUDGET_TOOL_CALLS,
            "failure_reasons": [
                *state.failure_reasons,
                f"{node}: per-iteration tool call budget exhausted "
                f"({len(calls)} calls, limit {deps.coder.max_tool_calls})",
            ],
        }
    if result.stop_reason == "token_budget":
        return {
            **update,
            "status": RunStatus.BUDGET_EXHAUSTED,
            "stop_reason": StopReason.BUDGET_TOKENS,
            "failure_reasons": [
                *state.failure_reasons,
                f"{node}: token budget exhausted inside the coding loop",
            ],
        }
    if not patch and any(not item.ok and item.name != "finish" for item in result.tool_calls):
        return {
            **update,
            "status": RunStatus.TOOL_ERROR,
            "stop_reason": StopReason.TOOL_ERROR,
            "failure_reasons": [
                *state.failure_reasons,
                f"{node}: every editing tool call failed",
            ],
        }
    return update


async def generate_patch(state: RepairState, deps: Deps) -> dict[str, Any]:
    """首轮补丁生成。首轮**不计入** ``max_iterations``（口径见 budget.py）。"""
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.CODEGEN, Node.GENERATE_PATCH)
    if blocked is not None:
        return blocked
    if state.plan is None:
        return _terminal(
            state,
            deps,
            Node.GENERATE_PATCH.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["generate_patch requires a plan"],
        )
    return _run_coder(state, deps, state.plan, Node.GENERATE_PATCH.value)


async def validate_patch(state: RepairState, deps: Deps) -> dict[str, Any]:
    """补丁校验并**追加** Attempt（G-11）。无效补丁不会送去执行（路由负责）。"""
    _identity(state, deps)
    if state.plan is None:
        return _terminal(
            state,
            deps,
            Node.VALIDATE_PATCH.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["validate_patch requires a plan"],
        )
    current = state.current_patch
    digest = patch_hash(current)
    replayed = _replayed_attempt(state, digest)
    tests = list(state.plan.tests_to_run) if state.plan else []
    validation = deps.validator.validate(related_tests=tests, impact=state.impact)
    if replayed is not None:
        # 同一轮的重放：保留原 Attempt，不重复追加。
        attempt = replayed
        attempts = _update_last(state, attempt)
        outcome = "replayed"
    else:
        attempt = Attempt(
            iteration=_next_iteration(state),
            patch_diff=redact_text(current),
            patch_hash=digest,
            validation=validation,
        )
        attempts = _append(state, attempt)
        outcome = "passed" if validation.passed else "rejected"
    events = _emit(
        state,
        deps,
        Node.VALIDATE_PATCH.value,
        outcome,
        iteration=attempt.iteration,
        errors=len(validation.errors),
    )
    if validation.passed:
        return {"attempts": attempts, "events": events}
    return {
        "attempts": attempts,
        "failure_reasons": [*state.failure_reasons, *validation.errors],
        "events": events,
    }


# ------------------------------------------------------------------- 测试执行


def _artifact_evidence_valid(deps: Deps, execution: ExecutionResult) -> bool:
    """已记录的 PASS 是否仍有可读的持久化证据。

    Checkpoint 里写着"测试通过"并不等于证据还在：Artifact 可能被清理或损坏。
    没有可验证证据时**不能**把这条 PASS 当作可复用的结论（G-13 第 10 条）。
    """
    if not execution.artifact_dir:
        return False
    return deps.artifacts.exists(execution.artifact_dir)


async def run_target_tests(state: RepairState, deps: Deps) -> dict[str, Any]:
    """执行目标测试、解析失败、写签名与进度快照到当前 Attempt。"""
    _identity(state, deps)
    attempt = state.current_attempt
    if attempt is None:
        return _terminal(
            state,
            deps,
            Node.RUN_TARGET_TESTS.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["run_target_tests requires a validated attempt"],
        )
    if (
        deps.evidence_trusted
        and attempt.execution is not None
        and attempt.patch_hash == patch_hash(state.current_patch)
        and _artifact_evidence_valid(deps, attempt.execution)
    ):
        # 崩溃恢复：这一轮已经跑过并有可读证据，绝不重复执行、绝不重复计费。
        return {
            "execution": attempt.execution,
            "events": _emit(
                state,
                deps,
                Node.RUN_TARGET_TESTS.value,
                f"reused:{attempt.execution.status.value}",
                iteration=attempt.iteration,
            ),
        }
    blocked = _budget_guard(state, deps, Activity.TEST, Node.RUN_TARGET_TESTS)
    if blocked is not None:
        return blocked
    targets = _authorized_targets(deps, state)
    if not targets:
        # 没有可执行的目标测试：**不伪造**"目标测试通过"。
        return {
            **_terminal(
                state,
                deps,
                Node.RUN_TARGET_TESTS.value,
                RunStatus.FAILED,
                StopReason.TESTS_FAILED,
                [
                    "no authorized target test available: the patch was never executed, "
                    "so repair cannot be self-assessed"
                ],
            ),
            "test_runs": deps.test_runs,
        }
    execution = deps.sandbox.run_tests(
        workspace=deps.require_workspace(),
        environment=deps.environment,
        env_spec=deps.env_spec,
        tests=targets,
        artifact_iteration=attempt.iteration,
    )
    # 立刻把执行结果落成 Artifact，而不是等 finalize。
    #
    # 崩溃恢复要核对的是"这轮 PASS 到底有没有外部证据"，而证据必须在该轮结束时
    # 就存在。只在 finalize 写的话，一次崩溃的 run 一个执行 Artifact 都没有，
    # verify_recovery 只能判"证据缺失"，把已经做成的事重跑一遍。
    # Sandbox 自己没给 artifact_dir 时（脚本化后端）用这里写下的路径补上，
    # 让 fake 与真实后端遵守同一条证据契约。
    execution_path = deps.artifacts.attempt_relpath(attempt.iteration, EXECUTION_JSON)
    deps.artifacts.write_json(execution_path, execution.model_dump(mode="json"))
    if not execution.artifact_dir:
        execution = execution.model_copy(update={"artifact_dir": execution_path})
    deps.events.emit(
        node=Node.RUN_TARGET_TESTS.value,
        type=EventType.SANDBOX_RUN,
        status=execution.status.value,
        extra={"iteration": attempt.iteration, "tests": len(targets)},
    )
    failures = deps.failure_parser.parse_execution(execution).failures
    classified = [
        item.model_copy(update={"category": deps.failure_classifier.classify(item).category})
        for item in failures
    ]
    signatures = execution_signatures(classified)
    snapshot = build_progress_snapshot(
        execution,
        signatures=signatures,
        regression=state.regression,
    )
    updated = attempt.model_copy(
        update={
            "execution": execution,
            "failure_signatures": signatures,
            "progress": snapshot,
        }
    )
    fields: dict[str, Any] = {
        "execution": execution,
        "attempts": _update_last(state, updated),
        "test_runs": deps.test_runs,
        "events": _emit(
            state,
            deps,
            Node.RUN_TARGET_TESTS.value,
            execution.status.value,
            iteration=attempt.iteration,
            failed=execution.tests_failed,
        ),
    }
    if execution.is_environment_problem:
        # 环境问题直接终止（02 §13.4）；OOM 已在 Phase 4 归为 SANDBOX_ERROR。
        return {
            **fields,
            "status": RunStatus.SANDBOX_ERROR,
            "stop_reason": StopReason.SANDBOX_FAILURE,
            "failure_reasons": [
                *state.failure_reasons,
                f"execution ended with {execution.status.value}: "
                f"{execution.error_reason or 'unspecified'}",
            ],
        }
    return fields


# ------------------------------------------------------ Phase 9 接缝（不得伪造）


async def run_regression(state: RepairState, deps: Deps) -> dict[str, Any]:
    """Execute baseline-relative regression; legacy dependencies remain explicitly deferred."""
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.TEST, Node.RUN_REGRESSION)
    if blocked is not None and deps.regression_runner is None:
        return blocked
    if deps.regression_runner is None:
        return {
            "events": _emit(state, deps, Node.RUN_REGRESSION.value, _DEFERRED),
            "report_artifacts": {**state.report_artifacts, "regression": _DEFERRED},
        }
    regression = deps.regression_runner.run(state, deps)
    regression_attempt = state.current_attempt
    if regression.has_regression and regression_attempt is not None:
        failures = [
            t
            for e in regression.executions.values()
            for t in e.tests
            if t.node_id in regression.new_failures
        ]
        evidence = ExecutionResult(
            status=ExecutionStatus.FAIL, tests=failures, tests_failed=len(failures)
        )
        parsed = deps.failure_parser.parse_execution(evidence).failures
        classified = [
            f.model_copy(update={"category": deps.failure_classifier.classify(f).category})
            for f in parsed
        ]
        signatures = execution_signatures(classified)
        regression_attempt = regression_attempt.model_copy(
            update={
                "failure_signatures": sorted(signatures),
                "progress": build_progress_snapshot(
                    evidence, signatures=signatures, regression=regression
                ),
            }
        )
    fields: dict[str, Any] = {}
    if regression_attempt is not None:
        fields["attempts"] = _update_last(
            state, regression_attempt.model_copy(update={"regression": regression})
        )
    if any(e.is_environment_problem for e in regression.executions.values()) or any(
        e.is_environment_problem for retries in regression.retries.values() for e in retries
    ):
        fields.update(status=RunStatus.SANDBOX_ERROR, stop_reason=StopReason.SANDBOX_FAILURE)
    budget_errors = [
        e.error_reason
        for e in [
            *regression.executions.values(),
            *(e for retries in regression.retries.values() for e in retries),
        ]
        if e.error_reason and e.error_reason.startswith("budget_")
    ]
    if budget_errors:
        fields.update(status=RunStatus.BUDGET_EXHAUSTED, stop_reason=StopReason(budget_errors[0]))
    return {
        **fields,
        "regression": regression,
        "test_runs": deps.test_runs,
        "events": _emit(
            state, deps, Node.RUN_REGRESSION.value, "ok", new_failures=len(regression.new_failures)
        ),
    }


async def review(state: RepairState, deps: Deps) -> dict[str, Any]:
    """Run independent review after completed regression with bounded high-risk repair."""
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.LLM, Node.REVIEW)
    if blocked is not None and deps.reviewer is None:
        return blocked
    if deps.reviewer is None:
        return {
            "events": _emit(state, deps, Node.REVIEW.value, _DEFERRED),
            "report_artifacts": {**state.report_artifacts, "review": _DEFERRED},
        }
    if state.regression is not None and (
        not state.regression.complete or state.regression.has_regression
    ):
        return {}
    from codepilot.agents.reviewer import ReviewBudgetError

    try:
        result = deps.reviewer.review(state, deps)
    except ReviewBudgetError as exc:
        return _terminal(
            state,
            deps,
            Node.REVIEW.value,
            RunStatus.BUDGET_EXHAUSTED,
            exc.reason,
            ["review budget exhausted before model call"],
        )
    fields: dict[str, Any] = {}
    if state.current_attempt is not None:
        fields["attempts"] = _update_last(
            state, state.current_attempt.model_copy(update={"review": result})
        )
    return {
        **fields,
        "usage_uncertain": state.usage_uncertain or not result.usage_known,
        "review": result,
        "usage": deps.usage,
        "events": _emit(
            state, deps, Node.REVIEW.value, "approved" if result.approved else "changes-requested"
        ),
    }


# ------------------------------------------------------------------- 失败诊断


async def diagnose(state: RepairState, deps: Deps) -> dict[str, Any]:
    """解析 → 分类 → 压缩 → 诊断 → 生效策略（Phase 7 交接）。

    ``Diagnosis.confidence`` 不参与任何后续分支（02 §14.6）；策略可以被
    :class:`RepairPolicy` 的强制规则改写，模型无法否决（G-10）。
    """
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.LLM, Node.DIAGNOSE)
    if blocked is not None:
        return blocked
    attempt = state.current_attempt
    execution = state.execution
    if attempt is None or execution is None:
        return _terminal(
            state,
            deps,
            Node.DIAGNOSE.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["diagnose requires a current attempt with an execution result"],
        )
    if state.regression is not None and state.regression.has_regression:
        failed = [
            t
            for e in state.regression.executions.values()
            for t in e.tests
            if t.node_id in state.regression.new_failures
        ]
        execution = ExecutionResult(
            status=ExecutionStatus.FAIL, tests=failed, tests_failed=len(failed)
        )
    failures = deps.failure_parser.parse_execution(execution).failures
    if not failures:
        # 超时 / OOM / 收集错误可能完全没有逐用例失败记录。没有真实失败证据就
        # 不能凭空产出一个 Diagnosis（Phase 7 的 DiagnosisAgent 对此直接抛错）。
        return {
            "failure_reasons": [
                *state.failure_reasons,
                f"diagnose skipped: execution status {execution.status.value} "
                "produced no per-test failure evidence",
            ],
            "usage": deps.usage,
            "events": _emit(
                state, deps, Node.DIAGNOSE.value, f"no-failures:{execution.status.value}"
            ),
        }
    classified = [
        item.model_copy(update={"category": deps.failure_classifier.classify(item).category})
        for item in failures
    ]
    compressed = compress_failures(
        classified,
        target_tests=set(selected_target_tests(state)),
        reproduction_tests=set(reproduction_target_tests(state)),
    )
    repro_nodes = set(reproduction_target_tests(state))
    diagnosis = deps.diagnoser.diagnose(
        classified,
        compressed=compressed,
        issue_analysis=state.issue_analysis,
        localization=state.localization,
        plan=state.plan,
        current_patch=state.current_patch,
        base_commit=state.base_commit,
        reproduction_tests=repro_nodes,
        artifact_iteration=attempt.iteration,
    )
    decision = _policy.decide(state, diagnosis)
    effective = diagnosis.model_copy(update={"repair_strategy": decision.effective})
    updated = attempt.model_copy(update={"diagnosis": effective})
    events = _emit(
        state,
        deps,
        Node.DIAGNOSE.value,
        decision.effective.value,
        proposed=decision.proposed.value,
        forced=decision.forced,
        suspected=len(effective.suspected_symbols),
        rationale=decision.reason,
    )
    fields: dict[str, Any] = {
        "attempts": _update_last(state, updated),
        "usage": deps.usage,
        "events": events,
    }
    if decision.overridden:
        # 模型原始提议与生效策略不同：两者都要留下，否则审计时只能看到结果。
        fields["report_artifacts"] = {
            **state.report_artifacts,
            f"policy_override_{attempt.iteration}": (
                f"proposed={decision.proposed.value} effective={decision.effective.value}"
            ),
        }
    return fields


# --------------------------------------------------------------- 预算与再修复


async def check_budget(state: RepairState, deps: Deps) -> dict[str, Any]:
    """停止判定。本节点**不**调用 LLM / Sandbox / 文件修改。

    它同时是三条边的汇合点（无效补丁、诊断之后、复审要求修改之后），所以
    ``validation_loop`` 与 ``repeated_patch`` 这类需要"跨过一轮"才能数出来的
    规则都在这里有唯一的判定点。
    """
    decision = deps.rules.evaluate(state)
    deps.events.emit(
        node=Node.CHECK_BUDGET.value,
        type=EventType.BUDGET_CHECK,
        status="stop" if decision.should_stop else "continue",
        summary=", ".join(reason.value for reason in decision.all_reasons) or "none",
    )
    if not decision.should_stop:
        return {
            "events": _emit(state, deps, Node.CHECK_BUDGET.value, "continue"),
        }
    primary = decision.primary
    status = deps.rules.status_for(decision)
    if primary is None or status is None:  # should_stop 已保证两者都在
        return {"events": _emit(state, deps, Node.CHECK_BUDGET.value, "continue")}
    reasons = [f"{Node.CHECK_BUDGET.value}: {item}" for item in decision.explanations]
    events = _emit(state, deps, Node.CHECK_BUDGET.value, f"stop:{primary.value}")
    deps.events.emit(node=Node.CHECK_BUDGET.value, type=EventType.STOP, status=primary.value)
    return {
        "status": status,
        "stop_reason": decision.primary,
        "failure_reasons": [*state.failure_reasons, *reasons],
        "events": events,
    }


async def repair_patch(state: RepairState, deps: Deps) -> dict[str, Any]:
    """按生效策略真正改变 Workspace 或下一轮输入，然后再生成补丁（G-09）。

    三种策略都必须有**可观测差异**，只返回一个不同的字符串不算实现：

    * ``refine``：保留当前改动与失败反馈；
    * ``revert_and_retry``：隔离 Workspace 回退到 base commit，重新规划并带上
      Planner 的 ``alternatives``；
    * ``expand_scope``：按诊断给出的 symbol 重新做 symbol 级定位。
    """
    _identity(state, deps)
    blocked = _budget_guard(state, deps, Activity.REPAIR, Node.REPAIR_PATCH)
    if blocked is not None:
        return blocked
    attempt = state.current_attempt
    diagnosis = attempt.diagnosis if attempt is not None else None
    strategy = diagnosis.repair_strategy if diagnosis is not None else RepairStrategy.REFINE

    events = state.events
    fields: dict[str, Any] = {}
    round_plan = state.plan
    if round_plan is None:
        return _terminal(
            state,
            deps,
            Node.REPAIR_PATCH.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["repair_patch requires a plan"],
        )

    def record(status: str, **extra: Any) -> list[dict[str, Any]]:
        """把策略动作写进 State 事件（同时进 EventSink）。"""
        deps.events.emit(
            node=Node.REPAIR_PATCH.value, type=EventType.NODE_END, status=status, **extra
        )
        entry: dict[str, Any] = {
            "seq": len(events) + 1,
            "node": Node.REPAIR_PATCH.value,
            "status": status,
            "error": extra.get("error"),
        }
        if extra:
            entry["extra"] = redact_text(str(sorted(extra.items())))
        return [*events, entry]

    if strategy is RepairStrategy.REVERT_AND_RETRY:
        reverted, reason = _revert_workspace(deps)
        events = record(
            "workspace_reverted" if reverted else "workspace_revert_failed",
            error=reason,
            base_commit=state.base_commit,
        )
        round_plan = _replan_with_alternatives(state, deps, round_plan)
    elif strategy is RepairStrategy.EXPAND_SCOPE:
        expanded, added, dropped = _expand_scope(state, deps)
        events = record(
            "scope_expanded",
            added=sorted(added),
            dropped=sorted(dropped),
            snapshot=expanded.retrieval_snapshot_id,
        )
        fields["localization"] = expanded
        round_plan = _with_feedback(round_plan, diagnosis)
    else:
        # refine：不动 Workspace，但把失败反馈写进下一轮计划的可信上下文。
        round_plan = _with_feedback(round_plan, diagnosis)

    if state.review is not None and state.review.has_high_issues:
        if state.review_repairs >= 1:
            return _terminal(
                state,
                deps,
                Node.REPAIR_PATCH.value,
                RunStatus.STOPPED,
                StopReason.TESTS_FAILED,
                ["unresolved high review issues"],
            )
        fields["review_repairs"] = 1
        if round_plan is not None:
            round_plan = round_plan.model_copy(
                update={
                    "root_cause_hypothesis": round_plan.root_cause_hypothesis
                    + " Review: "
                    + state.review.model_dump_json()
                }
            )
    fields.update(regression=None, review=None)
    base_state = state.model_copy(update={"events": events, **fields})
    produced = _run_coder(base_state, deps, round_plan, Node.REPAIR_PATCH.value)
    merged: dict[str, Any] = {**produced, **fields}
    if round_plan is not state.plan:
        merged["plan"] = round_plan
    return merged


def _revert_workspace(deps: Deps) -> tuple[bool, str | None]:
    """只回退**本任务的隔离 Workspace** 到 base commit（03 §28）。

    绝不触碰开发者正在工作的 Git 工作树：``deps.workspace`` 是
    ``WorkspaceManager`` 从只读缓存派生出来的 per-run 目录。
    """
    if deps.workspace_manager is None:
        return False, "no WorkspaceManager injected; refusing to guess how to revert"
    try:
        deps.workspace_manager.reset(deps.require_workspace())
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, None


def _replan_with_alternatives(state: RepairState, deps: Deps, current: Any) -> Any:
    """回退后重新规划：把 Planner 的 ``alternatives`` 与失败历史作为可信上下文喂回。"""
    if state.issue_analysis is None or state.localization is None or state.impact is None:
        return current
    history = [
        f"iteration {attempt.iteration}: {attempt.execution.status.value if attempt.execution else 'no execution'}"
        for attempt in state.attempts
    ]
    context = "\n".join(
        [
            "Repair context (revert_and_retry): the workspace has been reset to the base commit.",
            f"Previously attempted hypothesis: {current.root_cause_hypothesis}",
            "Untried alternatives from the planner:",
            *(f"- {item}" for item in current.alternatives),
            "Failed iterations so far:",
            *(f"- {item}" for item in history),
        ]
    )
    return deps.planner.plan(
        issue_text=f"{state.issue}\n\n{context}",
        issue_analysis=state.issue_analysis,
        localization=state.localization,
        impact=state.impact,
        retrieval=state.retrieval,
    )


def _with_feedback(round_plan: Any, diagnosis: Any) -> Any:
    """把诊断结论与失败反馈并入计划的可信上下文（refine / expand_scope）。"""
    if diagnosis is None:
        return round_plan
    evidence = [diagnosis.root_cause, *diagnosis.suspected_symbols]
    return round_plan.model_copy(
        update={
            "evidence": [
                *round_plan.evidence,
                *(item for item in evidence if item not in round_plan.evidence),
            ]
        }
    )


def _expand_scope(
    state: RepairState, deps: Deps
) -> tuple[LocalizationResult, list[str], list[str]]:
    """按诊断疑似 symbol 重新做 symbol 级定位。

    新增 symbol **必须在索引里真实存在**：模型编造一个不存在的名字不能扩大范围，
    也不能把上下文引到仓库外。原始定位的检索快照引用保持不变（``retrieval_snapshot_id``
    不改写），新增项与丢弃项都写进事件。
    """
    base = state.localization
    diagnosis = state.current_attempt.diagnosis if state.current_attempt else None
    if base is None or diagnosis is None:
        return (
            base or LocalizationResult(),
            [],
            list(diagnosis.suspected_symbols if diagnosis else []),
        )
    known = {item.id: item for item in deps.index.symbol_objects()}
    indexed_files = {item.path: item for item in deps.index.files()}
    # 成对维护"候选 + 对应决策"：LocalizationResult 的校验器要求 file_decisions
    # 与 files、symbol_decisions 与 symbols **同序且一一对应**。分开追加会造出一个
    # 自相矛盾的定位结果，然后在模型生成补丁之前先炸在校验器里。
    file_pairs: list[tuple[RankedFile, FileDecision]] = [
        (item, next(decision for decision in base.file_decisions if decision.path == item.path))
        for item in base.files
    ]
    symbol_pairs: list[tuple[RankedSymbol, SymbolDecision]] = list(
        zip(base.symbols, base.symbol_decisions, strict=True)
    )
    added: list[str] = []
    dropped: list[str] = []
    for wanted in diagnosis.suspected_symbols:
        symbol = known.get(wanted)
        if symbol is None:
            # 索引里没有这个名字：模型不能凭空把范围扩到仓库外。
            dropped.append(f"{wanted}（不在索引里）")
            continue
        if symbol.id in {item.symbol_id for item, _ in symbol_pairs}:
            dropped.append(f"{symbol.id}（已在定位结果内）")
            continue
        if len(symbol_pairs) >= 5:
            dropped.append(f"{symbol.id}（symbol 上限 5）")
            continue
        if not 1 <= symbol.start_line <= symbol.end_line:
            dropped.append(f"{symbol.id}（索引行号无效）")
            continue
        file_node = indexed_files.get(symbol.path)
        if file_node is None:
            dropped.append(f"{symbol.id}（文件不在索引里）")
            continue
        if symbol.path not in {item.path for item, _ in file_pairs}:
            if len(file_pairs) >= 3:
                dropped.append(f"{symbol.id}（file 上限 3）")
                continue
            file_pairs.append(
                (
                    RankedFile(
                        path=symbol.path,
                        score=0.0,
                        top_symbols=[symbol.qualified_name],
                        reason=f"suspected by diagnosis: {symbol.qualified_name}",
                    ),
                    FileDecision(
                        candidate_id=file_node.id,
                        path=symbol.path,
                        reason=f"scope expansion from diagnosis: {symbol.qualified_name}",
                    ),
                )
            )
        symbol_pairs.append(
            (
                RankedSymbol(
                    symbol_id=symbol.id,
                    qualified_name=symbol.qualified_name,
                    file=symbol.path,
                    score=0.0,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    reason=f"suspected by diagnosis: {symbol.qualified_name}",
                ),
                SymbolDecision(
                    symbol_id=symbol.id,
                    file=symbol.path,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    file_line_count=max(file_node.line_count, symbol.end_line, 1),
                    reason=f"scope expansion from diagnosis symbol {symbol.qualified_name}",
                ),
            )
        )
        added.append(symbol.id)
    expanded = base.model_copy(
        update={
            "files": [item for item, _ in file_pairs],
            "file_decisions": [decision for _, decision in file_pairs],
            "symbols": [item for item, _ in symbol_pairs],
            "symbol_decisions": [decision for _, decision in symbol_pairs],
        }
    )
    return expanded, added, dropped


# ------------------------------------------------------------------- 收尾


async def finalize(state: RepairState, deps: Deps) -> dict[str, Any]:
    """写 Artifact、事件与 Run Report，并释放可关闭资源。

    **本节点没有预算守卫**（G-05）：预算耗尽时如果连报告都写不出来，运行就变成
    了"花了钱但没有任何证据"。清理与报告必须始终可执行。

    成功判定是保守的：只有"补丁通过校验 **且** 目标测试真的执行过并 PASS"才给
    ``provisionally_verified``；回归与复审未执行时写进报告的边界说明不可省略，
    本轮绝不宣称 M2 完成。
    """
    from codepilot.report import RunReport, write_run_report

    # 先继承前序节点写入的引用（含 run_regression / review 的 DEFERRED 标记）。
    # 不继承的话，finalize 返回的 report_artifacts 会整体覆盖 state 里的那份，
    # "回归与复审未执行"这条边界信息就在报告里消失了。
    paths: dict[str, str] = dict(state.report_artifacts)
    for record in state.attempts:
        paths[f"patch:{record.iteration}"] = deps.artifacts.attempt_relpath(
            record.iteration, PATCH_DIFF
        )
        deps.artifacts.write_text(paths[f"patch:{record.iteration}"], record.patch_diff)
        paths[f"validation:{record.iteration}"] = deps.artifacts.attempt_relpath(
            record.iteration, VALIDATION_JSON
        )
        deps.artifacts.write_json(
            paths[f"validation:{record.iteration}"],
            record.validation.model_dump(mode="json"),
        )
        if record.execution is not None:
            key = f"execution:{record.iteration}"
            paths[key] = deps.artifacts.attempt_relpath(record.iteration, EXECUTION_JSON)
            deps.artifacts.write_json(paths[key], record.execution.model_dump(mode="json"))
        if record.diagnosis is not None:
            key = f"diagnosis:{record.iteration}"
            paths[key] = deps.artifacts.attempt_relpath(record.iteration, DIAGNOSIS_JSON)
            deps.artifacts.write_json(paths[key], record.diagnosis.model_dump(mode="json"))

    status = state.status
    stop_reason = state.stop_reason
    reasons = list(state.failure_reasons)
    if status is RunStatus.RUNNING:
        # 一路走到 finalize 却没有终态：按真实证据判定，而不是默认成功。
        last_attempt = state.current_attempt
        execution = state.execution
        if last_attempt is None or not last_attempt.validation.passed:
            status = RunStatus.PATCH_REJECTED
            stop_reason = StopReason.PATCH_REJECTED
            reasons.append("finalize: no validator-approved patch was produced")
        elif execution is None:
            status = RunStatus.FAILED
            stop_reason = StopReason.TESTS_FAILED
            reasons.append("finalize: target tests were never executed")
        elif state.regression is not None and (
            not state.regression.complete or state.regression.has_regression
        ):
            status = RunStatus.FAILED
            stop_reason = StopReason.TESTS_FAILED
            reasons.append("regression incomplete or confirmed new failures")
        elif state.review is not None and state.review.has_high_issues:
            status = RunStatus.STOPPED
            stop_reason = StopReason.TESTS_FAILED
            reasons.append("unresolved high review issues")
        elif (
            execution.status is ExecutionStatus.PASS
            and state.regression is not None
            and state.regression.complete
            and state.review is not None
        ):
            status = RunStatus.SUCCEEDED
            stop_reason = None
        elif execution.status is ExecutionStatus.PASS:
            status = RunStatus.PROVISIONALLY_VERIFIED
            stop_reason = None
            reasons.append(
                "finalize: target tests passed; regression and review were NOT executed "
                f"({_DEFERRED}), so this is not M2 verification"
            )
        else:
            status = RunStatus.FAILED
            stop_reason = StopReason.TESTS_FAILED
            reasons.append(f"finalize: target tests ended as {execution.status.value}")

    interim = state.model_copy(
        update={"status": status, "stop_reason": stop_reason, "failure_reasons": reasons}
    )
    patch = generate_git_diff(
        deps.require_workspace(),
        state.base_commit,
        policy=deps.validator.policy,
        delivery=True,
        new_tests_reviewed=state.review is not None and not state.review.has_high_issues,
    )
    paths["final_patch"] = "patch_final.diff"
    deps.artifacts.write_text(paths["final_patch"], patch.text)
    paths["state"] = "state_final.json"
    report_state = interim.model_copy(update={"report_artifacts": paths})
    report = RunReport.from_state(
        report_state,
        repository=deps.repository,
        git_diff=patch.text,
        modified_files=patch.files,
        simulated_llm=deps.simulated_llm,
        artifacts=paths,
    )
    from codepilot.tools.base import is_test_path

    report.added_tests = {
        name: "trusted task authorization; reviewed; not used as independent success evidence"
        for name in patch.files
        if is_test_path(name) and not deps.validator._base_exists(name)
    }
    report.excluded_tests = [name for name in patch.excluded_files if is_test_path(name)]
    report_paths = write_run_report(report, deps.artifacts, report_state)
    merged = {**paths, **report_paths}
    # 最后再写一次 state：report_artifacts 要包含报告自身的路径，否则报告里的
    # 链接指向一个还不存在的文件。
    deps.artifacts.write_json(
        paths["state"],
        interim.model_copy(update={"report_artifacts": merged}).model_dump(mode="json"),
        redact_values=True,
    )
    events = _emit(interim, deps, Node.FINALIZE.value, status.value, attempts=len(state.attempts))
    deps.events.emit(
        node=Node.FINALIZE.value,
        type=EventType.STOP if stop_reason is not None else EventType.NODE_END,
        status=stop_reason.value if stop_reason is not None else status.value,
    )
    return {
        "status": status,
        "stop_reason": stop_reason,
        "failure_reasons": reasons,
        "report_artifacts": merged,
        "usage": deps.usage,
        "test_runs": deps.test_runs,
        "events": events,
    }
