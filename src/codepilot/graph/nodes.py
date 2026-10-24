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

import re
from typing import Any

from codepilot.artifacts import DIAGNOSIS_JSON, EXECUTION_JSON, PATCH_DIFF, VALIDATION_JSON
from codepilot.failure.compressor import compress_failures
from codepilot.failure.signature import execution_signatures
from codepilot.graph.budget import (
    RESERVATION_BEARING_ACTIVITIES,
    Activity,
    ClosureReservation,
    ClosureTestPlan,
    outstanding_in_budget_currency,
)
from codepilot.graph.deps import Deps, DepsConfigurationError
from codepilot.graph.progress import build_progress_snapshot
from codepilot.graph.regression import (
    MAX_FULL_REGRESSION_RUNS,
    full_regression_runs,
    usable_baseline,
)
from codepilot.graph.repair_policy import RepairPolicy
from codepilot.graph.routing import (
    Node,
    pending_review_calls,
    reproduction_target_tests,
    review_preconditions,
    selected_target_tests,
)
from codepilot.graph.stop_rules import patch_hash
from codepilot.graph.target_tests import changed_files, select_target_tests
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
from codepilot.tools.base import PathAccessError, WorkspacePathPolicy, is_test_path
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


def _outstanding_cost(state: RepairState, deps: Deps) -> tuple[float, ClosureReservation | None]:
    """派发层**已预扣但未结算**的费用（换算到预算币种）。

    返回 ``(金额, 额外预留或 None)``。第二项非 ``None`` 时代表"这笔预扣换算不到预算
    币种"：金额未知，而硬费用上限已经设了 —— 按既有的 fail-closed 口径，此时**先于
    审查花钱的活动**一律按"预留付不起"处理（:class:`ClosureReservation` 的
    ``estimated=False`` 分支），而不是拿 ``convert()`` 缺汇率时的 1.0 硬套一个方向
    可能反了七倍的数。
    """
    raw = deps.outstanding_cost()
    rates = deps.llm_config.exchange_rates if deps.llm_config is not None else None
    converted = outstanding_in_budget_currency(
        raw, target=state.budget.budget_currency, exchange_rates=rates
    )
    if converted is not None:
        return converted, None
    if state.budget.max_cost is None:
        return 0.0, None
    return 0.0, ClosureReservation(
        tokens=0,
        cost=float("inf"),
        review_calls=0,
        estimated=False,
        basis="outstanding-currency-unknown",
    )


def _closure_reservation(state: RepairState, deps: Deps) -> ClosureReservation:
    """闭环还欠着审查时，为它留出额度；不欠了就是 0（预留不得永久占用预算）。

    这是**统一预算准入**用到的那份预留：编码派发与 Diagnose 之类的前置模型调用共用
    同一个口径，节点里不再各自算钱。``reviewer_available`` 取"这次 run 到底接没接
    Reviewer"，``pending_review_calls`` 取状态机里还欠几次审查。
    """
    return deps.budget.closure_reservation(
        state,
        reviewer_available=deps.reviewer is not None,
        pending_review_calls=pending_review_calls(
            state, candidate_digest=patch_hash(state.current_patch)
        ),
        reviewer_config=deps.llm_config,
        simulated_provider=deps.simulated_llm,
    )


def _budget_guard(
    state: RepairState,
    deps: Deps,
    activity: Activity,
    node: Node,
    *,
    reservation: ClosureReservation | None = None,
) -> dict[str, Any] | None:
    """节点入口预算检查（G-05）。耗尽时返回终态更新，节点立即 return。

    放在**每个**可能产生 LLM 调用、Sandbox 执行、Workspace 修改或补丁生成的节点
    入口，而不只放在修复循环里（02 §16："Check the budget before every node"）。

    ``reservation`` 只由编码派发显式传入（它还要同一份数字去算循环内天花板）。
    其余"排在审查之前的模型调用"（:data:`RESERVATION_BEARING_ACTIVITIES`）在这里
    **自动**取同一份闭环预留：首轮真实运行之后剩下的缺口正是这一条 —— 编码让了路，
    Diagnose 没让，审查的预留照样被花光。
    预留把"验证与审查必须留下的额度"从本局可用上限里扣掉，于是预算不足在**派发前**
    就阻断，而不是等跑到 Reviewer 才发现没钱。
    预留按哪个口径估的（``basis``）必须一起写进事件与阻断消息：同一个"预算不足"
    可能是单价缺失、币种对不上，也可能只是真的花完了，三种情况的修法完全不同。

    ``outstanding`` 是派发层已预扣、尚未写进 ``state.usage`` 的费用：它先加进费用
    读数，再扣预留，同一段钱不会被算两遍。
    """
    outstanding, unknown = _outstanding_cost(state, deps)
    if activity in RESERVATION_BEARING_ACTIVITIES:
        if unknown is not None:
            # 预扣换算不出来：不知道它占掉多少钱时，任何估算的预留都不如"把剩余
            # 余额整段视为预留"保守，所以直接用它（fail closed）。
            reservation = unknown
        elif reservation is None:
            reservation = _closure_reservation(state, deps)
    evaluation = deps.budget.evaluate(
        state, activity, reservation=reservation, outstanding=outstanding
    )
    observed: dict[str, Any] = {}
    if reservation is not None and reservation.review_calls:
        observed["closure_reservation"] = reservation.summary()
    deps.events.emit(
        node=node.value,
        type=EventType.BUDGET_CHECK,
        status="exhausted" if evaluation.is_exhausted else "ok",
        summary=str(evaluation.summary()),
        activity=activity.value,
        **observed,
    )
    if evaluation.primary is None:
        return None
    reason = evaluation.primary
    status = RunStatus.BUDGET_EXHAUSTED
    withheld = ""
    if evaluation.reserved:
        withheld = "; reserved for the verification/review closure: " + ", ".join(
            f"{category.value}={amount:g}" for category, amount in evaluation.reserved
        )
        if reservation is not None:
            withheld += f" (estimated={reservation.estimated}, basis={reservation.basis})"
    message = f"{node.value}: budget exhausted ({reason.value}); usage={evaluation.summary()}"
    return {
        "status": status,
        "stop_reason": reason,
        "failure_reasons": [*state.failure_reasons, message + withheld],
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
    scope = _baseline_scope(state, deps.validator.policy)
    baseline: TestBaseline = _build_baseline(deps, state, scope)
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
            scope=len(scope),
        ),
    }


def _baseline_scope(state: RepairState, policy: WorkspacePathPolicy) -> tuple[str, ...]:
    """Impact-related existing test files used as the baseline's explicit scope.

    An empty tuple keeps the official test command's full range. The scope is
    filtered through the same path/test-target policy as any executable target;
    repository files such as ``conftest.py`` and ``__init__.py`` are excluded.
    """
    impact = state.impact
    if impact is None:
        return ()
    candidates = [*impact.related_tests, *(item.path for item in impact.related_test_evidence)]
    valid: set[str] = set()
    for raw in candidates:
        path = str(raw).split("::", 1)[0].strip()
        if not path or path.rsplit("/", 1)[-1] in {"__init__.py", "conftest.py"}:
            continue
        try:
            policy.resolve(path, must_exist=True)
            validate_test_target(policy, path)
        except (PathAccessError, ValueError, OSError):
            continue
        if is_test_path(path):
            valid.add(path)
    return tuple(sorted(valid))


def _build_baseline(deps: Deps, state: RepairState, scope: tuple[str, ...]) -> TestBaseline:
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
        tests=scope,
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
    """规划（Planner 只读，不改文件 —— 03 §10）。

    语义校验在 Planner 内部与 Schema 共用同一个有界纠正环；这里只负责把
    **审计**写进 state/artifact：工具调用轨迹与纠正轮数。二者缺失时报告只能
    显示"0 次"，而事实是"没采集"。
    """
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
    try:
        repair_plan = deps.planner.plan(
            issue_text=state.issue,
            issue_analysis=state.issue_analysis,
            localization=state.localization,
            impact=state.impact,
            retrieval=state.retrieval,
        )
    finally:
        # 失败的那次运行更需要工具证据：计划被拒时如果只留下 ``plan_semantic_rejections.jsonl``
        # 而没有 ``plan_tools.json``，事后就看不出模型到底查过什么才写出这个计划。
        audit = deps.planner.write_audit()
    corrections = int(getattr(deps.planner, "last_plan_corrections", 0))
    semantic_rejections = int(getattr(deps.planner, "last_semantic_rejections", 0))
    schema_rejections = int(getattr(deps.planner, "last_schema_rejections", 0))
    return {
        "plan": repair_plan,
        "plan_tools": audit,
        "plan_corrections": state.plan_corrections + corrections,
        "plan_semantic_rejections": state.plan_semantic_rejections + semantic_rejections,
        "plan_schema_rejections": state.plan_schema_rejections + schema_rejections,
        "usage": deps.usage,
        "events": _emit(
            state,
            deps,
            Node.PLAN.value,
            "ok",
            changes=len(repair_plan.changes),
            tests=len(repair_plan.tests_to_run),
            planned_new_tests=len(repair_plan.planned_new_tests),
            plan_corrections=corrections,
            plan_semantic_rejections=semantic_rejections,
            plan_schema_rejections=schema_rejections,
            tool_audit=audit.summary(),
        ),
    }


# --------------------------------------------------------------- 补丁生成与校验


_TRACEBACK_FILE = re.compile(r'File\s+"([^"]+)"')
_SOURCE_FRAME = re.compile(r"(?m)^([^\s:]+\.py):(\d+)(?::|$)")
_COLLECTING_FILE = re.compile(r"(?m)^ERROR collecting ([^\s:]+\.py)")


def _structured_error_paths(text: str) -> list[str]:
    """Paths pytest itself named in a traceback/collection header.

    Substring matching would attribute an incidental ``path.py`` mention in an
    unrelated stack to the patch. Only structured frames are evidence.
    """
    paths: list[str] = []
    for pattern in (_TRACEBACK_FILE, _SOURCE_FRAME, _COLLECTING_FILE):
        paths.extend(match.group(1) for match in pattern.finditer(text))
    return paths


def _patched_file_mentioned(text: str, changed: tuple[str, ...]) -> bool:
    """Whether a traceback *frame* points exactly at one of the changed files."""
    changed_set = {path.replace("\\", "/").lstrip("./") for path in changed}
    for raw in _structured_error_paths(text):
        path = raw.replace("\\", "/").lstrip("./")
        if path in changed_set:
            return True
        # Tracebacks usually carry the container-absolute form (/testbed/...).
        if any(path.endswith("/" + candidate) for candidate in changed_set):
            return True
    return False


def _target_execution_attribution(
    execution: ExecutionResult, changed: tuple[str, ...]
) -> tuple[str, str, str] | None:
    """COLLECTION_ERROR / ENV_ERROR 的归因；返回 ``(class, origin, reason)``。

    第三方依赖缺失与 Docker/环境故障属于基础设施；补丁导致的语法/导入错误属于
    模型。无法归到补丁的 collection 失败按基础设施处理，宁可承认没测到，也不把
    它算成模型修复失败。
    """
    if execution.status is ExecutionStatus.COLLECTION_ERROR:
        text = "\n".join(
            [
                execution.stdout_tail,
                execution.stderr_tail,
                execution.error_reason or "",
                *(f"{test.message or ''}\n{test.traceback or ''}" for test in execution.tests),
            ]
        )
        if _patched_file_mentioned(text, changed):
            return (
                "model_output_invalid",
                "model",
                "COLLECTION_ERROR caused by the patched code; entering diagnosis",
            )
        return (
            "workspace_error",
            "workspace",
            "COLLECTION_ERROR attribution UNKNOWN: no traceback frame points at a changed "
            "file; treated as an infrastructure failure, not as model-introduced",
        )
    if execution.status is ExecutionStatus.ENV_ERROR:
        return (
            "workspace_error",
            "workspace",
            f"ENV_ERROR before target tests could run: {execution.error_reason or 'unspecified'}",
        )
    if execution.status is ExecutionStatus.SANDBOX_ERROR:
        return (
            "workspace_error",
            "workspace",
            f"SANDBOX_ERROR during target tests: {execution.error_reason or 'unspecified'}",
        )
    return None


#: Coding Agent 内部触发的局部额度停止 → 权威 StopReason（G-08："不能静默忽略"）。
_LOCAL_CODING_STOPS: dict[str, StopReason] = {
    "tool_limit": StopReason.BUDGET_TOOL_CALLS,
    "token_budget": StopReason.BUDGET_TOKENS,
    "cost_budget": StopReason.BUDGET_COST,
}


def _run_coder(
    state: RepairState,
    deps: Deps,
    round_plan: Any,
    node: str,
    *,
    token_ceiling: int | None = None,
    cost_ceiling: float | None = None,
    test_run_ceiling: int | None = None,
) -> dict[str, Any]:
    """执行一次 Coding Agent 工具循环并把补丁从 Workspace 读回来。

    真实文件修改只发生在 ``deps.workspace``（授权 Workspace），由工具层的
    :class:`WorkspacePathPolicy` 约束；原始仓库从不被写。

    ## 局部额度耗尽不是这次运行的终点（Phase 11 场景 A / B）

    Coder 撞到每轮工具调用上限、或撞到本局 token/费用上限时：

    * **手上已有补丁** → 冻结这一版补丁（SHA-256 写进 ``frozen_patch_hash``），把
      原因记进 ``coding_stop_reason``，**不**写 ``status``。状态机继续走
      PatchValidator → 目标测试 → 回归 → Reviewer。局部额度只说明"不能再改代码了"，
      不说明"不需要验证了"。
    * **没有补丁** → 这才是真终态：``EMPTY_PATCH`` 写进失败原因，验证与审查都无从
      谈起，绝不能拿一个空补丁去骗一次成功判定。

    工具/ token 上限由 Coding Agent 内部触发，两种情况都映射成权威 StopReason，
    不能只留一个字符串标签。

    ``test_run_ceiling`` 是本轮**测试额度份额**（DA-024）：编码阶段自己可以调用几次
    ``run_tests``。它不是局部停止原因而是工具层的一次拒绝——被拦下的是"模型自己再跑一轮
    测试"，不是"这版补丁不再验证"，闭环那几次执行仍然由状态机保证会发生。
    """
    if state.issue_analysis is None:
        raise ValueError(f"{node} requires an issue analysis")
    result = deps.coder.run(
        issue_text=state.issue,
        issue_analysis=state.issue_analysis,
        plan=round_plan,
        retrieval=state.retrieval,
        token_ceiling=token_ceiling,
        cost_ceiling=cost_ceiling,
        test_run_ceiling=test_run_ceiling,
    )
    patch = _workspace_patch(deps, state)
    digest = patch_hash(patch)
    calls = [item.model_dump(mode="json") for item in result.tool_calls]
    local = _LOCAL_CODING_STOPS.get(result.stop_reason)
    update: dict[str, Any] = {
        "current_patch": patch,
        # 冻结「这一轮编码交出去的补丁版本」；空补丁没有可验证身份，写空串。
        "frozen_patch_hash": digest if patch else "",
        "tool_calls": calls,
        "coding_stop_reason": local,
        # 上一轮留下的审查缺口与本补丁无关：进入新的编码阶段即作废。
        "review_skip_reason": None,
        "usage": deps.usage,
        "test_runs": deps.test_runs,
        "events": _emit(state, deps, node, result.stop_reason, tool_call_count=len(calls)),
    }
    if local is None:
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
    if patch:
        return {
            **update,
            "failure_reasons": [
                *state.failure_reasons,
                f"{node}: coding stage stopped early on its local budget ({local.value}) "
                f"with a patch in hand; frozen {digest[:12]} for validation, tests and "
                f"review ({len(calls)} tool calls, limit {deps.coder.max_tool_calls})",
            ],
        }
    return {
        **update,
        "status": RunStatus.BUDGET_EXHAUSTED,
        "stop_reason": local,
        "failure_reasons": [
            *state.failure_reasons,
            f"{node}: coding stage stopped early on its local budget ({local.value}) "
            "and produced EMPTY_PATCH; there is nothing to validate, test or review",
        ],
    }


def _closure_test_plan(state: RepairState, deps: Deps) -> ClosureTestPlan:
    """这一版候选补丁的闭环还要几次测试执行、当时还剩几次（DA-024 的准入算式）。

    三个条件都**问即将真正执行的那段代码**，不在这里另写一套判据：

    * 目标测试：有效补丁现在一律进入 ``run_target_tests``（选择器可能从 impact/
      baseline/related 找到 Planner 没写的测试），因此每轮候选都按一次 L1 预留；
      真的没有合法目标时该节点会以 ``TARGET_TEST_UNAVAILABLE`` 终止，预留自然不用。
    * L2 / L3：只看 :func:`codepilot.graph.regression.usable_baseline`（回归自己的前置
      条件）与 L3 全量次数上限。**当前**这一版补丁测试还没跑过或跑成失败并不影响这一项
      ——修复轮要算的是"下一版候选"的闭环。
    * 抽样降级（``regression_mode=sampled``）同样是一次 ``run_tests``，所以准入不足
      **从来不是**把 FULL 改成 SAMPLED 的理由。

    计数用 ``deps.test_runs``（AccountingSandbox 的实时值），不是 ``state.test_runs``：
    后者只在节点返回时才落账，派发瞬间它是滞后的，用它会低估已经花掉的次数。
    """
    measured = usable_baseline(state) is not None
    return deps.budget.closure_test_plan(
        state,
        target_tests_planned=True,
        impact_level_expected=measured,
        full_level_expected=measured and full_regression_runs(state) < MAX_FULL_REGRESSION_RUNS,
        runs_spent=deps.test_runs,
    )


def _repair_trigger(state: RepairState) -> str:
    """这一轮修复是被谁要求的——准入读数里必须能看出触发方。

    ``review`` 与 ``diagnosis`` 的修法完全不同：前者是"审查提出 high、政策允许再修一轮"，
    后者是"测试证据不支持当前补丁"。``repair_patch`` 在派发前会把 ``review`` 清空，
    所以这里读到的永远是**触发本轮的那次**审查。
    """
    review = state.review
    if review is not None and review.has_high_issues:
        return "review"
    attempt = state.current_attempt
    if attempt is not None and attempt.diagnosis is not None:
        return "diagnosis"
    return "validation"


def _coding_stage(
    state: RepairState,
    deps: Deps,
    round_plan: Any,
    node: Node,
    *,
    trigger: str = "generation",
) -> dict[str, Any]:
    """派发一次编码：先把闭环额度留给验证与审查，再让 Coder 花钱。

    预留在这里生效三次，缺一次都会退回真实运行观察到的失败形状：

    1. **派发前判定（token/费用）**：剩余不足以保证闭环时，直接阻断这次编码派发，
       而不是等烧完钱才发现 Reviewer 没有预算。
    2. **循环内判定（token/费用）**：预留后的上限作为本局天花板传给 Coding Agent。节点
       入口只检查一次，而一次编码可能有几十次模型调用，只在入口检查等于把闭环额度
       交给编码阶段随意支配。
    3. **本轮测试份额（DA-024）**：``plan.coder_share`` 限制编码阶段自己能调用几次
       ``run_tests``。次数与闭环共用同一个计数器，不先扣出来就会重演 glm14：编码把最后
       几次花掉，第二轮候选的 L3 被拦在门外、审查从未发生。

    没有 Reviewer 时 token/费用预留为 0：审查会如实记录为未执行，不该为它空占额度。费用
    口径取 **Reviewer 实际绑定模型**的单价（``deps.llm_config``），不是账本平均单价——
    编码与审查用不同模型时，平均单价不是保守数字。
    """
    live = state.model_copy(update={"usage": deps.usage, "test_runs": deps.test_runs})
    reservation = _closure_reservation(live, deps)
    plan = _closure_test_plan(live, deps)
    record = plan.record(trigger=trigger)
    blocked = _budget_guard(live, deps, Activity.CODEGEN, node, reservation=reservation)
    deps.events.emit(
        node=node.value,
        type=EventType.BUDGET_CHECK,
        status="ok" if plan.feasible else "insufficient",
        summary=plan.detail(),
        activity=Activity.CODEGEN.value,
        closure_test_runs=record.summary(),
    )
    if blocked is not None:
        return {**blocked, "closure_admission": record}
    return {
        **_run_coder(
            state,
            deps,
            round_plan,
            node.value,
            token_ceiling=deps.budget.coding_token_ceiling(live, reservation),
            cost_ceiling=deps.budget.coding_cost_ceiling(live, reservation),
            test_run_ceiling=plan.coder_share,
        ),
        "closure_admission": record,
    }


async def generate_patch(state: RepairState, deps: Deps) -> dict[str, Any]:
    """首轮补丁生成。首轮**不计入** ``max_iterations``（口径见 budget.py）。

    预算守卫在 :func:`_coding_stage` 里：它必须带着闭环预留判定，否则"派发时没算
    上审查要花的额度"就会原样复发。
    """
    _identity(state, deps)
    if state.plan is None:
        return _terminal(
            state,
            deps,
            Node.GENERATE_PATCH.value,
            RunStatus.ERROR,
            StopReason.WORKFLOW_ERROR,
            ["generate_patch requires a plan"],
        )
    return _coding_stage(state, deps, state.plan, Node.GENERATE_PATCH)


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
    integrity: list[str] = []
    if state.frozen_patch_hash and digest != state.frozen_patch_hash:
        # 补丁在编码阶段之外被改动：冻结身份就是用来发现这件事的，绝不能悄悄拿一个
        # 没被编码阶段交出去的版本去验证。
        integrity.append(
            f"validate_patch: candidate {digest[:12]} does not match the patch frozen by the "
            f"coding stage ({state.frozen_patch_hash[:12]}); the coding stage must hand over "
            "every version that gets verified"
        )
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
        return {
            "attempts": attempts,
            "failure_reasons": [*state.failure_reasons, *integrity],
            "events": events,
        }
    return {
        "attempts": attempts,
        "failure_reasons": [*state.failure_reasons, *integrity, *validation.errors],
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
    selection = select_target_tests(
        state,
        policy=deps.validator.policy,
        index=deps.index,
        repo_graph=deps.repo_graph,
        baseline=state.baseline,
    )
    targets = list(selection.tests)
    if not targets:
        # 没有可执行的目标测试：**不伪造**"目标测试通过"。这是模型侧缺口，
        # 不是 Harness 崩溃；回归与审查保持 NOT EXECUTED。
        updated = attempt.model_copy(
            update={
                "target_test_source": selection.source,
                "target_tests": [],
                "target_test_baseline_failures": list(selection.baseline_failures),
            }
        )
        result = _terminal(
            state,
            deps,
            Node.RUN_TARGET_TESTS.value,
            RunStatus.FAILED,
            StopReason.TESTS_FAILED,
            [
                "TARGET_TEST_UNAVAILABLE: no_authorized_target_test; the patch was never "
                "executed, so repair cannot be self-assessed"
            ],
            attempts=_update_last(state, updated),
            test_runs=deps.test_runs,
        )
        result["error_class"] = "target_test_unavailable"
        result["error_origin"] = "model"
        result["events"] = _emit(
            state,
            deps,
            Node.RUN_TARGET_TESTS.value,
            "target_test_unavailable",
            error="no_authorized_target_test",
            target_test_source=selection.source,
            rejected=len(selection.rejected),
        )
        return result
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
        extra={
            "iteration": attempt.iteration,
            "tests": len(targets),
            "target_test_source": selection.source,
        },
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
            "target_test_source": selection.source,
            "target_tests": list(targets),
            "target_test_baseline_failures": list(selection.baseline_failures),
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
            target_test_source=selection.source,
        ),
    }
    attribution = _target_execution_attribution(execution, changed_files(state.current_patch))
    if attribution is not None:
        failure_class, origin, reason = attribution
        if origin == "model":
            # 补丁语法/导入错误可以纠正：保留执行证据，交给 diagnose 进入修复循环。
            return {
                **fields,
                "error_class": failure_class,
                "error_origin": origin,
                "failure_reasons": [*state.failure_reasons, reason],
            }
        # 第三方依赖、Docker 或 collection 环境故障：直接终止，不进入诊断。
        return {
            **fields,
            "status": RunStatus.SANDBOX_ERROR,
            "stop_reason": StopReason.SANDBOX_FAILURE,
            "error_class": failure_class,
            "error_origin": origin,
            "failure_reasons": [*state.failure_reasons, reason],
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
    """Run independent review after completed regression with bounded high-risk repair.

    三条不可让的规矩（Phase 11 §五）：

    * **前置条件不满足就不调用**。没有已批准补丁、目标测试没跑过或没通过、回归没测完
      时，让模型对着一句"我觉得修好了"产出一个 approved 比不调用有害得多；这种情况
      把原因写进 ``review_skip_reason``，报告里审查就是 NOT EXECUTED，而不是通过。
    * **结论绑定补丁身份**。审查结果盖上当前补丁的 SHA-256；同一补丁的重放复用已有
      结论（不重复调用模型、不重复计数、不重复记账），补丁换了版本则旧结论作废。
    * **额度独立**。审查用的是自己的 ``max_review_calls``，不是编码阶段剩下的工具次数
      ——否则 Coder 烧光 30 次工具调用就会把 Reviewer 一起冻结，正是首次真实运行的形状。

    判定顺序也是有意的：**先判能不能复用/该不该跳过，再判额度**。把额度检查放在最
    前面会让"同一版补丁早已审过"的重放被拦在门外，也会让一次没钱了的审查在
    Operation Journal 里留下一条没有结果的意图行（下次重进直接判"结果不确定"）。
    """
    _identity(state, deps)
    if deps.reviewer is None:
        return {
            "review_skip_reason": "no reviewer wired into this run",
            "events": _emit(state, deps, Node.REVIEW.value, _DEFERRED),
            "report_artifacts": {**state.report_artifacts, "review": _DEFERRED},
        }
    digest = patch_hash(state.current_patch)
    attempt = state.current_attempt
    recorded = attempt.review if attempt is not None else None
    if recorded is not None and recorded.reviewed_patch_hash == digest:
        # 同一版补丁已经有审查结论：重放时绝不重复调用模型，也不重复计审查次数。
        # 写回的是**绝对值**而不是 ``+= 0``：恢复场景下同一条边可能被再走一次，
        # 增量写法会把复用读成第二次审查。
        return {
            "review": recorded,
            "review_calls": state.review_calls,
            "review_skip_reason": None,
            "usage": deps.usage,
            "events": _emit(
                state, deps, Node.REVIEW.value, f"reused:{recorded.reviewed_iteration}"
            ),
        }
    unmet = review_preconditions(state)
    if unmet is not None:
        return {
            "review_skip_reason": unmet,
            "usage": deps.usage,
            "test_runs": deps.test_runs,
            "events": _emit(state, deps, Node.REVIEW.value, f"not-executed:{unmet}"),
        }
    blocked = _budget_guard(state, deps, Activity.REVIEW, Node.REVIEW)
    if blocked is not None:
        # 审查因预算拦下时，"未执行"必须写进 ``review_skip_reason``：报告里的
        # ``review_executed=False`` 只说"没审"，说不出是没审还是没钱审。留空会让
        # closure 写成 "the reviewer produced no result"，把预算问题读成接线问题。
        reason = blocked["stop_reason"]
        blocked["review_skip_reason"] = f"not executed: {reason.value} spent before any call"
        return blocked
    from codepilot.agents.reviewer import ReviewBudgetError

    try:
        result = deps.reviewer.review(state, deps)
    except ReviewBudgetError as exc:
        # 第二道闸：Reviewer 自己按"审查上下文实际占多少 token"再判一次（节点入口只
        # 看累计用量，看不到还没构造的上下文）。同样必须把未执行原因写清楚。
        return _terminal(
            state,
            deps,
            Node.REVIEW.value,
            RunStatus.BUDGET_EXHAUSTED,
            exc.reason,
            ["review budget exhausted before model call"],
            review_skip_reason=f"not executed: {exc.reason.value} spent before the model call",
        )
    stamped = result.model_copy(
        update={"reviewed_patch_hash": digest, "reviewed_iteration": state.iteration}
    )
    fields: dict[str, Any] = {}
    if attempt is not None:
        fields["attempts"] = _update_last(state, attempt.model_copy(update={"review": stamped}))
    return {
        **fields,
        "usage_uncertain": state.usage_uncertain or not stamped.usage_known,
        "review": stamped,
        "review_calls": state.review_calls + 1,
        "review_skip_reason": None,
        "usage": deps.usage,
        "events": _emit(
            state,
            deps,
            Node.REVIEW.value,
            "approved" if stamped.approved else "changes-requested",
            reviewed_patch_hash=digest[:12],
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


def _closure_refused(
    state: RepairState, deps: Deps, plan: ClosureTestPlan, *, trigger: str
) -> dict[str, Any]:
    """额度撑不起闭环时**不启动**修复轮：留在手里的那一版补丁与它的审查结论原样保留。

    这里刻意**不**清空 ``current_patch`` / ``review`` / ``regression``，也不写
    ``review_repairs``、不回退 Workspace、不重新规划、不向模型要任何东西：DA-024 要的
    结果不是"少修一轮"，而是"绝不产出一版注定无法完成验证的新候选"。已经挣到过一次完整
    闭环的补丁仍然是那一版，报告里它绑定的审查结论仍然成立；被拒掉的只是下一轮。

    ``RunStatus.BUDGET_EXHAUSTED`` 复用既有的类型化状态，而区分口径落在
    :attr:`StopReason.BUDGET_CLOSURE_TEST_RUNS` 上：``budget_test_runs`` 说的是
    "12 次已经花完"，这一条说的是"还剩 2 次，而闭环要 3 次"——后者的修法是提高额度或改变
    回归政策，前者的修法是这一轮本来就跑不动。留在 ``budget_*`` 家族里不是随手起名：
    评测侧的 ``over_budget`` / ``BUDGET_STOPPED`` 分桶按前缀判定，换成家族外的名字会让
    一次预算准入被读成"补丁无效"或"模型没修好"。
    """
    record = plan.record(trigger=trigger)
    message = f"{Node.REPAIR_PATCH.value}: {record.detail}"
    deps.events.emit(
        node=Node.REPAIR_PATCH.value,
        type=EventType.BUDGET_CHECK,
        status="refused",
        summary=record.detail,
        activity=Activity.REPAIR.value,
        closure_test_runs=record.summary(),
    )
    return {
        "closure_admission": record,
        "status": RunStatus.BUDGET_EXHAUSTED,
        "stop_reason": StopReason.BUDGET_CLOSURE_TEST_RUNS,
        "failure_reasons": [*state.failure_reasons, message],
        "usage": deps.usage,
        "test_runs": deps.test_runs,
        "events": _emit(
            state,
            deps,
            Node.REPAIR_PATCH.value,
            f"refused:{StopReason.BUDGET_CLOSURE_TEST_RUNS.value}",
        ),
    }


async def repair_patch(state: RepairState, deps: Deps) -> dict[str, Any]:
    """按生效策略真正改变 Workspace 或下一轮输入，然后再生成补丁（G-09）。

    三种策略都必须有**可观测差异**，只返回一个不同的字符串不算实现：

    * ``refine``：保留当前改动与失败反馈；
    * ``revert_and_retry``：隔离 Workspace 回退到 base commit，重新规划并带上
      Planner 的 ``alternatives``；
    * ``expand_scope``：按诊断给出的 symbol 重新做 symbol 级定位。

    审查后修复（Phase 11 场景 D）最多**一次**：``review_repairs`` 的上限由 Schema
    钉在 1，第二次 high 直接安全停止并把未解决问题留在报告里。旧补丁的回归与审查
    结论在这里一并清空——补丁身份变了，结论必须重新挣。

    **闭环准入在动任何东西之前**（DA-024）：剩余测试额度撑不起"目标测试 + 当前配置要求
    的回归层级"时，本轮直接以 :attr:`StopReason.BUDGET_CLOSURE_TEST_RUNS` 结束，
    不回退 Workspace、不重新规划、不派发编码。清空结论那一步发生在这道判定**之后**，
    所以被拒的轮次不会把已经挣到的审查证据一起带走。
    """
    _identity(state, deps)
    trigger = _repair_trigger(state)
    blocked = _budget_guard(state, deps, Activity.REPAIR, Node.REPAIR_PATCH)
    if blocked is not None:
        return blocked
    plan = _closure_test_plan(state, deps)
    if not plan.feasible:
        return _closure_refused(state, deps, plan, trigger=trigger)
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
    produced = _coding_stage(base_state, deps, round_plan, Node.REPAIR_PATCH, trigger=trigger)
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
    digest = patch_hash(state.current_patch)
    stop_note = state.coding_stop_reason.value if state.coding_stop_reason else ""
    # 审查结论必须绑定**当前**候选补丁：审过 v1 不等于审过 v2（§五.5）。
    review_trusted = state.review is not None and state.review.reviewed_patch_hash == digest
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
        elif state.review is not None and not review_trusted:
            status = RunStatus.FAILED
            stop_reason = StopReason.TESTS_FAILED
            reasons.append(
                "finalize: the recorded review was made against a different patch identity "
                f"(reviewed {state.review.reviewed_patch_hash[:12]}, candidate {digest[:12]})"
            )
        elif state.review is not None and state.review.has_high_issues:
            status = RunStatus.STOPPED
            stop_reason = StopReason.TESTS_FAILED
            reasons.append("unresolved high review issues")
        elif (
            execution.status is ExecutionStatus.PASS
            and state.regression is not None
            and state.regression.complete
            and review_trusted
        ):
            status = RunStatus.SUCCEEDED
            stop_reason = None
        elif execution.status is ExecutionStatus.PASS:
            status = RunStatus.PROVISIONALLY_VERIFIED
            stop_reason = None
            missing: list[str] = []
            if state.regression is None:
                missing.append("regression was not measured")
            elif not state.regression.complete:
                missing.append(f"regression is incomplete ({state.regression.status})")
            if state.review is None:
                missing.append(
                    "review was NOT executed "
                    f"({state.review_skip_reason or 'the reviewer produced no result'})"
                )
            if stop_note:
                missing.append(f"the coding stage stopped early on its local budget ({stop_note})")
            reasons.append(
                "finalize: target tests passed, but the verification closure is incomplete — "
                + "; ".join(missing)
                + ". This is not M2 verification: each stage listed here is reported as not "
                "executed, never as passed"
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
        new_tests_reviewed=review_trusted
        and state.review is not None
        and not state.review.has_high_issues,
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
