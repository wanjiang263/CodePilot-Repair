"""G-09：三种修复策略必须真的改变下一轮输入或 Workspace。

只返回一个不同的字符串不算实现。每条策略都有一个**可证伪**的断言：

* ``refine``：Workspace 保留上一轮的改动，且诊断结论进入下一轮计划上下文，
  且不重新调用 Planner；
* ``revert_and_retry``：隔离 Workspace 被逐字恢复到 base commit 的内容，
  Planner 被重新调用，且 ``alternatives`` 出现在新的 prompt 里；
* ``expand_scope``：``localization`` 多出诊断点名的那个**索引中真实存在**的
  symbol，编造的名字被丢弃并留下理由，原始检索快照引用保持不变。

用节点级测试而不是整图测试，是为了把"策略做了什么"和"路由之后发生了什么"分开：
后者由 ``test_path_*`` 覆盖。前置状态由 :meth:`Harness.prime` 跑真实分析链得到，
不是手工捏对象——``LocalizationResult`` 的成员校验本身就是被测的一部分。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from harness import (
    DEFECTIVE,
    FINISH,
    ROUND1_FIXTURE,
    assertion_failure,
    build_harness,
)

from codepilot.graph import nodes
from codepilot.graph.deps import DepsConfigurationError
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.diagnosis import Diagnosis
from codepilot.schemas.enums import RepairStrategy, RunStatus, StopReason
from codepilot.schemas.localization import LocalizationResult
from codepilot.schemas.plan import PlannedChange, RepairPlan
from codepilot.schemas.state import RepairState
from codepilot.schemas.validation import ValidationResult

BROKEN = "def normalize(value: int) -> int:\n    return value - 2\n"
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


def _round_one_state(harness, *, strategy, suspected=(), extra_attempts: int = 0) -> RepairState:
    """跑到"第 1 轮补丁已落地、测试已失败、诊断已给出策略"的现场。

    顺序很重要：先跑真实分析链，**再**把第 1 轮改坏的内容写进 Workspace。
    反过来做会让 ``SkeletonBuilder`` 的 sha256 核对发现"索引比文件旧"并抛
    ``stale index`` ——那是 Phase 1 的完整性保护在正常工作，不是测试想要的路径。
    """
    state = harness.prime(
        patch_diff=ROUND1_DIFF,
        execution=assertion_failure("tests/test_core.py::test_normalize", message="assert 1 == 3"),
        diagnosis_payload=Diagnosis(
            root_cause="hypothesis",
            repair_strategy=strategy,
            suspected_symbols=list(suspected),
        ),
        extra_attempts=extra_attempts,
    )
    (harness.workspace / "src/pkg/core.py").write_text(BROKEN, encoding="utf-8")
    return state


def _repair(state: RepairState, harness) -> RepairState:
    update = asyncio.run(nodes.repair_patch(state, harness.deps))
    return state.model_copy(update=update)


def _coder_rounds() -> list[list[dict[str, object]]]:
    return [[ROUND1_FIXTURE, FINISH]]


# ------------------------------------------------------------------ refine


def test_refine_keeps_the_current_changes_and_feeds_back_the_diagnosis(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds())
    state = _round_one_state(harness, strategy=RepairStrategy.REFINE)

    produced = _repair(state, harness)

    # refine 不回退：上一轮写进去的那一行仍然在文件里。
    assert (harness.workspace / "src/pkg/core.py").read_text(encoding="utf-8") == BROKEN
    assert produced.plan is not None
    # 诊断结论被写进了下一轮计划的可信上下文。
    assert any("hypothesis" in item for item in produced.plan.evidence)
    # refine 复用现有计划：Planner 只有 prime 那一次（= 2 个请求），没有新增。
    assert harness.llm.purposes().count("planner") == 2
    assert produced.current_patch


# ------------------------------------------------------------------ revert_and_retry


def test_revert_and_retry_restores_the_isolated_workspace(tmp_path: Path) -> None:
    """回退必须能被单独观察到，所以本轮的 Coding Agent 只 ``finish`` 不再编辑。

    如果这里给 coder 一段编辑脚本，它会**合法地**把文件改成新内容，测试看到的就是
    "回退之后又被改写"，无法证明回退发生过 —— 第一次写这个用例时就踩过这个坑。
    """
    harness = build_harness(tmp_path, results=[], coder_rounds=[[FINISH]], extra_plans=1)
    state = _round_one_state(harness, strategy=RepairStrategy.REVERT_AND_RETRY)
    assert (harness.workspace / "src/pkg/core.py").read_text(encoding="utf-8") == BROKEN

    produced = _repair(state, harness)

    # 逐字恢复到 base commit 的内容 —— 这是"真的回退了"的唯一硬证据。
    assert (harness.workspace / "src/pkg/core.py").read_text(encoding="utf-8") == DEFECTIVE
    # 原始开发仓库不受影响（revert 只作用于任务隔离 Workspace）。
    assert (harness.original / "src/pkg/core.py").read_text(encoding="utf-8") == DEFECTIVE
    assert [item for item in produced.events if item.get("status") == "workspace_reverted"]


def test_revert_and_retry_replans_with_the_alternatives(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds(), extra_plans=1)
    state = _round_one_state(harness, strategy=RepairStrategy.REVERT_AND_RETRY)
    _repair(state, harness)

    # 一次 plan = 2 个请求；4 次说明真的重新规划过一轮。
    purposes = harness.llm.purposes()
    assert purposes.count("planner") == 4, "revert_and_retry must re-plan"
    # 取**最后一个** planner 请求：purposes() 末尾还有 coder 调用。
    last_planner = len(purposes) - 1 - purposes[::-1].index("planner")
    prompt = harness.llm.prompt_texts()[last_planner]
    assert "revert_and_retry" in prompt
    assert "guard the subtraction instead of removing it" in prompt
    assert "iteration 1: FAIL" in prompt


def test_revert_refuses_to_guess_without_a_workspace_manager(tmp_path: Path) -> None:
    """没有 WorkspaceManager 时明确报告失败，而不是"假装回退了"。"""
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds())
    harness.deps.workspace_manager = None
    reverted, reason = nodes._revert_workspace(harness.deps)
    assert reverted is False
    assert "WorkspaceManager" in reason


def test_workspace_reset_is_scoped_to_the_task_workspace(tmp_path: Path) -> None:
    """回退动作只碰 deps.workspace，不碰调用方传入的原始仓库。"""
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds())
    (harness.workspace / "scratch.txt").write_text("junk", encoding="utf-8")
    assert harness.deps.workspace_manager is not None
    harness.deps.workspace_manager.reset(harness.workspace)
    assert not (harness.workspace / "scratch.txt").exists()
    assert (harness.original / "src/pkg/core.py").read_text(encoding="utf-8") == DEFECTIVE


# ------------------------------------------------------------------ expand_scope


def test_expand_scope_adds_a_real_symbol_and_keeps_the_snapshot(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds())
    helper = next(
        item
        for item in harness.deps.index.symbol_objects()
        if item.qualified_name == "pkg.helper.coerce"
    )
    state = _round_one_state(harness, strategy=RepairStrategy.EXPAND_SCOPE, suspected=[helper.id])
    original_snapshot = state.localization.retrieval_snapshot_id

    produced = _repair(state, harness)

    assert produced.localization is not None
    assert helper.id in {item.symbol_id for item in produced.localization.symbols}
    assert "src/pkg/helper.py" in {item.path for item in produced.localization.files}
    # 新增定位是"扩展"，不是替换证据：原始检索快照引用必须保持不变。
    assert produced.localization.retrieval_snapshot_id == original_snapshot


def test_expand_scope_drops_symbols_that_do_not_exist(tmp_path: Path) -> None:
    """模型编造的名字不能扩大范围，更不能把上下文引到仓库外。"""
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds())
    state = _round_one_state(
        harness,
        strategy=RepairStrategy.EXPAND_SCOPE,
        suspected=["invented_symbol", "../outside/module"],
    )
    produced = _repair(state, harness)

    assert produced.localization is not None
    ids = {item.symbol_id for item in produced.localization.symbols}
    assert "invented_symbol" not in ids
    assert not any("../" in item.file for item in produced.localization.symbols)
    expansion = [item for item in produced.events if item.get("status") == "scope_expanded"]
    assert expansion and "不在索引里" in expansion[-1]["extra"]


def test_expand_scope_respects_the_localization_caps(tmp_path: Path) -> None:
    """symbol 上限 5 / 文件上限 3 是 Context 预算的一部分，扩展不得突破。"""
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds())
    symbols = list(harness.deps.index.symbol_objects())
    state = _round_one_state(
        harness, strategy=RepairStrategy.EXPAND_SCOPE, suspected=[item.id for item in symbols]
    )
    produced = _repair(state, harness)

    assert produced.localization is not None
    assert len(produced.localization.symbols) <= 5
    assert len(produced.localization.files) <= 3
    # 扩展后的定位结果仍然通过 schema 自身的成员校验（顺序、归属、行号范围）。
    LocalizationResult.model_validate(produced.localization.model_dump())


# ------------------------------------------------------------------ 预算与缺依赖


def test_repair_patch_is_blocked_before_any_side_effect_when_out_of_budget(
    tmp_path: Path,
) -> None:
    """G-05：repair_patch 入口预算耗尽时不调 LLM、不动 Workspace。"""
    harness = build_harness(
        tmp_path,
        results=[],
        coder_rounds=_coder_rounds(),
        budget=RepairBudget(max_iterations=1),
    )
    state = _round_one_state(harness, strategy=RepairStrategy.REFINE, extra_attempts=2)
    before = (harness.workspace / "src/pkg/core.py").read_text(encoding="utf-8")
    purposes_before = list(harness.llm.purposes())

    update = asyncio.run(nodes.repair_patch(state, harness.deps))

    assert update["status"] is RunStatus.BUDGET_EXHAUSTED
    assert update["stop_reason"] is StopReason.BUDGET_ITERATIONS
    assert (harness.workspace / "src/pkg/core.py").read_text(encoding="utf-8") == before
    assert harness.llm.purposes() == purposes_before


def test_repair_patch_requires_a_plan_and_says_so(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds())
    state = _round_one_state(harness, strategy=RepairStrategy.REFINE).model_copy(
        update={"plan": None}
    )
    update = asyncio.run(nodes.repair_patch(state, harness.deps))
    assert update["status"] is RunStatus.ERROR
    assert update["stop_reason"] is StopReason.WORKFLOW_ERROR
    assert "requires a plan" in update["failure_reasons"][-1]


def test_node_identity_check_rejects_a_foreign_run(tmp_path: Path) -> None:
    """拿 A 任务的 Deps 跑 B 任务必须抛错（G-02 的并发保护）。"""
    harness = build_harness(tmp_path, results=[], coder_rounds=_coder_rounds())
    foreign = harness.initial.model_copy(update={"run_id": "other-run"})
    with pytest.raises(DepsConfigurationError, match="run_id"):
        asyncio.run(nodes.repair_patch(foreign, harness.deps))


def test_plan_and_validation_shapes_are_not_bypassed_by_the_fixture() -> None:
    """正控制：确认夹具没有靠省略必填字段让断言变得比现实宽松。"""
    plan = RepairPlan(
        root_cause_hypothesis="h",
        changes=[PlannedChange(file="a.py", description="fix")],
        tests_to_run=["tests/test_core.py"],
        alternatives=["alt"],
    )
    assert plan.risk.value == "low"
    assert ValidationResult(passed=True).checks == []
