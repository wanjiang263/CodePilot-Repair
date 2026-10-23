"""真实 GLM 失败场景的离线复现 + M2 闭环回归（Harness Hardening 集成验收 1 / 2）。

第一组用例把首次真实实验的那条计划（脱敏后）喂给真的 Planner 与真的状态机，
验证它不再以 ``workflow_error`` 收场；第二组验证合法计划在新语义校验下**没有回归**
——这条很重要：修复缺陷时把 M2 闭环跑挂了，等于用一个新错误替换了旧错误。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from harness import FINISH, TARGET, build_harness, edit, passed_result

from codepilot.graph.routing import Node
from codepilot.schemas.enums import RunStatus, StopReason

#: 首次真实 GLM 输出的脱敏形态：形状合法、语义上是"打算写一个还不存在的测试"。
PLANNED_TARGET = f"planned:{TARGET}::test_new"


def _plan(tests: list[str], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "root_cause_hypothesis": "normalize subtracts one unexpectedly",
        "evidence": ["src/pkg/core.py returns value - 1"],
        "changes": [
            {
                "file": "src/pkg/core.py",
                "symbol": "pkg.core.normalize",
                "action": "modify",
                "description": "return the input unchanged",
            }
        ],
        "tests_to_run": tests,
        "risk": "low",
        "alternatives": ["guard the subtraction instead of removing it"],
    }
    payload.update(overrides)
    return payload


def _coder_round() -> list[dict[str, Any]]:
    return [[edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH]]


# --------------------------------------------------------------- 失败场景复现


def test_planned_marker_is_corrected_and_the_run_completes(tmp_path: Path) -> None:
    """纠正环真的在工作：第一次给 ``planned:``，第二次给出既有测试，然后一路跑完。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(TARGET)],
        coder_rounds=_coder_round(),
        planner_replies=[
            {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}},
            _plan([PLANNED_TARGET]),
            _plan([TARGET]),
        ],
    )
    state = harness.run()

    assert state.plan is not None, state.model_dump_json(indent=2)
    assert state.plan.tests_to_run == [TARGET]
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.failure_reasons
    assert state.error_class is None
    assert state.plan_corrections == 1
    assert state.plan_semantic_rejections == 1
    assert state.plan_schema_rejections == 0
    assert state.plan_tools is not None
    assert state.plan_tools.requested >= 1
    assert state.plan_tools.records_present is True

    events = _event_lines(harness)
    attempts = [item for item in events if item.get("status", "").startswith("structured_attempt:")]
    assert [item["status"] for item in attempts] == [
        "structured_attempt:1",
        "structured_attempt:2",
    ]


def test_uncorrectable_plan_ends_as_plan_rejected_not_workflow_error(
    tmp_path: Path,
) -> None:
    """纠正预算耗尽：明确的 ``plan_rejected`` + 模型侧归因，绝不伪装成基础设施崩溃。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(TARGET)],
        coder_rounds=_coder_round(),
        planner_replies=[
            {"tool": "read_file", "arguments": {"path": "src/pkg/core.py"}},
            *[_plan([PLANNED_TARGET]) for _ in range(6)],
        ],
    )
    state = harness.run()

    assert state.status is RunStatus.PLAN_REJECTED, state.model_dump_json(indent=2)
    assert state.stop_reason is StopReason.PLAN_REJECTED
    assert state.error_class == "plan_semantic_invalid"
    assert state.error_origin == "model"
    assert state.plan is None
    assert state.current_patch == "", "计划被拒不该继续生成补丁"
    assert not any("PathAccessError" in item for item in state.failure_reasons)
    assert "workflow_error" not in {str(state.stop_reason)}

    # 被拒的计划必须留下证据——首轮实验什么都没存，事后只能猜模型写了什么。
    run_dir = harness.artifacts_root / "runs" / state.run_id
    rejections = run_dir / "plan_semantic_rejections.jsonl"
    lines = [json.loads(item) for item in rejections.read_text(encoding="utf-8").splitlines()]
    assert lines
    assert "intent_marker_in_tests_to_run" in lines[0]["codes"]
    assert lines[0]["rejected_plan"]["tests_to_run"] == [PLANNED_TARGET]
    # 工具审计在被拒路径上同样落盘：失败运行更需要"模型查过什么"的证据。
    assert (run_dir / "plan_tools.json").is_file()

    events = _event_lines(harness)
    error_events = [item for item in events if item.get("extra", {}).get("error_class")]
    assert error_events, "归因要写进事件流，而不是只在 state 里"
    assert error_events[0]["extra"]["error_class"] == "plan_semantic_invalid"
    assert error_events[0]["extra"]["error_origin"] == "model"


def test_tool_permission_denial_is_not_a_harness_crash(tmp_path: Path) -> None:
    """越界读取被拒是工具契约的一部分，图要继续走下去。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(TARGET)],
        coder_rounds=_coder_round(),
        planner_replies=[
            {"tool": "read_file", "arguments": {"path": "../../etc/passwd"}},
            _plan([TARGET]),
        ],
    )
    state = harness.run()
    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.failure_reasons
    assert state.plan_tools is not None
    assert state.plan_tools.requested_but_denied == 1
    assert state.plan_tools.records[0].error_code == "path_escape"


# ------------------------------------------------------------------ M2 闭环


def test_legal_plan_still_walks_the_whole_m2_loop(tmp_path: Path) -> None:
    """新语义校验不能让合法计划回归：Issue → Plan → Code → Validate → Tests → Report。"""
    harness = build_harness(
        tmp_path,
        results=[passed_result(TARGET)],
        coder_rounds=_coder_round(),
    )
    state = harness.run()

    assert state.status is RunStatus.PROVISIONALLY_VERIFIED, state.model_dump_json(indent=2)
    assert state.plan is not None and state.plan.tests_to_run == [TARGET]
    assert len(state.attempts) == 1
    assert state.attempts[0].execution is not None
    assert state.attempts[0].validation.passed is True

    nodes = [event["node"] for event in state.events]
    for expected in (
        Node.UNDERSTAND_ISSUE.value,
        Node.RETRIEVE.value,
        Node.LOCALIZE.value,
        Node.ANALYZE_IMPACT.value,
        Node.BUILD_ENV.value,
        Node.PLAN.value,
        Node.GENERATE_PATCH.value,
        Node.VALIDATE_PATCH.value,
        Node.RUN_TARGET_TESTS.value,
        Node.FINALIZE.value,
    ):
        assert expected in nodes, (expected, nodes)

    report_path = harness.artifacts_root / "runs" / state.run_id / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["plan_tool_audit"]["records_present"] is True
    assert report["plan_tool_audit"]["requested"] >= 1
    assert report["error_class"] is None
    kinds = {item["type"] for item in report["evidence"]}
    assert "planner_tool_audit" in kinds


def _event_lines(harness: Any) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in harness.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
