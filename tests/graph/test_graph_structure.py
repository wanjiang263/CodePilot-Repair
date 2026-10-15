"""G-01 / G-14：图是真的 StateGraph，recursion_limit 真的生效。

两条容易自欺欺人的地方，这里都要能证伪：

1. "多轮修复"其实只是把 Phase 5 的 solve 套了个 for 循环 —— 所以断言节点集合、
   条件边和 LangGraph 的 superstep 计数，而不是只看返回值；
2. ``recursion_limit`` 只写在文档里，代码实际用 LangGraph 默认的 25 —— 所以
   这里用一个刻意过小的上限把 ``GraphRecursionError`` 真的打出来，再确认
   ``run_graph`` 把它转成了保守的终态而不是"什么都没发生"。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from harness import (
    FINISH,
    ROUND1_FIXTURE,
    assertion_failure,
    build_harness,
    diagnosis,
)

from codepilot.graph.builder import (
    NODE_FUNCS,
    build_graph,
    graph_config,
    node_signature_ok,
    recursion_limit_for,
    run_graph,
    supersteps_used,
)
from codepilot.graph.checkpoint import open_checkpointer, run_with_checkpoint
from codepilot.graph.routing import Node
from codepilot.graph.stop_rules import patch_hash
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus, StopReason

EXPECTED_NODES = {
    "understand_issue",
    "retrieve",
    "localize",
    "analyze_impact",
    "build_env",
    "reproduce",
    "plan",
    "generate_patch",
    "validate_patch",
    "run_target_tests",
    "run_regression",
    "review",
    "diagnose",
    "check_budget",
    "repair_patch",
    "finalize",
}


def _looping_harness(tmp_path: Path):
    """一个会反复失败的 run：每轮补丁不同、签名不同、且一直在推进。

    这样的历史不会命中任何 StopRules（既不是重复补丁也不是重复失败），只能由
    recursion_limit 拦住，正好用来验证"最后一道防线"确实在拦。
    """
    rounds = []
    for index in range(1, 9):
        rounds.append(
            [
                {
                    "tool": "edit_file",
                    "arguments": {
                        "path": "src/pkg/core.py",
                        "search": f"    return value - {index}",
                        "replace": f"    return value - {index + 1}",
                    },
                },
                FINISH,
            ]
        )
    return build_harness(
        tmp_path,
        results=[
            assertion_failure(f"tests/test_core.py::t{index}", message=f"case {index} fails")
            for index in range(1, 9)
        ],
        coder_rounds=rounds,
        diagnoses=[
            diagnosis("refine", line=2, text=f"return value - {index + 1}") for index in range(1, 9)
        ],
        budget=RepairBudget(max_iterations=200, max_test_runs=200, max_tool_calls_per_iteration=30),
    )


# ------------------------------------------------------------------ G-01 结构


def test_graph_exposes_exactly_the_documented_node_set(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, results=[], coder_rounds=[])
    compiled = build_graph(harness.deps)
    assert set(compiled.get_graph().nodes) - {"__start__", "__end__"} == EXPECTED_NODES
    assert set(NODE_FUNCS) == {Node(name) for name in EXPECTED_NODES}


@pytest.mark.parametrize("name", sorted(EXPECTED_NODES))
def test_every_node_is_an_async_two_argument_function(name: str) -> None:
    """03 §5 的节点签名：``async def node(state, deps) -> dict``。"""
    fn = NODE_FUNCS[Node(name)]
    assert inspect.iscoroutinefunction(fn), name
    assert node_signature_ok(fn)


def test_conditional_edges_are_present_not_linear_only(tmp_path: Path) -> None:
    """分支必须真的存在：线性边连起来的"循环"是另一种 bug。"""
    harness = build_harness(tmp_path, results=[], coder_rounds=[])
    edges = {(edge.source, edge.target) for edge in build_graph(harness.deps).get_graph().edges}
    assert (Node.VALIDATE_PATCH.value, Node.CHECK_BUDGET.value) in edges
    assert (Node.VALIDATE_PATCH.value, Node.RUN_TARGET_TESTS.value) in edges
    assert (Node.RUN_TARGET_TESTS.value, Node.DIAGNOSE.value) in edges
    assert (Node.RUN_TARGET_TESTS.value, Node.RUN_REGRESSION.value) in edges
    assert (Node.CHECK_BUDGET.value, Node.REPAIR_PATCH.value) in edges
    assert (Node.REPAIR_PATCH.value, Node.VALIDATE_PATCH.value) in edges
    assert (Node.DIAGNOSE.value, Node.CHECK_BUDGET.value) in edges
    assert (Node.FINALIZE.value, "__end__") in edges


def test_graph_actually_runs_multiple_supersteps_not_a_python_loop(tmp_path: Path) -> None:
    """事件轨迹是 LangGraph 逐节点写出来的，节点名与顺序都对得上。"""
    harness = build_harness(
        tmp_path,
        results=[
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
        ],
        coder_rounds=[
            [ROUND1_FIXTURE, FINISH],
            [ROUND1_FIXTURE, FINISH],
        ],
        diagnoses=[
            diagnosis("refine", line=2, text="return value - 2"),
            diagnosis("give_up", line=2, text="return value - 2"),
        ],
    )
    state = harness.run()
    steps = supersteps_used(state)
    assert steps > 12, steps
    visited = [item["node"] for item in state.events]
    assert visited[0] == Node.UNDERSTAND_ISSUE.value
    assert visited[-1] == Node.FINALIZE.value
    # 第二轮是被 give_up 停下来的，不是循环到一半自己没了。
    assert state.stop_reason is StopReason.DIAGNOSIS_GIVE_UP


# ------------------------------------------------------------------ G-14


@pytest.mark.parametrize(
    ("max_iterations", "expected"),
    [(0, 20), (1, 26), (5, 50), (8, 68), (20, 140)],
)
def test_recursion_limit_follows_the_documented_formula(max_iterations: int, expected: int) -> None:
    assert recursion_limit_for(RepairBudget(max_iterations=max_iterations)) == expected


def test_config_carries_the_limit_derived_from_this_runs_budget(tmp_path: Path) -> None:
    harness = build_harness(
        tmp_path,
        results=[],
        coder_rounds=[],
        budget=RepairBudget(max_iterations=3),
    )
    config = graph_config(harness.deps, harness.initial)
    assert config["recursion_limit"] == 38
    assert config["configurable"]["thread_id"] == harness.deps.run_id
    # thread_id 与 run_id 绑定，别的 run 拿不到这条线程。
    assert config["metadata"]["run_id"] == harness.initial.run_id


def test_tiny_limit_actually_raises_and_is_reported_conservatively(tmp_path: Path) -> None:
    """上限真的传给了 LangGraph，而不是只写在文档里。

    用一个明显不够走完一条修复链的小上限：LangGraph 必须抛
    ``GraphRecursionError``，而 ``run_graph`` 必须把它转成 ``error`` +
    ``workflow_error``，**不能**让报告看起来像成功。
    """
    harness = _looping_harness(tmp_path)
    state = run_graph(harness.initial, harness.deps, recursion_limit=12)

    assert state.status is RunStatus.ERROR
    assert state.stop_reason is StopReason.WORKFLOW_ERROR
    assert any("recursion_limit=12" in item for item in state.failure_reasons)


def test_recursion_overflow_keeps_the_accounting_that_already_happened(tmp_path: Path) -> None:
    """有 checkpoint 时回读最后一次落盘的状态：已经烧掉的用量不能凭空消失。

    没有这一层的话，一次"跑到第 3 轮被上限拦住"的运行会在报告里显示
    ``llm_calls=0``，而钱确实已经花出去了。
    """
    harness = _looping_harness(tmp_path)

    async def go() -> object:
        async with await open_checkpointer(tmp_path / "cap.sqlite", harness.deps.run_id) as handle:
            return await run_with_checkpoint(
                harness.initial, harness.deps, handle, recursion_limit=12
            )

    recovered = asyncio.run(go())
    assert recovered.status is RunStatus.ERROR
    assert recovered.stop_reason is StopReason.WORKFLOW_ERROR
    assert recovered.usage.llm_calls > 0, "already-paid LLM calls vanished"
    assert recovered.test_runs > 0, "already-executed test runs vanished"
    assert any("recursion_limit=12" in item for item in recovered.failure_reasons)


def test_formula_limit_never_substitutes_for_the_stop_rules(tmp_path: Path) -> None:
    """最后一道防线不该被正常路径碰到：停止规则要先拦住。"""
    harness = build_harness(
        tmp_path,
        results=[
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
        ],
        coder_rounds=[[ROUND1_FIXTURE, FINISH], [ROUND1_FIXTURE, FINISH]],
        diagnoses=[
            diagnosis("refine", line=2, text="return value - 2"),
            diagnosis("refine", line=2, text="return value - 2"),
        ],
        budget=RepairBudget(max_iterations=5),
    )
    state = harness.run()
    # 重复补丁先命中，远早于 recursion_limit=50。
    assert state.stop_reason is StopReason.REPEATED_PATCH
    assert supersteps_used(state) < recursion_limit_for(state.budget)


def test_nodes_cannot_rewrite_the_whole_state(tmp_path: Path) -> None:
    """节点返回整份 State 或未知字段必须报错，而不是静默接受。"""
    from codepilot.graph.builder import _assert_partial

    harness = build_harness(tmp_path, results=[], coder_rounds=[])
    _assert_partial(Node.PLAN, {"plan": None})  # 正常：只返回字段
    with pytest.raises(TypeError):
        _assert_partial(Node.PLAN, harness.initial)
    with pytest.raises(TypeError):
        _assert_partial(Node.PLAN, "not a dict")
    with pytest.raises(KeyError):
        _assert_partial(Node.PLAN, {"not_a_state_field": 1})


def test_patch_identity_is_stable_across_recomputes(tmp_path: Path) -> None:
    """补丁哈希只在内容真的变化时才变，否则 repeated_patch 判定没有意义。"""
    diff = ROUND1_DIFF
    assert patch_hash(diff) == patch_hash(diff)
    assert patch_hash(diff) != patch_hash(diff.replace("- 2", "- 3"))


ROUND1_DIFF = "\n".join(
    [
        "diff --git a/src/pkg/core.py b/src/pkg/core.py",
        "--- a/src/pkg/core.py",
        "+++ b/src/pkg/core.py",
        "@@ -1,2 +1,2 @@",
        " def normalize(value: int) -> int:",
        "-    return value - 1",
        "+    return value - 2",
        "",
    ]
)
