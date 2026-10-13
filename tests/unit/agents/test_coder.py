from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from codepilot.agents.coder import CodingAgent
from codepilot.llm.fake import FakeLLM
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.plan import RepairPlan
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
