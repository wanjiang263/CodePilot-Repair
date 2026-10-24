from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from harness import FINISH, TARGET, build_harness, edit, passed_result

from codepilot.agents.reviewer import PatchReviewer
from codepilot.errors import FailureClass
from codepilot.graph.regression import RegressionRunner
from codepilot.llm.fake import FakeLLM

APPROVED = {"approved": True, "summary": "looks good"}
WRONG_REFERENCE = {
    "approved": False,
    "summary": "wrong repository",
    "issues": [
        {
            "severity": "high",
            "file": "tests/unit/test_commands.py",
            "line": 1,
            "description": "path copied from another project",
        }
    ],
}


def _harness(tmp_path: Path, replies: list[object]) -> tuple[object, FakeLLM]:
    harness = build_harness(
        tmp_path,
        results=[passed_result(), passed_result(), passed_result()],
        coder_rounds=[
            [edit("src/pkg/core.py", "    return value - 1", "    return value"), FINISH]
        ],
    )
    provider = FakeLLM(replies)
    harness.deps.regression_runner = RegressionRunner()
    harness.deps.reviewer = PatchReviewer(provider)
    return harness, provider


def test_reviewer_receives_actual_diff_and_corrects_wrong_reference(tmp_path: Path) -> None:
    harness, provider = _harness(tmp_path, [WRONG_REFERENCE, APPROVED])
    try:
        state = harness.run()
        assert state.review is not None and state.review.approved
        assert len(provider.calls) == 2
        first_prompt = provider.prompt_texts()[0]
        assert "diff --git a/src/pkg/core.py b/src/pkg/core.py" in first_prompt
        assert "-    return value - 1" in first_prompt
        assert "+    return value" in first_prompt
        assert provider.calls[1].attempt == 2
        assert provider.calls[1].correction is True
        assert "tests/unit/test_commands.py" in provider.prompt_texts()[1]
        assert "tests/_codepilot_repro" not in state.current_patch
        assert TARGET in first_prompt
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("replies", "expected_class"),
    [
        (["not json", "still not json"], FailureClass.MODEL_OUTPUT_INVALID),
        (
            [WRONG_REFERENCE, WRONG_REFERENCE],
            FailureClass.MODEL_OUTPUT_INVALID,
        ),
    ],
    ids=["schema", "semantic"],
)
def test_reviewer_schema_and_semantic_errors_are_bounded_and_attributed(
    tmp_path: Path, replies: list[object], expected_class: FailureClass
) -> None:
    harness, provider = _harness(tmp_path, replies)
    try:
        state = harness.run()
        assert len(provider.calls) == 2
        assert provider.calls[1].correction is True
        assert state.review is None
        assert state.error_class == expected_class.value
        assert state.error_origin == "model"
    finally:
        harness.close()


def test_reviewer_provider_error_is_not_reclassified_as_model_output(tmp_path: Path) -> None:
    harness, provider = _harness(tmp_path, [httpx.ConnectError("provider unavailable")])
    try:
        state = harness.run()
        assert len(provider.calls) == 1
        assert state.review is None
        assert state.error_class == FailureClass.LLM_PROVIDER_ERROR.value
        assert state.error_origin == "provider"
    finally:
        harness.close()
