"""C-01：IssueAnalysis 作为跨模块数据契约的校验与序列化行为。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

import codepilot.agents.schemas as agent_schemas
from codepilot.agents.schemas import IssueSemanticDraft, RuleEvidence, TraceFrame
from codepilot.schemas.enums import IssueType
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.retrieval import RetrievalQuery


def frame(**overrides: Any) -> TraceFrame:
    data: dict[str, Any] = {
        "file": "calc/main.py",
        "line": 12,
        "function": "divide",
        "in_repo": True,
    }
    data.update(overrides)
    return TraceFrame(**data)


def full_analysis(**overrides: Any) -> IssueAnalysis:
    data: dict[str, Any] = {
        "issue_type": IssueType.REGRESSION,
        "summary": "除法在 0 除数时抛 ZeroDivisionError",
        "expected_behavior": "返回错误提示",
        "observed_behavior": "进程崩溃",
        "reproduction_steps": ["调用 divide(1, 0)"],
        "mentioned_files": ["calc/main.py"],
        "mentioned_symbols": ["divide", "calc.main.divide"],
        "error_messages": ["ZeroDivisionError: division by zero"],
        "traceback_frames": [frame()],
        "keywords": ["divide", "zero"],
    }
    data.update(overrides)
    return IssueAnalysis(**data)


def test_agents_module_reuses_the_l0_contract_instead_of_copying_it():
    """02 §10.1 只有一份 IssueAnalysis；agents 层不得再建平行模型。"""
    from codepilot.schemas import issue as l0_issue

    assert agent_schemas.IssueAnalysis is l0_issue.IssueAnalysis is IssueAnalysis


@pytest.mark.parametrize("issue_type", list(IssueType), ids=[m.value for m in IssueType])
def test_every_issue_type_round_trips(issue_type: IssueType) -> IssueType | None:
    analysis = full_analysis(issue_type=issue_type)
    restored = IssueAnalysis.model_validate(json.loads(analysis.model_dump_json()))
    assert restored.issue_type is issue_type
    assert restored == analysis


def test_json_round_trip_keeps_nested_frames_and_optionals():
    restored = IssueAnalysis.model_validate_json(full_analysis().model_dump_json())
    assert restored == full_analysis()
    assert restored.traceback_frames[0].in_repo is True
    assert restored.expected_behavior == "返回错误提示"


def test_incomplete_issue_is_representable_without_fabricated_values():
    """无 traceback、无文件名、只有一句自然语言时，各集合就是空的。"""
    analysis = IssueAnalysis(summary="按钮点不动")
    assert analysis.traceback_frames == []
    assert analysis.mentioned_files == []
    assert analysis.mentioned_symbols == []
    assert analysis.error_messages == []
    assert analysis.reproduction_steps == []
    assert analysis.keywords == []
    assert analysis.expected_behavior is None
    assert analysis.observed_behavior is None


def test_collections_default_to_independent_instances():
    first = IssueAnalysis()
    second = IssueAnalysis()
    first.keywords.append("shared")
    assert second.keywords == []
    assert IssueAnalysis().mentioned_files == []


def test_empty_lists_survive_serialization_as_lists_not_null():
    payload = json.loads(
        full_analysis(reproduction_steps=[], traceback_frames=[]).model_dump_json()
    )
    assert payload["reproduction_steps"] == []
    assert payload["traceback_frames"] == []
    assert IssueAnalysis.model_validate(payload).traceback_frames == []


@pytest.mark.parametrize(
    "payload",
    [
        {"issue_type": "typo"},
        {"summary": 5},
        {"mentioned_files": "calc/main.py"},
        {"traceback_frames": [{"file": "a.py", "line": 1, "function": "f"}]},
        {"traceback_frames": [{"file": "a.py", "line": "x", "function": "f", "in_repo": True}]},
        {"traceback_frames": [1]},
        {"reproduction_steps": [None]},
    ],
    ids=[
        "unknown type",
        "int summary",
        "scalar files",
        "frame missing in_repo",
        "frame line not int",
        "frame not object",
        "null step",
    ],
)
def test_illegal_fields_are_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IssueAnalysis.model_validate(payload)


def test_traceback_frame_is_immutable_so_evidence_cannot_be_edited_downstream():
    with pytest.raises(ValidationError):
        frame().line = 99


def test_draft_expresses_absence_rather_than_a_plausible_default():
    """模型没意见时必须是 None/空串，不能靠默认值冒充结论。"""
    draft = IssueSemanticDraft()
    assert draft.issue_type is None
    assert draft.summary == ""
    assert draft.expected_behavior is None
    assert draft.observed_behavior is None
    assert draft.keywords == []


def test_draft_cannot_carry_verifiable_evidence():
    """traceback / 文件类字段在类型层面就不存在，多余键被丢弃而非覆盖。"""
    schema_fields = set(IssueSemanticDraft.model_fields)
    assert not schema_fields & {"traceback_frames", "mentioned_files", "mentioned_symbols"}
    draft = IssueSemanticDraft.model_validate(
        {
            "summary": "s",
            "traceback_frames": [{"file": "guessed.py", "line": 1}],
            "mentioned_files": ["guessed.py"],
            "root_cause": "就是它",
        }
    )
    assert draft.summary == "s"
    assert "traceback_frames" not in draft.model_dump()
    assert "root_cause" not in draft.model_dump()


def test_rule_evidence_requires_the_parsed_query_it_was_derived_from():
    """中间结构必须能追到 V-01 的原始查询，否则证据无从核对。"""
    query = RetrievalQuery(raw_issue="boom", parsed_frames=[frame()])
    with pytest.raises(ValidationError):
        RuleEvidence(mentioned_files=["calc/main.py"])
    evidence = RuleEvidence(mentioned_files=["calc/main.py"], query=query)
    assert evidence.query is not None
    assert evidence.issue_type is None
    assert evidence.model_dump()["traceback_frames"] == []


def test_rule_evidence_round_trips_with_frames_and_query():
    query = RetrievalQuery(raw_issue="boom", file_paths=["calc/main.py"], parsed_frames=[frame()])
    evidence = RuleEvidence(
        mentioned_files=["calc/main.py"],
        traceback_frames=[frame()],
        reproduction_steps=["1", "2"],
        issue_type=IssueType.BUG,
        query=query,
    )
    restored = RuleEvidence.model_validate(evidence.model_dump())
    assert restored == evidence
    assert restored.query.parsed_frames[0].function == "divide"
