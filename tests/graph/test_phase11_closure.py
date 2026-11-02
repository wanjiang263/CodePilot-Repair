"""Phase 11：M2 自验证闭环的图级回归测试（任务书 §七）。

这一轮要修的缺陷只有一个形状：**Coder 的局部额度耗尽，被当成了整个 run 的终点**。
首次真实 GLM 运行（``docs/notes/phase11-runtime-acceptance.md``）里，``repair_patch``
的编码循环撞到每轮 30 次工具上限，图直接 finalize，PatchReviewer 从未执行——补丁
其实是对的，官方判了 RESOLVED，但 Agent 自己没有任何验证闭环。

所以这里的断言全部围绕两件事：

* **局部额度耗尽之后，验证与审查还必须发生**（场景 A）；
* **不满足前置条件时，验证与审查必须如实记录为未执行**，而不是被跳过或被伪造成通过
  （场景 B/E，以及 §六 的报告口径）。

夹具与 Phase 8/9 一致：真实 git 仓库、真实索引、真实 PatchValidator、真实回归与
Reviewer，只有模型与 Sandbox 是脚本化的（``simulated_llm=True``）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from harness import (
    FINISH,
    TARGET,
    assert_original_clean,
    assertion_failure,
    build_harness,
    diagnosis,
    edit,
    passed_result,
)

from codepilot.agents.reviewer import PatchReviewer
from codepilot.eval.swebench_harness import HarnessDiscrepancy, compare_with_internal_m2
from codepilot.graph import nodes
from codepilot.graph.budget import (
    REVIEW_CONTEXT_TOKENS,
    REVIEW_OUTPUT_TOKENS,
    Activity,
    BudgetManager,
    Category,
)
from codepilot.graph.regression import RegressionRunner
from codepilot.graph.routing import (
    Node,
    pending_review_calls,
    route_after_rules,
    route_after_validate_patch,
)
from codepilot.graph.stop_rules import patch_hash
from codepilot.llm.config import KNOWN_ROLES, LLMConfig
from codepilot.llm.fake import FakeLLM
from codepilot.report import VerificationClosure, verification_level
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason
from codepilot.schemas.state import RepairState

APPROVED: dict[str, object] = {"approved": True, "summary": "reviewed the candidate patch"}
HIGH: dict[str, object] = {
    "approved": False,
    "issues": [
        {
            "severity": "high",
            "file": "src/pkg/core.py",
            "line": 2,
            "description": "The identity contract still needs an explicit assertion",
        }
    ],
}
LOW: dict[str, object] = {
    "approved": True,
    "issues": [
        {
            "severity": "low",
            "file": "src/pkg/core.py",
            "line": 2,
            "description": "Consider naming the contract in the docstring",
        }
    ],
}

#: 一次编辑 + 29 次只读 = 30 次工具调用，正好等于默认的每轮上限。
READ = {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}}
THIRTY_CALLS: list[dict[str, object]] = [
    edit("src/pkg/core.py", "    return value - 1", "    return value"),
    *[READ] * 29,
]


def _report(harness, state: RepairState) -> dict[str, object]:
    """读**落盘的** report.json —— 断言产物而不是内存对象。"""
    path = harness.deps.artifacts.run_dir / state.report_artifacts["report_json"]
    return json.loads(path.read_text(encoding="utf-8"))


def _closure(harness, state: RepairState) -> dict[str, object]:
    return _report(harness, state)["closure"]  # type: ignore[index]


def _wired(harness, reviewer_replies: list[dict[str, object]]) -> FakeLLM:
    """接上真实回归与独立审查，返回审查用的 FakeLLM（可断言它被调了几次）。"""
    llm = FakeLLM(reviewer_replies)
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(llm)
    return llm


def _pending(state: RepairState) -> int:
    """还欠几次审查 —— 预留的计数口径必须与 :func:`nodes._coding_stage` 同源。

    测试自己另写一套"一般是 2 次"的推导，就等于没测状态机那一条。
    """
    return pending_review_calls(state, candidate_digest=patch_hash(state.current_patch))


def _visited(state: RepairState) -> list[str]:
    return [item["node"] for item in state.events]


def _analysis_chain(harness) -> RepairState:
    """把图跑到"计划已就绪、还没生成补丁"的那一步。"""
    state = harness.initial
    for name in ("understand_issue", "retrieve", "localize", "analyze_impact", "plan"):
        state = state.model_copy(update=asyncio.run(getattr(nodes, name)(state, harness.deps)))
    return state


# ------------------------------------------------------------------ 场景 A / §七.1


def test_coder_tool_cap_with_valid_patch_still_runs_the_whole_closure(tmp_path: Path) -> None:
    """§七.1 + 任务书要求的完整图级集成测试：30 次工具调用用满，闭环照走。

    这是首次真实运行那一次的形状：补丁有效、目标测试通过，但编码阶段把每轮工具
    额度花光了。修复前在这里直接 finalize；修复后必须依次经过 PatchValidator、
    目标测试、回归与 Reviewer，并产出真实审查结论。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
    )
    reviewer = _wired(harness, [APPROVED])
    state = harness.run()

    # 局部额度**确实**耗尽了——这是本用例的前提，不是副作用。
    assert len(state.tool_calls) == 30
    assert state.coding_stop_reason is StopReason.BUDGET_TOOL_CALLS
    assert harness.deps.coder.max_tool_calls == 30

    # 但它没有把 run 判死：图继续走完闭环。
    visited = _visited(state)
    for node in (
        Node.VALIDATE_PATCH,
        Node.RUN_TARGET_TESTS,
        Node.RUN_REGRESSION,
        Node.REVIEW,
        Node.FINALIZE,
    ):
        assert node.value in visited, (node.value, visited)
    assert visited.index(Node.VALIDATE_PATCH.value) < visited.index(Node.REVIEW.value)
    assert Node.REPAIR_PATCH.value not in visited

    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert state.stop_reason is None
    assert state.review is not None and state.review.approved
    assert state.review_calls == 1
    assert reviewer.calls, "Reviewer 必须真的向模型要过审查结论"

    # 补丁身份：编码阶段冻结的版本 == 送去验证/测试/审查的版本。
    digest = patch_hash(state.current_patch)
    assert state.frozen_patch_hash == digest
    assert state.review.reviewed_patch_hash == digest
    assert state.current_attempt is not None
    assert state.current_attempt.patch_hash == digest

    closure = _closure(harness, state)
    assert closure["patch_present"] is True
    assert closure["candidate_patch_sha256"] == digest
    assert closure["frozen_by_coding_stage"] is True
    assert closure["coding_stop_reason"] == "budget_tool_calls"
    assert closure["validator_executed"] is True and closure["validator_passed"] is True
    assert closure["target_tests_executed"] is True and closure["target_tests_status"] == "PASS"
    assert closure["regression_executed"] is True and closure["regression_complete"] is True
    assert closure["regression_new_failures"] == 0
    assert closure["review_executed"] is True
    assert closure["review_bound_to_candidate"] is True
    assert closure["unexecuted_stages"] == []
    assert closure["review_calls"] == 1
    assert _report(harness, state)["verification_level"] == "m2_verified"
    assert_original_clean(harness)


def test_local_tool_exhaustion_does_not_consume_the_review_allowance(tmp_path: Path) -> None:
    """§七.9：Coder 烧光工具额度，绝不能把审查的专属额度一起冻结。

    两个方向都要成立：``tool_calls`` 到达上限时审查额度还是满的（编码没有代审查
    花钱），而审查活动判定完全不看工具调用数。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
    )
    _wired(harness, [APPROVED])
    state = harness.run()

    assert len(state.tool_calls) == 30
    assert state.coding_stop_reason is StopReason.BUDGET_TOOL_CALLS
    assert state.review_calls == 1
    assert state.budget.max_review_calls == 2

    readings = harness.deps.budget.readings(state.budget, state)
    assert readings[Category.REVIEW_CALLS] == (1.0, 2.0)
    assert readings[Category.TOOL_CALLS] == (30.0, 30.0)

    # 反过来也必须成立：工具额度封顶时，审查活动自己仍然有额度。
    review_only = state.model_copy(update={"review_calls": 0, "tool_calls": [READ] * 30})
    assert harness.deps.budget.check(review_only, Activity.REVIEW) is None
    # 而编码额度耗尽照样拦住"再来一轮编码"——两套额度各自成立才叫独立计数。
    assert harness.deps.budget.check(review_only, Activity.REPAIR) is StopReason.BUDGET_TOOL_CALLS


# ------------------------------------------------------------------ 场景 B / §七.2


def test_tool_cap_without_any_patch_terminates_without_a_success_claim(tmp_path: Path) -> None:
    """§七.2 + 场景 B：额度耗尽且没有补丁 → 安全终止，不误报成功、不强行审查。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[[READ]],
        budget=RepairBudget(max_tool_calls_per_iteration=1),
    )
    reviewer = _wired(harness, [APPROVED])
    state = harness.run()

    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert state.stop_reason is StopReason.BUDGET_TOOL_CALLS
    assert state.coding_stop_reason is StopReason.BUDGET_TOOL_CALLS
    assert state.current_patch == ""
    assert state.frozen_patch_hash == ""
    assert state.attempts == []
    assert any("EMPTY_PATCH" in item for item in state.failure_reasons)
    # 前置条件不成立时**不得**调用 Reviewer：这里连模型都没问。
    assert reviewer.calls == []
    assert state.review is None

    closure = _closure(harness, state)
    assert closure["patch_present"] is False
    assert closure["validator_executed"] is False
    assert "patch_validation" in closure["unexecuted_stages"]
    assert any(str(item).startswith("review:") for item in closure["unexecuted_stages"])
    assert closure["review_executed"] is False
    assert _report(harness, state)["verification_level"] not in {"m2_verified", "sampled_verified"}


def test_tool_cap_with_a_patch_the_validator_rejects_does_not_finish_successful(
    tmp_path: Path,
) -> None:
    """§七.3：额度耗尽 + 补丁过不了 PatchValidator → 不进成功收尾。"""
    six_new_files = [
        {
            "tool": "create_file",
            "arguments": {"path": f"src/pkg/extra{index}.py", "content": f"X{index} = {index}\n"},
        }
        for index in range(6)
    ]
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[six_new_files],
        budget=RepairBudget(max_tool_calls_per_iteration=6),
    )
    reviewer = _wired(harness, [APPROVED])
    state = harness.run()

    assert state.coding_stop_reason is StopReason.BUDGET_TOOL_CALLS
    assert state.current_patch.strip(), "补丁是存在的，只是不被校验器接受"
    attempt = state.current_attempt
    assert attempt is not None and attempt.validation.passed is False
    assert "exceed limit" in " ".join(state.failure_reasons)
    # 被拒的补丁不会被送去执行，更不会被拿去审查。
    assert state.execution is None
    assert reviewer.calls == []
    assert state.status is RunStatus.BUDGET_EXHAUSTED, state.status
    assert _report(harness, state)["verification_level"] not in {"m2_verified", "sampled_verified"}
    closure = _closure(harness, state)
    assert closure["validator_passed"] is False
    assert closure["review_executed"] is False


# ------------------------------------------------------------------ 场景 C / §七.7


def test_target_test_failure_goes_to_diagnosis_not_a_success_claim(tmp_path: Path) -> None:
    """§七.7：目标测试失败 → Failure Diagnosis，而不是宣称修好了。"""
    harness = build_harness(
        tmp_path,
        results=[assertion_failure(f"{TARGET}::test_normalize", message="assert 1 == 3")],
        coder_rounds=[
            [edit("src/pkg/core.py", "    return value - 1", "    return value - 2"), FINISH]
        ],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
        budget=RepairBudget(max_iterations=0),
    )
    reviewer = _wired(harness, [APPROVED])
    state = harness.run()

    visited = _visited(state)
    assert Node.DIAGNOSE.value in visited, visited
    assert Node.REPAIR_PATCH.value not in visited
    # 从未通过目标测试 → 审查从未发生，成功也从没被宣称。
    assert reviewer.calls == []
    assert state.review is None
    assert state.status not in {RunStatus.SUCCEEDED, RunStatus.PROVISIONALLY_VERIFIED}
    assert verification_level(state) not in {"m2_verified", "sampled_verified"}
    closure = _closure(harness, state)
    assert closure["target_tests_status"] == "FAIL"
    assert any(str(item).startswith("review:") for item in closure["unexecuted_stages"])


# ------------------------------------------------------------------ 场景 D / §七.4-6


def test_high_review_gets_exactly_one_extra_repair_then_stops_safely(tmp_path: Path) -> None:
    """§七.4/5 + 场景 D：high 风险最多一次额外修复；第二次 high 安全停止并保留问题。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result() for _ in range(6)],
        coder_rounds=[
            [edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH],
            [edit("src/pkg/core.py", "    return value", "    return int(value)"), FINISH],
        ],
    )
    reviewer = _wired(harness, [HIGH, HIGH])
    state = harness.run()

    assert state.review_repairs == 1, state.failure_reasons
    assert state.review_calls == 2
    assert len(state.attempts) == 2
    assert len(reviewer.calls) == 2, "复审必须重新问模型，不能复用旧结论"
    assert state.status is RunStatus.STOPPED, state.failure_reasons
    assert any("unresolved high review issues" in item for item in state.failure_reasons)

    closure = _closure(harness, state)
    assert closure["post_review_repair"] is True
    assert closure["review_calls"] == 2
    assert closure["review_high_issues"] == 1
    assert closure["review_bound_to_candidate"] is True
    assert _report(harness, state)["verification_level"] not in {"m2_verified", "sampled_verified"}
    # 补丁身份确实变了：两轮不能共用一份验证结论。
    first, second = state.attempts
    assert first.patch_hash != second.patch_hash
    assert first.review is not None and first.review.reviewed_patch_hash == first.patch_hash
    assert second.review is not None and second.review.reviewed_patch_hash == second.patch_hash


def test_low_or_medium_review_closes_without_an_extra_repair(tmp_path: Path) -> None:
    """§七.6：只有 low/medium 意见时按既有规则记录并完成收尾。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[
            [edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH]
        ],
    )
    _wired(harness, [LOW])
    state = harness.run()

    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert state.review_repairs == 0
    assert len(state.attempts) == 1
    assert state.review is not None and not state.review.has_high_issues
    closure = _closure(harness, state)
    assert closure["review_executed"] is True
    assert closure["review_high_issues"] == 0
    assert closure["post_review_repair"] is False
    assert _report(harness, state)["verification_level"] == "m2_verified"


# ------------------------------------------------------------------ §七.8 补丁身份


def test_a_review_of_an_older_patch_never_blesses_the_new_candidate(tmp_path: Path) -> None:
    """§七.8：补丁哈希变了之后，旧测试与旧审查证据不得冒充新补丁的结论。

    这里同时钉住三件事：同版本重放**复用**（不重复调用模型、不重复计审查次数）、
    换版本之后 finalize **拒绝**给成功、以及级别判定不给未核对的结论背书。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[
            [edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH]
        ],
    )
    reviewer = _wired(harness, [APPROVED])
    state = harness.run()
    assert state.review is not None

    # 同一版补丁重放：复用结论，绝不第二次问模型。
    calls_before = len(reviewer.calls)
    again = asyncio.run(nodes.review(state, harness.deps))
    assert again["review_calls"] == state.review_calls
    assert again["review"] == state.review
    assert len(reviewer.calls) == calls_before

    # 候选补丁被换成另一版：旧审查结论立刻不作数。
    stale = state.model_copy(
        update={
            "current_patch": f"{state.current_patch}\n# widened after review\n",
            "status": RunStatus.RUNNING,
            "stop_reason": None,
        }
    )
    final = asyncio.run(nodes.finalize(stale, harness.deps))
    assert final["status"] is not RunStatus.SUCCEEDED
    assert any("different patch identity" in item for item in final["failure_reasons"])
    forced = stale.model_copy(update={"status": RunStatus.SUCCEEDED})
    assert verification_level(forced) not in {"m2_verified", "sampled_verified"}


# ------------------------------------------------------------------ §七.12 未执行审查


def test_missing_regression_records_review_as_not_executed_instead_of_calling_it(
    tmp_path: Path,
) -> None:
    """§七.12：审查阶段不可执行时如实标记未完成，且不产出 ``m2_verified``。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[
            [edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH]
        ],
    )
    reviewer = _wired(harness, [APPROVED])
    harness.deps.regression_runner = None
    state = harness.run()

    assert reviewer.calls == [], "前置条件不满足时不得强行调用 Reviewer"
    assert state.review is None
    assert state.review_skip_reason == "regression was not measured"
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED
    closure = _closure(harness, state)
    assert "regression" in closure["unexecuted_stages"]
    assert closure["review_executed"] is False
    assert _report(harness, state)["verification_level"] == "provisionally_verified"
    assert "review was NOT executed" in " ".join(state.failure_reasons)


# ------------------------------------------------------------------ §七.13 双重结论


def test_official_resolved_and_internal_open_loop_are_kept_apart(tmp_path: Path) -> None:
    """§七.13：官方 RESOLVED 与内部 M2 未闭环必须各自保存，互不覆盖。

    这正是首次真实 GLM 运行的报告形状：补丁有效（官方可能判 RESOLVED），但审查
    从未发生，内部只能记到 ``provisionally_verified``。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[
            [edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH]
        ],
    )
    _wired(harness, [APPROVED])
    harness.deps.regression_runner = None
    state = harness.run()

    internal = verification_level(state)
    assert internal == "provisionally_verified"
    # 官方侧：一个 RESOLVED 的结论进来，只被分类，永不被内部状态改写。
    official = SimpleNamespace(resolved=True)
    assert compare_with_internal_m2(official, m2_verified=False) is (
        HarnessDiscrepancy.OFFICIAL_RESOLVED_INTERNAL_NOT_VERIFIED
    )
    assert official.resolved is True

    report = _report(harness, state)
    closure = report["closure"]  # type: ignore[index]
    assert closure["patch_present"] is True  # type: ignore[index]
    assert closure["candidate_patch_sha256"]  # type: ignore[index]
    assert closure["review_executed"] is False  # type: ignore[index]
    assert report["verification_level"] == internal


# ------------------------------------------------------------------ §七.14 状态转移


def test_a_crashed_coding_stage_leaves_no_stale_reservation(tmp_path: Path) -> None:
    """§七.11：预留不是记账。编码阶段异常后重跑，账本里只能有**真实发生**的调用。

    这里制造一次"编码阶段炸在派发中途"的现场：预留被算出来、编码被中断，然后
    换回真 Coder 重跑同一轮。若预留进了账本（或被累积），第二次的可用上限就会比
    第一次小，账本与真实花费也就对不上 —— 这是恢复场景里最贵的一类不一致。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
    )
    _wired(harness, [APPROVED])
    state = _analysis_chain(harness)

    class _Boom:
        max_tool_calls = 30

        def run(self, **_kwargs: object) -> None:
            raise RuntimeError("provider exploded mid-stage")

    real = harness.deps.coder
    harness.deps.coder = _Boom()
    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(nodes.generate_patch(state, harness.deps))
    calls_at_crash = harness.deps.usage.llm_calls
    cost_at_crash = harness.deps.usage.cost
    after_crash = state.model_copy(update={"usage": harness.deps.usage})

    harness.deps.coder = real
    retried = asyncio.run(nodes.generate_patch(after_crash, harness.deps))

    # 重跑用的是同一个天花板：预留没有被"用掉"一次就少一份。
    reserve = harness.deps.budget.closure_reservation(
        after_crash, reviewer_available=True, pending_review_calls=_pending(after_crash)
    )
    assert harness.deps.budget.coding_token_ceiling(after_crash, reserve) == (
        after_crash.budget.max_total_tokens - reserve.tokens
    )
    # 账本只包含真实调用：异常那一次没有留下幻影费用，也没有重复计费。
    assert harness.deps.usage.llm_calls == calls_at_crash + 30
    assert harness.deps.usage.cost == cost_at_crash
    assert retried["coding_stop_reason"] is StopReason.BUDGET_TOOL_CALLS
    assert retried["usage"].llm_calls == calls_at_crash + 30


def test_closure_reservation_blocks_the_coding_dispatch_before_any_spend(tmp_path: Path) -> None:
    """§七.10：剩余预算撑不起闭环时，编码派发前就阻断，而不是等烧完才发现审查没钱。

    上限按"分析链实际花掉的 token + 闭环预留 - 1"来定：这样阻断的原因只能是预留，
    而不是随手挑的一个数字。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
        input_tokens=5_000,
    )
    _wired(harness, [APPROVED])
    state = _analysis_chain(harness)
    spent = state.usage.total_tokens
    reserve = harness.deps.budget.closure_reservation(
        state, reviewer_available=True, pending_review_calls=_pending(state)
    )
    assert reserve.tokens > 0

    tight = state.model_copy(
        update={
            "budget": state.budget.model_copy(
                update={"max_total_tokens": spent + reserve.tokens - 1}
            )
        }
    )
    update = asyncio.run(nodes.generate_patch(tight, harness.deps))

    assert update["status"] is RunStatus.BUDGET_EXHAUSTED
    assert update["stop_reason"] is StopReason.BUDGET_TOKENS
    assert "reserved" in " ".join(update["failure_reasons"])
    # 关键：编码阶段一次模型都没问，所以闭环预留确实被保住了。
    assert harness.llm.purposes().count("coder") == 0

    # 负控制：把预留腾出来之后，同一条链路必须正常派发（30 次调用全部跑完）。
    roomy = state.model_copy(
        update={
            "budget": state.budget.model_copy(
                update={"max_total_tokens": spent + reserve.tokens + 200_000}
            )
        }
    )
    dispatched = asyncio.run(nodes.generate_patch(roomy, harness.deps))
    assert "status" not in dispatched, dispatched.get("failure_reasons")
    assert dispatched["coding_stop_reason"] is StopReason.BUDGET_TOOL_CALLS
    assert sum(harness.llm.purposes().count(p) for p in ("coder", "coder_relocalize")) == 30


def test_a_patch_in_hand_keeps_the_graph_on_the_verification_path(tmp_path: Path) -> None:
    """§七.14 的节点级最小件：局部额度耗尽**不写终态**，路由因此不会直奔 finalize。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
    )
    state = _analysis_chain(harness)
    update = asyncio.run(nodes.generate_patch(state, harness.deps))

    assert "status" not in update, update.get("failure_reasons")
    assert update["coding_stop_reason"] is StopReason.BUDGET_TOOL_CALLS
    assert update["frozen_patch_hash"] == patch_hash(update["current_patch"])
    after = state.model_copy(update=update)
    assert route_after_rules(after, Node.VALIDATE_PATCH) is Node.VALIDATE_PATCH

    # 补丁真的过了校验器之后，路由必须继续往验证与审查走。
    validated = after.model_copy(update=asyncio.run(nodes.validate_patch(after, harness.deps)))
    assert validated.current_attempt is not None
    assert validated.current_attempt.validation.passed
    assert route_after_validate_patch(validated) is Node.RUN_TARGET_TESTS


def test_no_patch_in_hand_short_circuits_the_graph_to_finalize(tmp_path: Path) -> None:
    """§七.14 的反向件：没有补丁时局部停止**就是**终态，路由必须直奔 finalize。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[[READ]],
        budget=RepairBudget(max_tool_calls_per_iteration=1),
    )
    state = _analysis_chain(harness)
    update = asyncio.run(nodes.generate_patch(state, harness.deps))

    assert update["status"] is RunStatus.BUDGET_EXHAUSTED
    assert update["stop_reason"] is StopReason.BUDGET_TOOL_CALLS
    after = state.model_copy(update=update)
    assert route_after_rules(after, Node.VALIDATE_PATCH) is Node.FINALIZE


# ------------------------------------------------------------------ §三.1 预留的单价口径


def _priced_config(*, reviewer_input: float, reviewer_output: float) -> LLMConfig:
    """Coder 与 Reviewer 用不同别名、不同单价的配置。

    只用一个模型时"账本平均单价"恰好等于"审查单价"，预留算错也看不出来。
    """
    roles = dict.fromkeys(KNOWN_ROLES, "cheap")
    roles["reviewer"] = "pricey"
    return LLMConfig.model_validate(
        {
            "providers": {
                "p": {
                    "name": "p",
                    "base_url": "https://.invalid/v1",
                    "api_key_env": "TEST_KEY_ONLY",
                }
            },
            "models": {
                "cheap": {
                    "alias": "cheap",
                    "provider": "p",
                    "model": "coder-model",
                    "price": {
                        "currency": "CNY",
                        "input_per_mtok": 1.0,
                        "output_per_mtok": 1.0,
                    },
                },
                "pricey": {
                    "alias": "pricey",
                    "provider": "p",
                    "model": "review-model",
                    "price": {
                        "currency": "CNY",
                        "input_per_mtok": reviewer_input,
                        "output_per_mtok": reviewer_output,
                    },
                },
            },
            "roles": roles,
        }
    )


def test_the_closure_reservation_is_priced_by_the_reviewers_own_model(
    tmp_path: Path,
) -> None:
    """图级 §三.1：按审查模型的单价会阻断派发，按账本平均单价不会——差别必须可见。

    首轮真实运行之后剩下的就是这一条：闭环的走向修好了，但"给审查留多少钱"仍然
    按已经跑过的那些调用（编码模型）估。编码便宜、审查贵时，这种预留不是保守值。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
        input_tokens=5_000,
        cost_per_call=0.01,
        llm_config=_priced_config(reviewer_input=4.0, reviewer_output=16.0),
    )
    _wired(harness, [APPROVED])
    state = _analysis_chain(harness)
    spent = state.usage.cost
    tight = state.model_copy(
        update={"budget": state.budget.model_copy(update={"max_cost": spent + 0.2})}
    )
    priced = harness.deps.budget.closure_reservation(
        tight,
        reviewer_available=True,
        pending_review_calls=_pending(tight),
        reviewer_config=harness.deps.llm_config,
    )
    ledger = BudgetManager().closure_reservation(
        tight, reviewer_available=True, pending_review_calls=_pending(tight)
    )
    expected = 2 * (4.0 * REVIEW_CONTEXT_TOKENS + 16.0 * REVIEW_OUTPUT_TOKENS) / 1_000_000
    assert priced.basis == "reviewer-model:pricey"
    assert priced.cost == pytest.approx(expected)
    assert ledger.basis == "observed-unit"
    # 前提：两种口径正好落在"付得起 / 付不起"两侧，否则本用例什么也没证明。
    assert ledger.cost < 0.2 < priced.cost

    update = asyncio.run(nodes.generate_patch(tight, harness.deps))
    assert update["status"] is RunStatus.BUDGET_EXHAUSTED
    assert update["stop_reason"] is StopReason.BUDGET_COST
    reasons = " ".join(update["failure_reasons"])
    assert "reserved for the verification/review closure" in reasons
    assert "basis=reviewer-model:pricey" in reasons
    assert harness.llm.purposes().count("coder") == 0, "被预留拦下时一个编码请求都不许发"

    # 负控制：审查与编码同价时同一条链路必须照常派发，阻断不是无条件焊死的。
    harness.deps.llm_config = _priced_config(reviewer_input=1.0, reviewer_output=1.0)
    cheap = harness.deps.budget.closure_reservation(
        tight,
        reviewer_available=True,
        pending_review_calls=_pending(tight),
        reviewer_config=harness.deps.llm_config,
    )
    assert cheap.cost < 0.2
    dispatched = asyncio.run(nodes.generate_patch(tight, harness.deps))
    assert "status" not in dispatched, dispatched.get("failure_reasons")
    assert dispatched["frozen_patch_hash"] == patch_hash(dispatched["current_patch"])
    assert harness.llm.purposes().count("coder") > 0


def test_reservation_falls_back_to_the_ledger_and_says_so(tmp_path: Path) -> None:
    """没有可采信的单价时退回账本口径，并把口径写进阻断消息而不是留给人猜。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
        input_tokens=5_000,
        cost_per_call=0.01,
    )
    _wired(harness, [APPROVED])
    state = _analysis_chain(harness)
    reserve = BudgetManager().closure_reservation(
        state, reviewer_available=True, pending_review_calls=_pending(state)
    )
    assert reserve.basis == "observed-unit"
    # 上限只比"已花 + 预留"少一分：剩下的确实撑不起预留，阻断只能来自预留本身。
    tight = state.model_copy(
        update={
            "budget": state.budget.model_copy(
                update={"max_cost": state.usage.cost + reserve.cost - 0.01}
            )
        }
    )
    update = asyncio.run(nodes.generate_patch(tight, harness.deps))
    assert update["stop_reason"] is StopReason.BUDGET_COST
    assert "basis=observed-unit" in " ".join(update["failure_reasons"])
    assert harness.llm.purposes().count("coder") == 0


# ---------------------------------------------------- §三.2 前置模型调用也吃预留


def _at_diagnose(harness) -> RepairState:
    """真跑到 ``diagnose`` 的节点入口。

    补丁必须来自**真实 Workspace**：``DiagnosisAgent`` 会核对 ``current_patch`` 就是
    当前 canonical diff，手工捏一个补丁字符串会先在身份核对里炸掉，那样测不到预算。
    """
    state = _analysis_chain(harness)
    for name in (
        Node.GENERATE_PATCH.value,
        Node.VALIDATE_PATCH.value,
        Node.RUN_TARGET_TESTS.value,
    ):
        state = state.model_copy(update=asyncio.run(getattr(nodes, name)(state, harness.deps)))
    assert state.execution is not None
    assert state.execution.status is ExecutionStatus.FAIL, state.execution.status
    return state


def _with_tokens(state: RepairState, limit: int) -> RepairState:
    return state.model_copy(
        update={"budget": state.budget.model_copy(update={"max_total_tokens": limit})}
    )


def _with_cost(state: RepairState, limit: float) -> RepairState:
    return state.model_copy(update={"budget": state.budget.model_copy(update={"max_cost": limit})})


def _diagnose_harness(tmp_path: Path) -> object:
    """一次"补丁有效但目标测试仍然失败"的真实运行现场（FakeSandbox 脚本化）。"""
    return build_harness(
        tmp_path,
        results=[assertion_failure(f"{TARGET}::test_normalize", message="assert 1 == 3")],
        coder_rounds=[
            [edit("src/pkg/core.py", "    return value - 1", "    return value - 2"), FINISH]
        ],
        diagnoses=[diagnosis("refine", line=2, text="return value - 2")],
        baseline_results=[],
        input_tokens=5_000,
    )


def test_diagnose_cannot_spend_the_money_reserved_for_the_review(tmp_path: Path) -> None:
    """§三.2：Diagnose 排在审查之前，剩余预算撑不起预留时**必须在发出请求前**被拒。

    第一轮修复只让编码派发让路，诊断没让 —— 现象一模一样：审查最后仍然被记成
    "未执行"，只是这次钱是被诊断花掉的。上限钉在"已花 + 预留 - 1"，所以阻断只能
    来自预留；负控制把预留腾出来之后同一条边必须照常跑出诊断结论。
    """
    harness = _diagnose_harness(tmp_path)
    reviewer = _wired(harness, [APPROVED])
    state = _at_diagnose(harness)
    spent = state.usage.total_tokens
    reserve = harness.deps.budget.closure_reservation(
        state, reviewer_available=True, pending_review_calls=_pending(state)
    )
    assert reserve.tokens > 0

    tight = _with_tokens(state, spent + reserve.tokens - 1)
    update = asyncio.run(nodes.diagnose(tight, harness.deps))
    assert update["status"] is RunStatus.BUDGET_EXHAUSTED
    assert update["stop_reason"] is StopReason.BUDGET_TOKENS
    assert "reserved for the verification/review closure" in " ".join(update["failure_reasons"])
    assert harness.llm.purposes().count("diagnoser") == 0, "被预留拦下时一个诊断请求都不许发"
    assert reviewer.calls == [], "诊断被拦下不等于审查可以顺势跳过记账"

    # §三.3 连续消耗：被拦下之后再试仍然是同一判定，而且**没有**任何新支出。
    again = asyncio.run(nodes.diagnose(tight, harness.deps))
    assert again["stop_reason"] is StopReason.BUDGET_TOKENS
    assert harness.deps.usage.total_tokens == spent, "预算拒绝不得产生半笔支出"

    # §三.3 事件证据：预算检查事件必须带着预留口径落盘，事后才核得出是哪一口价拦的。
    records = [
        json.loads(line) for line in harness.events_path.read_text(encoding="utf-8").splitlines()
    ]
    blocked_events = [
        item
        for item in records
        if item.get("node") == "diagnose" and item.get("status") == "exhausted"
    ]
    assert blocked_events, [item.get("status") for item in records]
    payload = json.dumps(blocked_events[-1], ensure_ascii=False)
    assert "closure_reservation" in payload and "basis" in payload, payload[:400]

    # 负控制：把预留腾出来，同一条边照常执行。
    roomy = _with_tokens(state, spent + reserve.tokens + 200_000)
    ran = asyncio.run(nodes.diagnose(roomy, harness.deps))
    assert "status" not in ran, ran.get("failure_reasons")
    assert harness.llm.purposes().count("diagnoser") == 1


def test_the_reservation_is_released_once_no_review_is_pending(tmp_path: Path) -> None:
    """§三.6：审查已经绑定当前候选补丁时预留归零，不得永久占用预算。

    同一份"很紧"的上限在两个方向上都必须给出**正确**答案：还欠审查时拦住前置调用
    （上一个用例），不再欠时放行收尾前的最后一步。后者不成立的话，预留就变成一把
    把整个 run 焊死的锁。
    """
    harness = _diagnose_harness(tmp_path)
    _wired(harness, [APPROVED])
    state = _at_diagnose(harness)
    digest = patch_hash(state.current_patch)
    from codepilot.schemas.review import ReviewResult

    reviewed = state.model_copy(
        update={
            "review": ReviewResult(approved=True, summary="ok", reviewed_patch_hash=digest),
            "review_calls": 1,
        }
    )
    assert _pending(reviewed) == 0
    # 剩余预算只够一次诊断：预留还在时这条路必被拦死，现在必须放行。
    pinned = _with_tokens(reviewed, reviewed.usage.total_tokens + 1)
    assert (
        harness.deps.budget.closure_reservation(
            pinned, reviewer_available=True, pending_review_calls=_pending(pinned)
        ).tokens
        == 0
    )
    update = asyncio.run(nodes.diagnose(pinned, harness.deps))
    assert "status" not in update, update.get("failure_reasons")
    assert harness.llm.purposes().count("diagnoser") == 1


def test_an_unsettled_precharge_is_visible_to_the_budget_gate(tmp_path: Path) -> None:
    """§三.4：派发层已预扣、还没写进 ``state.usage`` 的钱，必须进预算准入。

    ``Usage`` 是响应回来之后才累加的，所以飞行中的那笔钱在账本上是隐形的。不把它
    算进"已花"，预留就是在一段并不存在的 headroom 上又扣一次；把它算两次，就会在
    同一笔钱上拦两次。
    """
    harness = _diagnose_harness(tmp_path)
    _wired(harness, [APPROVED])
    state = _at_diagnose(harness)
    reserve = harness.deps.budget.closure_reservation(
        state, reviewer_available=True, pending_review_calls=_pending(state)
    )
    settled = state.usage.cost
    harness.deps.outstanding_cost = lambda: (0.6, "CNY")
    tight = state.model_copy(
        update={
            "budget": state.budget.model_copy(
                update={"max_cost": settled + 0.6 + reserve.cost - 0.01}
            )
        }
    )
    update = asyncio.run(nodes.diagnose(tight, harness.deps))
    assert update["stop_reason"] is StopReason.BUDGET_COST
    assert harness.llm.purposes().count("diagnoser") == 0

    # 负控制：同一笔钱结算进账本（outstanding 归零、已结算加上它）之后，判定给的
    # 余量必须与"只算一次"一致 —— 否则就是在同一笔费用上扣两次。
    harness.deps.outstanding_cost = lambda: None
    after = state.model_copy(
        update={
            "budget": state.budget.model_copy(
                update={"max_cost": settled + 0.6 + reserve.cost + 200.0}
            )
        }
    )
    ran = asyncio.run(nodes.diagnose(after, harness.deps))
    assert "status" not in ran, ran.get("failure_reasons")


def test_a_hard_budget_stop_is_never_masked_by_the_local_tool_cap(tmp_path: Path) -> None:
    """§四.1 负控制：硬预算耗尽不能被"局部额度"这个更轻的原因盖掉。

    局部额度与硬预算同时耗尽时，``stop_reason`` 只容得下一个值。要守住的是**信息**：
    两个原因都必须进 ``all_reasons``、都必须写进 failure_reasons 的解释里、终态必须
    是 ``budget_exhausted``，而报告必须单独标出"硬费用上限触顶"。否则读者会把"钱花完
    了"读成"再多给几次工具调用就能继续"，那是一笔不存在的余地。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
        input_tokens=5_000,
        cost_per_call=0.01,
    )
    _wired(harness, [APPROVED])
    state = _analysis_chain(harness)
    coded = state.model_copy(update=asyncio.run(nodes.generate_patch(state, harness.deps)))
    assert coded.coding_stop_reason is StopReason.BUDGET_TOOL_CALLS
    assert coded.status is RunStatus.RUNNING, "有补丁在手时局部额度不写终态（场景 A）"

    overspent = coded.model_copy(
        update={"budget": coded.budget.model_copy(update={"max_cost": 0.0})}
    )
    update = asyncio.run(nodes.check_budget(overspent, harness.deps))
    decision = harness.deps.rules.evaluate(overspent)

    assert update["status"] is RunStatus.BUDGET_EXHAUSTED
    assert update["stop_reason"] in {StopReason.BUDGET_TOOL_CALLS, StopReason.BUDGET_COST}
    assert StopReason.BUDGET_TOOL_CALLS in decision.all_reasons
    assert StopReason.BUDGET_COST in decision.all_reasons
    text = " ".join(update["failure_reasons"])
    assert "budget_tool_calls" in text and "budget_cost" in text, text

    closure = VerificationClosure.from_state(overspent)
    assert closure.coding_stop_reason == "budget_tool_calls"
    assert closure.cost_hard_cap == 0.0 and closure.cost_hard_cap_hit is True
    assert closure.budget_usage["cost"].endswith("/0"), closure.budget_usage["cost"]
    assert verification_level(overspent) not in {"m2_verified", "sampled_verified"}
    # 局部原因与硬预算各记各的：``stop_note`` 不能顶掉 ``stop_reason`` 的权威口径。
    assert overspent.stop_reason is None


def test_resuming_a_finished_closure_replays_stages_without_rebilling(tmp_path: Path) -> None:
    """§四.1 恢复语义：重进已完成阶段既不重跑容器、也不重复问模型或重复记账。

    恢复出来的 run 面对的是**旧现场**：Operation Journal 里已经有 L2/L3 的结果，
    审查结论已经绑定当前补丁。正确做法是复用；任何"再执行一遍"都会把测试次数、审查
    次数与费用同时推高，而那正是 G-04/G-13 禁止的清零重烧。

    这里刻意**逐节点驱动**而不是 ``harness.run()``：只有手里同时握着"审查还没发生"与
    "审查已经发生"两个现场，才分得清"未完成的阶段真的被执行了"和"已完成的阶段真的被
    复用了"——只看最终状态这两件事长得一模一样。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
    )
    reviewer = _wired(harness, [APPROVED, APPROVED])

    state = _analysis_chain(harness)
    for name in (
        Node.BUILD_ENV.value,
        Node.GENERATE_PATCH.value,
        Node.VALIDATE_PATCH.value,
        Node.RUN_TARGET_TESTS.value,
    ):
        state = state.model_copy(update=asyncio.run(getattr(nodes, name)(state, harness.deps)))
    state = state.model_copy(update=asyncio.run(nodes.run_regression(state, harness.deps)))
    assert state.regression is not None and state.regression.complete
    assert reviewer.calls == [], "还没进审查节点时不许提前问模型"

    reviewed = state.model_copy(update=asyncio.run(nodes.review(state, harness.deps)))
    assert len(reviewer.calls) == 1 and reviewed.review_calls == 1
    usage_before = harness.deps.usage.model_dump(mode="json")
    test_runs_before = harness.deps.test_runs
    assert test_runs_before == reviewed.test_runs

    # 重进已完成阶段：回归走 Operation Journal 复用，审查走同版补丁复用。
    replayed_regression = asyncio.run(nodes.run_regression(reviewed, harness.deps))
    replayed_review = asyncio.run(nodes.review(reviewed, harness.deps))

    assert harness.deps.test_runs == test_runs_before, "回归必须走 Journal 复用，不得再起容器"
    if "regression" in replayed_regression and replayed_regression["regression"] is not None:
        assert replayed_regression["regression"] == reviewed.regression
    assert replayed_review["review_calls"] == reviewed.review_calls
    assert replayed_review["review"] == reviewed.review
    assert len(reviewer.calls) == 1, "同版补丁的审查结论只属于那一次调用"
    assert harness.deps.usage.model_dump(mode="json") == usage_before, "复用不得再记一笔费用"
    assert reviewed.review is not None
    assert reviewed.review.reviewed_patch_hash == patch_hash(reviewed.current_patch)


def _llm_call_events(harness) -> list[dict[str, object]]:
    """落盘的 ``llm_call`` 事件：一次 HTTP 请求应当只对应一条。"""
    return [
        item
        for item in (
            json.loads(line)
            for line in harness.events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if item.get("type") == "llm_call"
    ]


class _OverrideLayer:
    """与冒烟脚本的 ``--temperature`` / ``--max-output-tokens`` 覆盖层同形的装饰器。

    真实运行就是因为它把 ``UsageTrackingProvider`` 藏到了第二层，
    ``isinstance(provider, UsageTrackingProvider)`` 判成"没有统计层"，于是 Reviewer 的
    那一次调用被记了两遍（账本 26 次 vs 派发账本 25 次）。
    """

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def complete(self, request: object) -> object:
        return self.inner.complete(request)  # type: ignore[union-attr]

    def close(self) -> None:
        self.inner.close()  # type: ignore[union-attr]


def test_a_reviewer_behind_a_decorator_is_booked_exactly_once(tmp_path: Path) -> None:
    """§三.5 的真实形态：审查的这一次调用，账本与事件都只能有一条。

    预留不是记账，但**真实发生的调用必须是记账**：多记一次就会让预算判定看到一笔
    并不存在的支出，闭环额度因此被凭空吃掉一份。
    """
    from codepilot.llm.tracking import UsageTrackingProvider

    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
    )
    harness.deps.regression_runner = RegressionRunner()
    llm = FakeLLM([APPROVED])
    harness.deps.reviewer = PatchReviewer(
        _OverrideLayer(UsageTrackingProvider(llm, usage=harness.usage, sink=harness.deps.events))
    )

    state = _analysis_chain(harness)
    for name in (
        Node.BUILD_ENV.value,
        Node.GENERATE_PATCH.value,
        Node.VALIDATE_PATCH.value,
        Node.RUN_TARGET_TESTS.value,
    ):
        state = state.model_copy(update=asyncio.run(getattr(nodes, name)(state, harness.deps)))
    state = state.model_copy(update=asyncio.run(nodes.run_regression(state, harness.deps)))
    assert state.regression is not None and state.regression.complete

    calls_before = harness.usage.llm_calls
    events_before = len(_llm_call_events(harness))
    reviewed = state.model_copy(update=asyncio.run(nodes.review(state, harness.deps)))
    assert reviewed.review is not None and reviewed.review_calls == 1

    assert harness.usage.llm_calls - calls_before == 1, (
        "装饰过的 Reviewer provider 不得开出第二本账"
    )
    assert len(_llm_call_events(harness)) - events_before == 1, "一次审查只能有一条 llm_call 事件"


def test_a_review_blocked_by_its_own_budget_records_the_stage_as_unexecuted(
    tmp_path: Path,
) -> None:
    """§二.2 预算不足：审查被自己的专属额度拦下时，未执行原因必须精确到"是额度"。

    报告里 ``review_executed=False`` 只说"没审"；``closure`` 的未执行阶段必须能区分
    "没接 Reviewer""前置条件不满足""没钱/没次数审"。这里还要顺带证明**没有**向模型
    要过审查——否则等于把没执行写成执行了一半。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[THIRTY_CALLS],
        coder_relocalize_replies=[READ],
    )
    reviewer = _wired(harness, [APPROVED, APPROVED])
    state = harness.run()
    assert state.review is not None and state.review_calls == 1
    calls_after_run = len(reviewer.calls)

    last = state.attempts[-1].model_copy(update={"review": None})
    before = state.model_copy(
        update={
            "attempts": [*state.attempts[:-1], last],
            "review": None,
            "review_calls": state.budget.max_review_calls,
            "status": RunStatus.RUNNING,
            "stop_reason": None,
        }
    )
    update = asyncio.run(nodes.review(before, harness.deps))

    assert update["status"] is RunStatus.BUDGET_EXHAUSTED
    assert update["stop_reason"] is StopReason.BUDGET_REVIEW_CALLS
    assert update["review_skip_reason"] == (
        "not executed: budget_review_calls spent before any call"
    )
    assert len(reviewer.calls) == calls_after_run, "拦下审查时不许再向模型要结论"

    # 复用优先于额度：同一版补丁早已审过时，即使额度用满也必须复用，不重复调用。
    recorded = state.attempts[-1].review
    assert recorded is not None
    replay = asyncio.run(
        nodes.review(
            state.model_copy(
                update={"review": None, "review_calls": state.budget.max_review_calls}
            ),
            harness.deps,
        )
    )
    assert replay["review"] is not None and replay["review"].reviewed_patch_hash == (
        patch_hash(state.current_patch)
    )
    assert replay["review_calls"] == state.budget.max_review_calls
    assert len(reviewer.calls) == calls_after_run
