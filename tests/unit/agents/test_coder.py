from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from codepilot.agents.coder import CodingAgent
from codepilot.llm.fake import FakeLLM
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.plan import RepairPlan
from codepilot.schemas.usage import Usage
from codepilot.tools.base import Tool, ToolPermission, ToolResponse
from codepilot.tools.registry import ToolRegistry


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int = 0


def _tool(name: str, *, succeeds: bool = True) -> Tool:
    def handle(raw: BaseModel) -> ToolResponse:
        args = _Input.model_validate(raw)
        if succeeds:
            return ToolResponse.success(name, value=args.value)
        return ToolResponse.failure(name, "scripted_failure", "failed")

    return Tool(
        name=name,
        description=name,
        permission=ToolPermission.READ,
        input_model=_Input,
        handler=handle,
    )


def _plan() -> RepairPlan:
    return RepairPlan(root_cause_hypothesis="x", alternatives=[])


def test_coder_stops_after_exactly_thirty_tool_calls() -> None:
    llm = FakeLLM([{"tool": "read_file", "arguments": {"value": i}} for i in range(30)])
    coder = CodingAgent(llm, ToolRegistry([_tool("read_file")]))
    result = coder.run(issue_text="x", issue_analysis=IssueAnalysis(summary="x"), plan=_plan())
    assert result.stop_reason == "tool_limit"
    assert len(result.tool_calls) == 30
    assert len(llm.calls) == 30


def test_coder_finish_stops_early_without_claiming_validation() -> None:
    llm = FakeLLM([{"tool": "finish", "arguments": {"value": 1}}])
    coder = CodingAgent(llm, ToolRegistry([_tool("finish")]))
    result = coder.run(issue_text="x", issue_analysis=IssueAnalysis(summary="x"), plan=_plan())
    assert result.stop_reason == "finish"
    assert result.finished
    assert len(result.tool_calls) == 1


def test_coder_audits_unknown_invalid_and_failed_tools() -> None:
    llm = FakeLLM(
        [
            {"tool": "not_registered", "arguments": {}},
            {"tool": "read_file", "arguments": {"unknown": 1}},
            {"tool": "search_code", "arguments": {}},
            {},
        ]
    )
    registry = ToolRegistry([_tool("read_file"), _tool("search_code", succeeds=False)])
    result = CodingAgent(llm, registry).run(
        issue_text="x", issue_analysis=IssueAnalysis(summary="x"), plan=_plan()
    )
    assert [item.error_code for item in result.tool_calls] == [
        "unregistered_tool",
        "invalid_arguments",
        "scripted_failure",
    ]
    assert result.stop_reason == "model_completed"


def test_coder_token_budget_stops_before_provider_call() -> None:
    coder = CodingAgent(FakeLLM([]), ToolRegistry(), max_total_tokens=0)
    result = coder.run(issue_text="x", issue_analysis=IssueAnalysis(summary="x"), plan=_plan())
    assert result.stop_reason == "token_budget"
    assert result.llm_calls == 0


def _usage(tokens: int = 0, cost: float = 0.0) -> Usage:
    usage = Usage()
    usage.add_call(alias="coder", input_tokens=tokens, output_tokens=0, cost=cost)
    return usage


def test_local_token_ceiling_is_tighter_than_the_hard_cap() -> None:
    """闭环预留通过 ``token_ceiling`` 传给编码循环：它在每一圈都判定，而节点入口只判一次。

    这里硬上限还剩很多（已用 96 / 上限 1000），但本局天花板已经被预留压到 95 ——
    必须停，而且一次模型都不能再问，否则留给审查的额度就被编码阶段花掉了。
    """
    usage = _usage(tokens=96)
    coder = CodingAgent(FakeLLM([]), ToolRegistry(), usage=usage, max_total_tokens=1_000)
    result = coder.run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=_plan(),
        token_ceiling=95,
    )
    assert result.stop_reason == "token_budget"
    assert result.llm_calls == 0


def test_local_ceiling_can_only_tighten_the_hard_cap() -> None:
    """负控制：局部上限**不得**放宽硬上限（§三.6）。"""
    usage = _usage(tokens=1_000)
    coder = CodingAgent(FakeLLM([]), ToolRegistry(), usage=usage, max_total_tokens=1_000)
    result = coder.run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=_plan(),
        token_ceiling=10_000_000,
    )
    assert result.stop_reason == "token_budget"
    assert result.llm_calls == 0


def test_local_cost_ceiling_stops_with_a_distinct_reason() -> None:
    """费用局部上限单独映射成 ``cost_budget``，才能与 token 上限区分着报告。"""
    usage = _usage(tokens=10, cost=0.95)
    llm = FakeLLM([{}])
    coder = CodingAgent(llm, ToolRegistry(), usage=usage, max_total_tokens=1_000)
    stopped = coder.run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=_plan(),
        cost_ceiling=0.95,
    )
    assert stopped.stop_reason == "cost_budget"
    assert stopped.llm_calls == 0

    # 负控制：没有局部上限时同样的用量不该停下来（硬上限还没到）。
    running = coder.run(issue_text="x", issue_analysis=IssueAnalysis(summary="x"), plan=_plan())
    assert running.stop_reason == "model_completed"


# ---------------------------------------------------------------- 本轮测试份额（DA-024）


def _counting_tool(name: str, seen: list[int]) -> Tool:
    """记录"这个工具真的被执行了几次"——份额管的是执行，不是模型的想法。"""

    def handle(raw: BaseModel) -> ToolResponse:
        args = _Input.model_validate(raw)
        seen.append(args.value)
        return ToolResponse.success(name, value=args.value)

    return Tool(
        name=name,
        description=name,
        permission=ToolPermission.EXECUTE,
        input_model=_Input,
        handler=handle,
    )


def _four_test_requests() -> FakeLLM:
    return FakeLLM(
        [{"tool": "run_tests", "arguments": {"value": index}} for index in range(1, 5)]
        + [{"tool": "finish", "arguments": {"value": 0}}]
    )


def test_the_round_test_run_share_bounds_only_the_executions() -> None:
    """份额之外的 ``run_tests`` 请求被回话拒绝，而不是被静默丢掉或多跑一次容器。

    这条纪律是 DA-024 的另一半：闭环那几次执行不能由编码阶段先借走。
    """
    seen: list[int] = []
    registry = ToolRegistry([_counting_tool("run_tests", seen), _tool("finish")])
    coder = CodingAgent(_four_test_requests(), registry)
    result = coder.run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=_plan(),
        test_run_ceiling=2,
    )
    assert seen == [1, 2], "只有份额内的两次真的进了沙箱"
    assert [item.error_code for item in result.tool_calls] == [
        None,
        None,
        "closure_reserved",
        "closure_reserved",
        None,
    ]
    assert result.stop_reason == "finish", "拒绝一次测试请求不该终止整轮编码"


def test_no_share_means_no_self_initiated_test_run_at_all() -> None:
    """份额为 0 时**一次都不许跑**：闭环刚好够用的一轮里，模型不能自己再验一遍。"""
    seen: list[int] = []
    registry = ToolRegistry([_counting_tool("run_tests", seen), _tool("finish")])
    result = CodingAgent(_four_test_requests(), registry).run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=_plan(),
        test_run_ceiling=0,
    )
    assert seen == []
    assert sum(item.error_code == "closure_reserved" for item in result.tool_calls) == 4


def test_an_absent_share_leaves_the_tool_loop_untouched() -> None:
    """负控制：不传份额时行为与 Phase 11 一致（``None`` 不是 0）。"""
    seen: list[int] = []
    registry = ToolRegistry([_counting_tool("run_tests", seen), _tool("finish")])
    result = CodingAgent(_four_test_requests(), registry).run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=_plan(),
    )
    assert seen == [1, 2, 3, 4]
    assert all(item.error_code is None for item in result.tool_calls)
    assert result.stop_reason == "finish"


def test_a_failed_test_run_does_not_spend_the_share() -> None:
    """失败的调用没花容器次数，因此也不该吃掉份额——否则额度会被"没跑成的尝试"耗光。"""
    seen: list[int] = []

    def handle(raw: BaseModel) -> ToolResponse:
        args = _Input.model_validate(raw)
        seen.append(args.value)
        return ToolResponse.failure("run_tests", "scripted_failure", "failed")

    registry = ToolRegistry(
        [
            Tool(
                name="run_tests",
                description="run_tests",
                permission=ToolPermission.EXECUTE,
                input_model=_Input,
                handler=handle,
            ),
            _tool("finish"),
        ]
    )
    result = CodingAgent(_four_test_requests(), registry).run(
        issue_text="x",
        issue_analysis=IssueAnalysis(summary="x"),
        plan=_plan(),
        test_run_ceiling=1,
    )
    assert seen == [1, 2, 3, 4], "四次尝试都执行了（都失败），一次也没被当成已花额度"
    assert all(item.error_code == "scripted_failure" for item in result.tool_calls[:4])
