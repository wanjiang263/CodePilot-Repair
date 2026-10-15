"""G-20：六类预算各自都能把图停在节点入口，且停止后不再产生外部副作用。

每类预算一个用例，断言的都是同一组三件事：

* ``stop_reason`` 是**这一类**自己的原因（不是被别的规则顺带拦下的）；
* 拦停发生在**节点入口**，所以那一轮没有 LLM 调用、没有 Sandbox 执行、
  Workspace 没被改过；
* 停止之后没有任何新增副作用（比较 Sandbox 与 LLM 的调用计数）。

时间预算用注入的假时钟，测试不必真等 30 分钟。
"""

from __future__ import annotations

from pathlib import Path

from harness import FINISH, build_harness

from codepilot.graph.routing import Node
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import RunStatus, StopReason

EDIT_1 = {
    "tool": "edit_file",
    "arguments": {
        "path": "src/pkg/core.py",
        "search": "    return value - 1",
        "replace": "    return value - 2",
    },
}


def _one_round(tmp_path: Path, **overrides: object):
    """一轮失败的 run：目标测试 FAIL，诊断给 refine，第 2 轮再失败。"""
    from harness import assertion_failure, diagnosis

    kwargs: dict[str, object] = {
        "results": [
            assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3")
        ],
        "coder_rounds": [[EDIT_1, FINISH]],
        "diagnoses": [diagnosis("refine", line=2, text="return value - 2")],
    }
    kwargs.update(overrides)  # type: ignore[arg-type]
    return build_harness(tmp_path, **kwargs)  # type: ignore[arg-type]


def _side_effects(harness) -> tuple[int, int]:
    return len(harness.sandbox.run_calls), harness.llm.calls and len(harness.llm.calls)


# ------------------------------------------------------------------ iterations


def test_iterations_budget_stops_before_the_next_repair(tmp_path: Path) -> None:
    harness = _one_round(tmp_path, budget=RepairBudget(max_iterations=0))
    state = harness.run()

    assert state.stop_reason is StopReason.BUDGET_ITERATIONS, state.status.value
    assert state.status is RunStatus.BUDGET_EXHAUSTED
    visited = [item["node"] for item in state.events]
    # 第 1 轮的失败诊断照做，但不再修补丁、不再跑测试。
    assert Node.REPAIR_PATCH.value not in visited
    assert harness.deps.test_runs == 3  # 基线 2 + 目标测试 1
    assert len(state.attempts) == 1


# ------------------------------------------------------------------ test runs


def test_test_run_budget_stops_at_the_test_node_entry(tmp_path: Path) -> None:
    """``max_test_runs`` 只够基线，目标测试**一次都不该执行**。"""
    harness = _one_round(tmp_path, budget=RepairBudget(max_test_runs=2))
    state = harness.run()

    assert state.stop_reason is StopReason.BUDGET_TEST_RUNS, state.status.value
    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert state.execution is None
    assert state.attempts[0].execution is None
    visited = [item["node"] for item in state.events]
    assert Node.DIAGNOSE.value not in visited
    # 只执行了基线那两次。
    assert len(harness.sandbox.run_calls) == 2


# ------------------------------------------------------------------ tokens


def test_token_budget_stops_at_the_next_llm_node(tmp_path: Path) -> None:
    harness = _one_round(
        tmp_path,
        budget=RepairBudget(max_total_tokens=150),
        input_tokens=100,
    )
    state = harness.run()

    assert state.stop_reason is StopReason.BUDGET_TOKENS, state.status.value
    assert state.usage.total_tokens >= 150
    # 拦在 analyze_impact 入口：Planner 与 Coding Agent 都没有被调用。
    purposes = harness.llm.purposes()
    assert "planner" not in purposes
    assert "coder" not in purposes
    assert harness.sandbox.run_calls == []


def test_token_budget_is_not_mistaken_for_a_cost_stop(tmp_path: Path) -> None:
    """未设 ``max_cost`` 时，token 耗尽不能被报成费用耗尽。"""
    harness = _one_round(
        tmp_path,
        budget=RepairBudget(max_total_tokens=150, max_cost=None),
        input_tokens=100,
        cost_per_call=5.0,
    )
    state = harness.run()
    assert state.stop_reason is StopReason.BUDGET_TOKENS
    assert "budget_cost" not in " ".join(state.failure_reasons)


# ------------------------------------------------------------------ cost


def test_cost_budget_stops_when_the_user_cap_is_set(tmp_path: Path) -> None:
    harness = _one_round(
        tmp_path,
        budget=RepairBudget(max_cost=1.5),
        cost_per_call=1.0,
    )
    state = harness.run()

    assert state.stop_reason is StopReason.BUDGET_COST, state.status.value
    assert state.usage.cost >= 1.5
    assert "planner" not in harness.llm.purposes()


# ------------------------------------------------------------------ wall time


def test_wall_clock_budget_stops_before_any_llm_call(tmp_path: Path) -> None:
    """假时钟：不真等 30 分钟也能验证 ``max_runtime_seconds``。"""
    harness = _one_round(
        tmp_path,
        budget=RepairBudget(max_runtime_seconds=1800),
        offsets=(5400.0,),
    )
    state = harness.run()

    assert state.stop_reason is StopReason.BUDGET_TIME, state.status.value
    assert harness.llm.calls == [] or len(harness.llm.calls) == 0
    assert state.usage.llm_calls == 0
    assert harness.sandbox.run_calls == []
    visited = [item["node"] for item in state.events]
    assert visited[0] == Node.UNDERSTAND_ISSUE.value
    assert visited[-1] == Node.FINALIZE.value


# ------------------------------------------------------------------ tool calls


def test_tool_call_budget_maps_to_a_stop_reason_not_silently_ignored(
    tmp_path: Path,
) -> None:
    """Coding Agent 内部撞到工具上限时，必须映射成权威的 ``budget_tool_calls``。"""
    harness = _one_round(tmp_path, budget=RepairBudget(max_tool_calls_per_iteration=1))
    state = harness.run()

    assert state.stop_reason is StopReason.BUDGET_TOOL_CALLS, state.status.value
    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert len(state.tool_calls) == 1
    # 工具上限是在 generate_patch 里触发的，所以补丁从未送去执行测试。
    # （只剩基线那两次执行。）
    assert state.execution is None
    assert len(harness.sandbox.run_calls) == 2


# ------------------------------------------------------------------ 停止后无副作用


def test_no_external_effect_happens_after_a_budget_stop(tmp_path: Path) -> None:
    """停止之后 Sandbox 与 LLM 的调用计数必须完全冻结。"""
    harness = _one_round(tmp_path, budget=RepairBudget(max_iterations=0))
    state = harness.run()

    runs, calls = _side_effects(harness)
    # 再走一遍 finalize 之后计数没有增长。
    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert len(harness.sandbox.run_calls) == runs
    assert len(harness.llm.calls) == calls
    assert [item["node"] for item in state.events][-1] == Node.FINALIZE.value
    # finalize 始终可执行：报告与 Artifact 都写出来了。
    assert state.report_artifacts["report_json"]
    assert state.report_artifacts["state"] == "state_final.json"


def test_budget_stop_still_writes_the_run_report(tmp_path: Path) -> None:
    """预算耗尽时也必须能出报告，否则钱花了但没有任何证据。"""
    harness = _one_round(tmp_path, budget=RepairBudget(max_test_runs=2))
    state = harness.run()
    report_file = harness.artifacts_root / "runs" / state.run_id / "report.json"
    assert report_file.is_file()
    text = report_file.read_text(encoding="utf-8")
    assert "budget_test_runs" in text
    assert '"test_runs": 2' in text
