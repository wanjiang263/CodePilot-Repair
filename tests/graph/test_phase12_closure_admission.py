"""Phase 12：预算感知的闭环准入（DA-024）的图级回归测试。

要修的缺陷形状来自第三次真实单题运行（glm14，`docs/notes/phase11-closure-budget.md` §11）：
``max_test_runs=12`` 在"两轮修复 + 每轮目标测试/L2/L3"这个形状下**先于费用耗尽**——那次
只花了 ¥0.192/¥10，把运行判死的是测试次数。第二轮候选的 L3 因此在门外被拦下
（``regression.complete=false``），审查从未执行，报告只能写
``unexecuted_stages=['regression_incomplete', 'review:…']``。

这不是"额度太小"的问题：把 12 改成 20 只是把同一件事推到更远的地方。真正缺的是
**在启动修复轮之前就知道这一版候选能不能验完**，所以这里逐个钉住任务书 §2.1 的要求：

* 额度够 → 修复、验证、回归、审查按预期发生（还要求回归**没有被降级**）；
* 额度不够 → 修复轮在派发之前被拒，且被拒的是"再来一轮"，不是"再验一次"：
  已经审过的补丁、它的审查结论与回归证据必须原样留下；
* 边界是精确的算术：``剩余 == 闭环所需`` 放行，``剩余 == 所需 - 1`` 拒绝；
* 编码阶段自己调用 ``run_tests`` 的份额从闭环所需之外扣，模型不能把闭环那几次借走；
* 重放/恢复不重复消耗额度，也不重复计费。

夹具与 Phase 8/9/11 一致：真实 git 仓库、真实索引、真实 PatchValidator、真实
RegressionRunner 与 PatchReviewer，只有模型与 Sandbox 是脚本化的。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

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
from codepilot.graph import nodes
from codepilot.graph.budget import (
    CLOSURE_FLAKY_RETRY_RESERVE,
    CLOSURE_TARGET_TEST_RUNS,
    ClosureTestPlan,
)
from codepilot.graph.regression import RegressionRunner
from codepilot.graph.routing import Node, selected_target_tests
from codepilot.graph.stop_rules import patch_hash
from codepilot.llm.fake import FakeLLM
from codepilot.report import VerificationClosure, verification_level
from codepilot.sandbox.fake import scripted_result
from codepilot.schemas.budget import RepairBudget
from codepilot.schemas.enums import ExecutionStatus, RunStatus, StopReason
from codepilot.schemas.regression import RegressionLevel, RegressionMode
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
#: 一次编辑 + 一次收束 = 两个 coder 请求；断言"第二轮编码从未发生"就是数这个。
ROUND_A: list[dict[str, Any]] = [
    edit("src/pkg/core.py", "    return value - 1", "    return value"),
    FINISH,
]
ROUND_B: list[dict[str, Any]] = [
    edit("src/pkg/core.py", "    return value", "    return int(value)"),
    FINISH,
]
READ: dict[str, Any] = {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}}
RUN_TESTS: dict[str, Any] = {"tool": "run_tests", "arguments": {"test_ids": [TARGET]}}


def _report(harness: Any, state: RepairState) -> dict[str, Any]:
    path = harness.deps.artifacts.run_dir / state.report_artifacts["report_json"]
    return json.loads(path.read_text(encoding="utf-8"))


def _closure(harness: Any, state: RepairState) -> dict[str, Any]:
    return _report(harness, state)["closure"]


def _markdown(harness: Any, state: RepairState) -> str:
    path = harness.deps.artifacts.run_dir / state.report_artifacts["report_markdown"]
    return path.read_text(encoding="utf-8")


def _wired(harness: Any, reviewer_replies: list[dict[str, object]]) -> FakeLLM:
    reviewer = FakeLLM(reviewer_replies)
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(reviewer)
    return reviewer


def _visited(state: RepairState) -> list[str]:
    return [item["node"] for item in state.events]


def _admission_events(harness: Any) -> list[dict[str, Any]]:
    """事件流里的闭环准入读数——报告不能是唯一说得出这笔账的地方。"""
    rows = [
        json.loads(line)
        for line in harness.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [row for row in rows if (row.get("extra") or {}).get("closure_test_runs")]


# ------------------------------------------------------------------ §2.1.3 拒绝启动


def test_a_second_round_that_cannot_finish_its_closure_is_never_started(tmp_path: Path) -> None:
    """剩余 2 次而闭环要 3 次 → 修复轮**在派发之前**被拒，且拒得有据可查。

    glm14 的真实形状：第一轮挣到了完整闭环（目标测试 PASS、L2/L3 跑完、审过一版），
    审查给了 high，但剩下的测试次数已经买不起"第二版的完整闭环"。修复前的行为是照样
    修、照样跑，最后卡在 L3 之外，产出一版**审查未执行**的新候选。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[ROUND_A],
        budget=RepairBudget(max_test_runs=6),
    )
    reviewer = _wired(harness, [HIGH, APPROVED])
    state = harness.run()

    # 前提：额度确实买不起下一轮（4 次已花，还剩 2 次，闭环要 3 次）。
    assert harness.deps.test_runs == 4
    admission = state.closure_admission
    assert admission is not None
    assert admission.feasible is False
    assert admission.required == 3
    assert admission.remaining == 2
    assert admission.trigger == "review"

    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert state.stop_reason is StopReason.BUDGET_CLOSURE_TEST_RUNS
    # 触发本轮的算式必须原样出现在失败原因里，而不是只留一个标签。
    assert any("closure requires 3" in item for item in state.failure_reasons), (
        state.failure_reasons
    )

    # **零新增支出**：没有第二次编码、没有新的容器执行、没有向审查模型要第二个结论。
    assert harness.llm.purposes().count("coder") == 2, "第二轮编码从未发生"
    assert len(reviewer.calls) == 1
    assert harness.deps.test_runs == 4
    assert state.review_calls == 1
    assert state.review_repairs == 0, "被拒的轮次不算已经修过一轮"

    # 不产出新候选：手里的补丁还是那一版，冻结身份没动。
    assert len(state.attempts) == 1
    assert state.frozen_patch_hash == patch_hash(state.current_patch)
    assert_original_clean(harness)

    closure = _closure(harness, state)
    assert closure["patch_present"] is True
    assert closure["post_review_repair"] is False
    assert closure["regression_complete"] is True
    assert closure["review_executed"] is True
    assert closure["review_high_issues"] == 1
    assert closure["review_bound_to_candidate"] is True
    assert _report(harness, state)["verification_level"] == "budget_exhausted"
    assert any(str(item).startswith("repair_round:") for item in closure["unexecuted_stages"])


def test_the_refused_round_keeps_the_reviewed_patch_and_its_evidence(tmp_path: Path) -> None:
    """§2.1.3：被拒之后，已审查的补丁、审查里留下的问题与回归证据都还在。

    这一条是"安全终止"的实质内容：拒绝新一轮不能顺手把上一轮挣到的证据抹掉，
    否则 run 结束时既没有新候选、也没有旧候选的结论，报告只能写"什么都没发生"。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[ROUND_A],
        budget=RepairBudget(max_test_runs=6),
    )
    _wired(harness, [HIGH, APPROVED])
    state = harness.run()

    attempt = state.current_attempt
    assert attempt is not None
    assert attempt.review is not None and attempt.review.has_high_issues
    assert attempt.review.reviewed_patch_hash == attempt.patch_hash
    assert attempt.regression is not None and attempt.regression.complete
    assert attempt.execution is not None and attempt.execution.status.value == "PASS"
    # 审查提出的东西必须在报告里看得见（不能因为"没能再修一轮"就消失）。
    assert state.review is not None
    description = state.review.issues[0].description
    assert description in _markdown(harness, state)
    assert _closure(harness, state)["review_high_issues"] == 1
    assert "REFUSED before dispatch" in _markdown(harness, state)


def test_refusal_is_idempotent_under_replay_and_recovery(tmp_path: Path) -> None:
    """重放与跨进程恢复都不重复消耗额度：同一条边再走一次，判定与支出都不变。

    恢复场景里同一个节点可能被重新执行一次（崩溃发生在 checkpoint 写入之前）。
    准入判定是**纯读数**，所以第二次必须给出同一个结论、且不产生任何副作用。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[ROUND_A],
        budget=RepairBudget(max_test_runs=6),
    )
    _wired(harness, [HIGH])
    state = harness.run()
    assert state.stop_reason is StopReason.BUDGET_CLOSURE_TEST_RUNS

    runs = harness.deps.test_runs
    calls = harness.deps.usage.llm_calls
    readings = len(_admission_events(harness))

    replayed = asyncio.run(nodes.repair_patch(state, harness.deps))
    assert replayed["stop_reason"] is StopReason.BUDGET_CLOSURE_TEST_RUNS
    assert harness.deps.test_runs == runs, "重放不得多花一次容器执行"
    assert harness.deps.usage.llm_calls == calls, "重放不得多记一次模型调用"
    assert replayed["closure_admission"] == state.closure_admission

    # 换一个"新进程"：用 state 的落账次数重新播种计数器，判定必须逐字相同。
    harness.deps.accounting.seed(state.test_runs)
    fresh = asyncio.run(nodes.repair_patch(state, harness.deps))
    assert fresh["stop_reason"] is StopReason.BUDGET_CLOSURE_TEST_RUNS
    assert fresh["closure_admission"].detail == state.closure_admission.detail
    assert len(_admission_events(harness)) == readings + 2, "每次判定各留一行读数，不多不少"


# ------------------------------------------------------------------ §2.3.1 额度充足


def test_a_round_with_room_completes_repair_tests_regression_and_review(
    tmp_path: Path,
) -> None:
    """额度刚好够（剩余 == 所需）→ 修复、验证、回归、审查按预期全部发生。

    这是上一个用例的负控制：同一形状、只差一个计数单位（7 → 8）。放行时新候选必须
    重新挣到全套证据，包括**绑定第二版补丁**的审查结论。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result() for _ in range(6)],
        coder_rounds=[ROUND_A, ROUND_B],
        budget=RepairBudget(max_test_runs=7),
    )
    reviewer = _wired(harness, [HIGH, APPROVED])
    state = harness.run()

    admission = state.closure_admission
    assert admission is not None and admission.feasible
    assert admission.remaining == admission.required == 3
    assert admission.coder_share == 0

    visited = _visited(state)
    assert visited.count(Node.REPAIR_PATCH.value) == 1
    assert harness.llm.purposes().count("coder") == 4, "两轮编码都真的发生了"
    assert len(reviewer.calls) == 2, "复审必须重新问模型"

    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert state.stop_reason is None
    assert len(state.attempts) == 2
    assert state.review_repairs == 1
    # baseline 2 + first closure 2 (target + L2; L3 reuses L2) + second closure 2
    assert harness.deps.test_runs == 6
    assert _report(harness, state)["verification_level"] == "m2_verified"

    closure = _closure(harness, state)
    assert closure["unexecuted_stages"] == []
    assert closure["post_review_repair"] is True
    assert closure["review_calls"] == 2
    assert closure["review_bound_to_candidate"] is True
    first, second = state.attempts
    assert second.review is not None
    assert second.review.reviewed_patch_hash == second.patch_hash != first.patch_hash
    assert first.review is not None and first.review.reviewed_patch_hash == first.patch_hash


def test_the_allowance_never_buys_a_quieter_regression(tmp_path: Path) -> None:
    """§2.1.4：准入不足绝不是把 FULL 回归悄悄降成 SAMPLED 的理由。

    放行的那一轮跑完之后，回归模式仍然是 ``full``、三个层级都执行过、抽样种子没被
    写过——省额度只能靠"不启动这一轮"，不能靠少验。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result() for _ in range(6)],
        coder_rounds=[ROUND_A, ROUND_B],
        budget=RepairBudget(max_test_runs=8),
    )
    _wired(harness, [HIGH, APPROVED])
    state = harness.run()

    regression = state.regression
    assert regression is not None
    assert regression.mode is RegressionMode.FULL
    assert regression.sampled_ratio is None
    assert regression.seed is None
    assert set(regression.levels_run) >= {
        RegressionLevel.TARGET,
        RegressionLevel.IMPACT,
        RegressionLevel.FULL,
    }
    assert regression.complete and not regression.new_failures
    assert _closure(harness, state)["regression_levels"] == ["L1_target", "L2_impact", "L3_full"]
    admission = state.closure_admission
    assert admission is not None
    assert admission.coder_share + admission.required <= admission.remaining, (
        "份额与闭环所需之和从不越过剩余额度"
    )


# ------------------------------------------------------------------ §2.3.3 边界


def _primed_for_repair(harness: Any) -> RepairState:
    """把现场摆成"第 1 轮补丁已测已诊断、正等待是否再来一轮"。

    跑的是真实节点（分析链 → 编码 → 校验 → 目标测试 → 诊断），不是手捏的 State：
    准入算式要对着真实的 ``selected_target_tests`` 与真实基线读数来判。
    """
    state = harness.initial
    for name in (
        Node.UNDERSTAND_ISSUE.value,
        Node.RETRIEVE.value,
        Node.LOCALIZE.value,
        Node.ANALYZE_IMPACT.value,
        Node.BUILD_ENV.value,
        Node.PLAN.value,
        Node.GENERATE_PATCH.value,
        Node.VALIDATE_PATCH.value,
        Node.RUN_TARGET_TESTS.value,
        Node.DIAGNOSE.value,
    ):
        state = state.model_copy(update=asyncio.run(getattr(nodes, name)(state, harness.deps)))
    assert state.current_attempt is not None and state.current_attempt.diagnosis is not None
    return state


def test_the_boundary_is_the_exact_number_of_runs_the_closure_needs(tmp_path: Path) -> None:
    """边界：``剩余 == 所需`` 放行，``剩余 == 所需 - 1`` 拒绝，两侧都是同一份判据。

    这里刻意用节点级调用而不是整图，因为要钉的是**判据本身**：整图会顺带把结果跑出来，
    而"少一个计数单位"时我们关心的是它*没有开始*。
    """
    harness = build_harness(
        tmp_path,
        results=[assertion_failure(f"{TARGET}::test_normalize", message="assert 1 == 3")],
        coder_rounds=[ROUND_A, ROUND_B],
        diagnoses=[diagnosis("refine")],
    )
    state = _primed_for_repair(harness)
    spent = harness.deps.test_runs
    required = CLOSURE_TARGET_TEST_RUNS + 1 + 1  # 目标测试 + 回归 L2 + 回归 L3
    assert selected_target_tests(state), "目标测试确实是要执行的那种"
    assert state.execution is not None and state.execution.status.value == "FAIL"

    roomy = state.model_copy(
        update={"budget": state.budget.model_copy(update={"max_test_runs": spent + required})}
    )

    allowed = asyncio.run(nodes.repair_patch(roomy, harness.deps))
    assert allowed.get("stop_reason") is not StopReason.BUDGET_CLOSURE_TEST_RUNS
    assert allowed["closure_admission"].feasible is True
    assert allowed["closure_admission"].required == required
    assert allowed["closure_admission"].remaining == required
    assert allowed["closure_admission"].coder_share == 0
    coder_calls_before = harness.llm.purposes().count("coder")
    assert coder_calls_before == 4, "放行那一轮真的派发了编码"

    # 少一个计数单位：拒绝，而且**没有**向模型要第三次编码。
    runs_before = harness.deps.test_runs
    tight = roomy.model_copy(
        update={
            "budget": roomy.budget.model_copy(update={"max_test_runs": spent + required - 1}),
            "closure_admission": None,
        }
    )
    refused = asyncio.run(nodes.repair_patch(tight, harness.deps))
    assert refused["stop_reason"] is StopReason.BUDGET_CLOSURE_TEST_RUNS
    assert refused["closure_admission"].feasible is False
    assert refused["closure_admission"].required == required
    assert refused["closure_admission"].remaining == required - 1
    assert harness.llm.purposes().count("coder") == coder_calls_before
    assert harness.deps.test_runs == runs_before, "拒绝判定不消耗任何容器执行"


# ------------------------------------------------------------------ §2.1.2 编码份额


def test_the_coding_stage_cannot_borrow_the_runs_reserved_for_its_own_closure(
    tmp_path: Path,
) -> None:
    """模型自己跑测试的份额必须从"闭环所需之外"扣——这是 DA-024 的另一半。

    次数额度是同一个计数器：编码多花一次，闭环就少一次。glm14 里第二轮的编码阶段
    自己调了 2 次 ``run_tests``，剩下的就不够 L3 了。这里把份额算成 2，然后要求模型
    连跑 4 次：**只有 2 次真的发生**，其余被工具层拒绝并把原因回给模型，闭环那三次
    仍然在账上。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result() for _ in range(6)],
        coder_rounds=[[ROUND_A[0], RUN_TESTS, RUN_TESTS, RUN_TESTS, RUN_TESTS, FINISH]],
        budget=RepairBudget(max_test_runs=9),
    )
    reviewer = _wired(harness, [APPROVED])
    state = harness.run()

    admission = state.closure_admission
    assert admission is not None
    assert admission.runs_spent == 2, "只有基线两次排在编码之前"
    assert admission.required == 3
    assert admission.remaining == 7
    assert admission.coder_share == 7 - 3 - CLOSURE_FLAKY_RETRY_RESERVE == 2

    refused = [item for item in state.tool_calls if item.get("error_code") == "closure_reserved"]
    assert len(refused) == 2, "四个请求里只有两个被允许，其余原样回话给模型"
    # The closure spends target + L2 only; L3 reuses the whole-baseline L2
    # evidence, so the admission's conservative 3-run reserve is not fully spent.
    assert harness.deps.test_runs == admission.runs_spent + admission.coder_share + 2
    assert state.status is RunStatus.SUCCEEDED, state.failure_reasons
    assert state.regression is not None and state.regression.complete
    assert reviewer.calls, "闭环没有被编码阶段借走的次数拖死"
    assert _report(harness, state)["verification_level"] == "m2_verified"


# ------------------------------------------------------------------ DA-022 局部 vs 全局


def test_a_local_tool_cap_never_overrides_the_global_stop_reason(tmp_path: Path) -> None:
    """§三.3：编码撞到每轮 30 次工具上限之后，真正的全局终止原因仍然各写各的。

    这里选的全局原因是 ``sandbox_failure``：``ENV_ERROR`` 按 02 §13.4 从不进诊断循环，
    在停止规则里也排在预算之前。两个字段必须同时可读、互不覆盖——``coding_stop_reason``
    说"这一轮不能再改代码了"，``stop_reason`` 说"这次任务为什么结束"。首轮真实运行的缺陷
    就是把前者写成了后者，于是整张图绕过验证与审查直奔 finalize。
    """
    capped: list[dict[str, Any]] = [
        edit("src/pkg/core.py", "    return value - 1", "    return value - 2"),
        *[READ] * 29,
    ]
    harness = build_harness(
        tmp_path,
        results=[scripted_result(ExecutionStatus.ENV_ERROR)],
        coder_rounds=[capped],
    )
    reviewer = _wired(harness, [APPROVED])
    state = harness.run()

    assert len(state.tool_calls) == 30
    assert state.coding_stop_reason is StopReason.BUDGET_TOOL_CALLS
    assert state.status is RunStatus.SANDBOX_ERROR
    assert state.stop_reason is StopReason.SANDBOX_FAILURE, state.stop_reason
    assert state.review is None
    assert reviewer.calls == [], "环境坏了也不许拿去审一次换个结论"

    closure = _closure(harness, state)
    assert closure["coding_stop_reason"] == "budget_tool_calls"
    assert closure["review_executed"] is False
    assert closure["target_tests_status"] == "ENV_ERROR"
    assert any(str(item).startswith("review:") for item in closure["unexecuted_stages"])
    assert _report(harness, state)["verification_level"] == "sandbox_error"
    assert _report(harness, state)["verification_level"] not in {
        "m2_verified",
        "sampled_verified",
        "provisionally_verified",
    }


# ------------------------------------------------------------------ §2.2 成功判定


def test_an_unverified_candidate_from_a_refused_round_cannot_claim_m2(tmp_path: Path) -> None:
    """额度不足绝不被读成"测试通过/修复成功"：级别、声称与未执行阶段三处一致。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[ROUND_A],
        budget=RepairBudget(max_test_runs=6),
    )
    _wired(harness, [HIGH])
    state = harness.run()

    report = _report(harness, state)
    assert report["verification_level"] == "budget_exhausted"
    assert state.status is RunStatus.BUDGET_EXHAUSTED
    # 报告里的七类预算读数必须把测试次数写成"已用/上限"，而不是只写上限。
    closure = report["closure"]
    assert closure["budget_usage"]["test_runs"] == "4/6"
    admission = closure["closure_admission"]
    assert admission["feasible"] is False
    assert admission["detail"].startswith("test runs: 4 spent of 6")
    # 事件流里同一笔账也要读得到（事后核对不依赖报告文件是否被留下）。
    rows = _admission_events(harness)
    assert rows and rows[-1]["status"] in {"insufficient", "refused"}


def test_a_plan_is_a_reading_not_a_ledger(tmp_path: Path) -> None:
    """准入读数不进账本：同一个 ``ClosureTestPlan`` 判两次，不会多出任何费用或次数。"""
    plan = ClosureTestPlan(runs_spent=5, allowance=7, target_runs=1, impact_runs=1, full_runs=1)
    assert plan.required == 3
    assert plan.remaining == 2
    assert plan.feasible is False
    assert plan.coder_share == 0
    first = plan.record(trigger="review")
    second = plan.record(trigger="review")
    assert first == second
    assert first.summary()["required"] == "3"
    assert "closure requires 3" in first.detail


# ------------------------------------------------------------------ §2.2 成功判定严格


@pytest.mark.parametrize(
    "stage",
    ["regression_incomplete", "review_unexecuted", "review_of_another_patch"],
)
def test_a_succeeded_status_alone_never_buys_m2(tmp_path: Path, stage: str) -> None:
    """``m2_verified`` 要的是"这一版候选自己挣到的全套证据"，缺一块就降级。

    起点是一次真正走完的高危修复轮（``m2_verified`` 成立），然后逐条抽掉闭环的三块
    地基：回归完成度、审查是否发生、审查绑定的是不是当前补丁。三种情况下级别都必须
    落回 ``provisionally_verified`` ——即使 ``status`` 仍然写着 ``succeeded``。
    """
    harness = build_harness(
        tmp_path,
        results=[passed_result() for _ in range(6)],
        coder_rounds=[ROUND_A, ROUND_B],
        budget=RepairBudget(max_test_runs=8),
    )
    _wired(harness, [HIGH, APPROVED])
    state = harness.run()
    assert verification_level(state) == "m2_verified", state.failure_reasons

    digest = patch_hash(state.current_patch)
    if stage == "regression_incomplete":
        assert state.regression is not None
        state = state.model_copy(
            update={"regression": state.regression.model_copy(update={"complete": False})}
        )
    elif stage == "review_unexecuted":
        state = state.model_copy(update={"review": None, "review_skip_reason": stage})
    else:
        assert state.review is not None
        other = state.model_copy(
            update={"current_patch": f"{state.current_patch}\n# another candidate\n"}
        )
        assert patch_hash(other.current_patch) != digest
        state = other

    level = verification_level(state)
    assert level not in {"m2_verified", "sampled_verified"}
    assert level == "provisionally_verified"
    closure = VerificationClosure.from_state(state)
    assert closure.review_bound_to_candidate is (stage == "regression_incomplete")
    if stage == "regression_incomplete":
        assert "regression_incomplete" in closure.unexecuted_stages
    if stage == "review_unexecuted":
        assert any(item.startswith("review:") for item in closure.unexecuted_stages)
