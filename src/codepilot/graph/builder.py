"""完整迭代修复 StateGraph 的装配（02 §15.2、G-01、G-14）。

节点是 :mod:`codepilot.graph.nodes` 里的 ``async def node(state, deps)``，
本模块负责两件事：

1. **绑定 deps**：LangGraph 只按 ``(state)`` 或 ``(state, config)`` 调用节点，
   所以依赖通过 :func:`_bound` 这层显式适配器注入，而不是让节点去读全局对象
   （03 §5 的硬约束）。节点本身因此仍然可以脱离图单独测试。
2. **接线**：条件边全部委托给 :mod:`codepilot.graph.routing` 里的纯函数，
   图里不内联任何判断逻辑。

## recursion_limit

``20 + max_iterations * 6``（02 §15.7）。它**不是**停止机制，而是路由逻辑写错
时的最后一道防线：正常路径永远先由 ``check_budget`` 的停止规则拦下。一轮修复最多
经过 ``repair_patch → validate_patch → run_target_tests → diagnose → check_budget``
五个节点，再加一次可能的回归/复审，6 个是带余量的上界；20 覆盖修复循环之前的
八个前置节点与收尾。

必须真正传给 ``invoke``：只在文档里写公式而代码用 LangGraph 的默认 25，
``max_iterations=5`` 的 run 会在第 3 轮被无声掐断。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Durability

from codepilot.graph import nodes
from codepilot.graph.deps import Deps
from codepilot.graph.routing import (
    Node,
    route_after_check_budget,
    route_after_diagnose,
    route_after_regression,
    route_after_review,
    route_after_rules,
    route_after_target_tests,
    route_after_validate_patch,
    terminated,
)
from codepilot.observability.events import EventType
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus, StopReason
from codepilot.schemas.state import RepairState

__all__ = [
    "NODE_FUNCS",
    "build_graph",
    "graph_config",
    "node_signature_ok",
    "recursion_limit_for",
    "run_graph",
    "run_graph_async",
    "supersteps_used",
]

#: 节点名 → 实现。Phase 9 直接在这里接上 ``regression`` / ``review`` 的真实实现。
NODE_FUNCS: dict[Node, Callable[[RepairState, Deps], Awaitable[dict[str, Any]]]] = {
    Node.UNDERSTAND_ISSUE: nodes.understand_issue,
    Node.RETRIEVE: nodes.retrieve,
    Node.LOCALIZE: nodes.localize,
    Node.ANALYZE_IMPACT: nodes.analyze_impact,
    Node.BUILD_ENV: nodes.build_env,
    Node.REPRODUCE: nodes.reproduce,
    Node.PLAN: nodes.plan,
    Node.GENERATE_PATCH: nodes.generate_patch,
    Node.VALIDATE_PATCH: nodes.validate_patch,
    Node.RUN_TARGET_TESTS: nodes.run_target_tests,
    Node.RUN_REGRESSION: nodes.run_regression,
    Node.REVIEW: nodes.review,
    Node.DIAGNOSE: nodes.diagnose,
    Node.CHECK_BUDGET: nodes.check_budget,
    Node.REPAIR_PATCH: nodes.repair_patch,
    Node.FINALIZE: nodes.finalize,
}


def recursion_limit_for(budget: RepairBudget) -> int:
    """02 §15.7 的公式。"""
    return 20 + budget.max_iterations * 6


def _guard(
    node: Node,
    fn: Callable[[RepairState, Deps], Awaitable[dict[str, Any]]],
    deps: Deps,
) -> Callable[[RepairState], Awaitable[dict[str, Any]]]:
    """把异常转成**可审计的终态**，而不是假成功（03 §26）。

    捕获后必须把状态写成 ``error``：如果只记日志然后返回 ``{}``，图会带着一个
    仍然 ``running`` 的状态继续往下走，最后的报告看起来像一次通过。
    """

    async def runner(state: RepairState) -> dict[str, Any]:
        try:
            update = await fn(state, deps)
        except Exception as exc:
            message = f"{node.value}: {type(exc).__name__}: {exc}"
            deps.events.emit(
                node=node.value, type=EventType.NODE_END, status="error", error=message
            )
            return {
                "usage_uncertain": state.usage_uncertain or node is Node.REVIEW,
                "status": RunStatus.ERROR,
                "stop_reason": StopReason.WORKFLOW_ERROR,
                "failure_reasons": [*state.failure_reasons, message],
                "usage": deps.usage,
                "test_runs": deps.test_runs,
                "events": [
                    *state.events,
                    {
                        "seq": len(state.events) + 1,
                        "node": node.value,
                        "status": "error",
                        "error": message,
                    },
                ],
            }
        _assert_partial(node, update)
        return update

    return runner


def _assert_partial(node: Node, update: Any) -> None:
    """节点只允许返回**字段子集**，不许整份重建 State。

    返回 ``state`` 对象本身会让"哪个节点改了什么"失去归属，checkpoint 的差异
    也就无法审计；这种错误越早炸越好。参数刻意是 ``Any`` 而不是 ``dict``：
    声明成 dict 会让下面的 isinstance 被判为"永不可能"而报 unreachable，
    但节点是运行时绑定的，类型注解挡不住返回整份 State 的实现。
    """
    if isinstance(update, RepairState):
        raise TypeError(f"node {node.value} returned a whole RepairState, not a field update")
    if not isinstance(update, dict):
        raise TypeError(
            f"node {node.value} returned {type(update).__name__}, expected a field dict"
        )
    unknown = set(update) - set(RepairState.model_fields)
    if unknown:
        raise KeyError(f"node {node.value} wrote unknown state fields: {sorted(unknown)}")


def _linear(workflow: Any, source: Node, target: Node) -> None:
    workflow.add_conditional_edges(
        source,
        lambda state: route_after_rules(state, target).value,
        {target.value: target.value, Node.FINALIZE.value: Node.FINALIZE.value},
    )


def build_graph(deps: Deps, *, checkpointer: Any | None = None) -> Any:
    """按 02 §15.2 装配并编译完整状态机。

    ``checkpointer`` 为 ``None`` 时图不持久化（单测与纯函数路由测试用）；
    生产路径必须传 :mod:`codepilot.graph.checkpoint` 构造的 SQLite checkpointer。
    """
    # LangGraph 的泛型重载无法表达"Pydantic state + 部分字段 dict 更新"这一
    # 组合，边界处按 Any 处理（与 Phase 5 minimal.py 同样的取舍）。
    workflow: Any = StateGraph(RepairState)
    for node, fn in NODE_FUNCS.items():
        workflow.add_node(node.value, _guard(node, fn, deps))

    workflow.add_edge(START, Node.UNDERSTAND_ISSUE.value)
    _linear(workflow, Node.UNDERSTAND_ISSUE, Node.RETRIEVE)
    _linear(workflow, Node.RETRIEVE, Node.LOCALIZE)
    _linear(workflow, Node.LOCALIZE, Node.ANALYZE_IMPACT)
    _linear(workflow, Node.ANALYZE_IMPACT, Node.BUILD_ENV)
    _linear(workflow, Node.BUILD_ENV, Node.REPRODUCE)
    _linear(workflow, Node.REPRODUCE, Node.PLAN)
    _linear(workflow, Node.PLAN, Node.GENERATE_PATCH)
    _linear(workflow, Node.GENERATE_PATCH, Node.VALIDATE_PATCH)

    workflow.add_conditional_edges(
        Node.VALIDATE_PATCH.value,
        lambda state: route_after_validate_patch(state).value,
        {
            Node.CHECK_BUDGET.value: Node.CHECK_BUDGET.value,
            Node.RUN_TARGET_TESTS.value: Node.RUN_TARGET_TESTS.value,
            Node.RUN_REGRESSION.value: Node.RUN_REGRESSION.value,
            Node.FINALIZE.value: Node.FINALIZE.value,
        },
    )
    workflow.add_conditional_edges(
        Node.RUN_TARGET_TESTS.value,
        lambda state: route_after_target_tests(state).value,
        {
            Node.RUN_REGRESSION.value: Node.RUN_REGRESSION.value,
            Node.DIAGNOSE.value: Node.DIAGNOSE.value,
            Node.FINALIZE.value: Node.FINALIZE.value,
        },
    )
    workflow.add_conditional_edges(
        Node.RUN_REGRESSION.value,
        lambda state: route_after_regression(state).value,
        {
            Node.REVIEW.value: Node.REVIEW.value,
            Node.DIAGNOSE.value: Node.DIAGNOSE.value,
            Node.FINALIZE.value: Node.FINALIZE.value,
        },
    )
    workflow.add_conditional_edges(
        Node.REVIEW.value,
        lambda state: route_after_review(state).value,
        {
            Node.CHECK_BUDGET.value: Node.CHECK_BUDGET.value,
            Node.FINALIZE.value: Node.FINALIZE.value,
        },
    )
    workflow.add_conditional_edges(
        Node.DIAGNOSE.value,
        lambda state: route_after_diagnose(state).value,
        {
            Node.CHECK_BUDGET.value: Node.CHECK_BUDGET.value,
            Node.FINALIZE.value: Node.FINALIZE.value,
        },
    )
    # check_budget 节点自己写入终态，因此路由的 ``stop`` 就是"状态已终止"。
    workflow.add_conditional_edges(
        Node.CHECK_BUDGET.value,
        lambda state: route_after_check_budget(state, stop=terminated(state)).value,
        {
            Node.REPAIR_PATCH.value: Node.REPAIR_PATCH.value,
            Node.FINALIZE.value: Node.FINALIZE.value,
        },
    )
    _linear(workflow, Node.REPAIR_PATCH, Node.VALIDATE_PATCH)
    workflow.add_edge(Node.FINALIZE.value, END)

    return workflow.compile(checkpointer=checkpointer)


def graph_config(
    deps: Deps, state: RepairState, *, recursion_limit: int | None = None
) -> dict[str, Any]:
    """一次调用的运行配置：``thread_id = run_id`` + 预算推导的 recursion_limit。"""
    return {
        "configurable": {"thread_id": state.run_id},
        "recursion_limit": recursion_limit or recursion_limit_for(state.budget),
        "metadata": {"run_id": deps.run_id, "task_id": deps.task_id},
    }


def supersteps_used(state: RepairState) -> int:
    """已经执行的节点次数——用它断言"图真的在跑 StateGraph"，而不是跑了个 for 循环。"""
    return len(state.events)


async def run_graph_async(
    initial: RepairState,
    deps: Deps,
    *,
    checkpointer: Any | None = None,
    recursion_limit: int | None = None,
    durability: Durability = "sync",
) -> RepairState:
    """异步入口：节点本身是 ``async def``，LangGraph 只有 ``ainvoke`` 能跑异步节点。"""
    graph = build_graph(deps, checkpointer=checkpointer)
    config = graph_config(deps, initial, recursion_limit=recursion_limit)
    payload = initial.model_dump(mode="python")
    mode: Durability | None = durability if checkpointer is not None else None
    try:
        result = await _ainvoke(graph, payload, config, durability=mode)
    except GraphRecursionError as exc:
        return await _report_recursion_overflow(
            initial, deps, config, exc, graph=graph if checkpointer is not None else None
        )
    return RepairState.model_validate(result)


async def _ainvoke(
    graph: Any, payload: dict[str, Any], config: dict[str, Any], *, durability: Durability | None
) -> Any:
    if durability is None:
        return await graph.ainvoke(payload, config)
    return await graph.ainvoke(payload, config, durability=durability)


def run_graph(
    initial: RepairState,
    deps: Deps,
    *,
    checkpointer: Any | None = None,
    recursion_limit: int | None = None,
    durability: Durability = "sync",
) -> RepairState:
    """同步入口：跑完整张图并返回最终 State。

    ``durability="sync"`` 不是性能上的奢侈。实测（``docs/notes/phase8.md``）在
    默认的 ``"async"`` 下，节点执行完但进程被硬杀时 checkpoint 还没落盘，
    恢复只能从头重跑——G-13 的"不重复已完成节点"就完全不成立。
    """
    return asyncio.run(
        run_graph_async(
            initial,
            deps,
            checkpointer=checkpointer,
            recursion_limit=recursion_limit,
            durability=durability,
        )
    )


async def _report_recursion_overflow(
    initial: RepairState,
    deps: Deps,
    config: dict[str, Any],
    exc: Exception,
    *,
    graph: Any | None = None,
) -> RepairState:
    """recursion_limit 命中时的安全终止。

    不假装成功：状态写成 ``error`` + ``workflow_error``，并把命中的上限与原因
    写进 ``failure_reasons``，报告里能直接看出这是"最后一道防线被触发"，
    而不是某条正常的停止规则。

    有 checkpointer 时**先回读最后一个 checkpoint**：里面可能有已经真实完成的
    节点与已计费的用量，直接拿 ``initial`` 会让一次已经烧了钱的运行在报告里
    变成"什么都没发生"。回读必须是 ``await aget_state``：``AsyncSqliteSaver``
    禁止在主线程里同步调用。
    """
    limit = config["recursion_limit"]
    recovered = initial
    if graph is not None:
        snapshot = await graph.aget_state(config)
        if snapshot.values:
            recovered = RepairState.model_validate(dict(snapshot.values))
    message = (
        f"recursion_limit={limit} reached; the graph stopped as a last resort "
        f"(StopRules should have stopped earlier). {type(exc).__name__}"
    )
    deps.events.emit(node="recursion_limit", type=EventType.STOP, status=message)
    return recovered.model_copy(
        update={
            "status": RunStatus.ERROR,
            "stop_reason": StopReason.WORKFLOW_ERROR,
            "failure_reasons": [*recovered.failure_reasons, message],
            "usage": deps.usage,
            "test_runs": deps.test_runs,
            "events": [
                *recovered.events,
                {
                    "seq": len(recovered.events) + 1,
                    "node": "recursion_limit",
                    "status": "error",
                    "error": message,
                },
            ],
        }
    )


def node_signature_ok(fn: Callable[..., Any]) -> bool:
    """节点必须是 ``async def node(state, deps)``——两个位置参数。"""
    parameters = list(inspect.signature(fn).parameters)
    return len(parameters) == 2 and inspect.iscoroutinefunction(fn)
