"""D-09：Self-verified Precision 纯函数。

每个期望值都能手算核对——这是它作为评测指标的全部价值：读报告的人不需要相信
实现，只要相信除法。
"""

from __future__ import annotations

import ast

import pytest

from codepilot.eval.metrics import (
    SelfVerifiedSample,
    agent_claims_success,
    self_verified_precision,
)
from codepilot.schemas.enums import RunStatus


def _sample(task: str, claims: bool, verdict: bool | None) -> SelfVerifiedSample:
    return SelfVerifiedSample(task_id=task, agent_claims_success=claims, evaluation_passed=verdict)


def test_no_self_claimed_success_is_not_reported_as_one_hundred_percent() -> None:
    result = self_verified_precision([_sample("a", False, True), _sample("b", False, False)])
    assert result.value is None
    assert result.denominator == 0 and result.numerator == 0
    assert result.defined is False
    assert "n/a" in result.describe()
    # 评测通过但 Agent 未宣称成功的两个任务里有一个被单独计数，不进分子。
    assert result.evaluated_without_claim == 1


def test_empty_input_is_well_defined() -> None:
    result = self_verified_precision([])
    assert (result.numerator, result.denominator, result.value) == (0, 0, None)
    assert result.total_tasks == 0


def test_all_claims_confirmed_is_a_perfect_precision() -> None:
    result = self_verified_precision([_sample(f"t{i}", True, True) for i in range(3)])
    assert (result.numerator, result.denominator) == (3, 3)
    assert result.value == 1.0


def test_overclaiming_lowers_precision() -> None:
    """手算：4 个自认成功里只有 1 个被评测确认 → 1/4 = 0.25。"""
    samples = [_sample("a", True, True)] + [_sample(name, True, False) for name in "bcd"]
    result = self_verified_precision(samples)
    assert (result.numerator, result.denominator, result.value) == (1, 4, 0.25)


def test_evaluation_success_without_a_claim_does_not_inflate_the_numerator() -> None:
    """手算：分子 1 / 分母 2 = 0.5，而不是 2/2。"""
    result = self_verified_precision(
        [
            _sample("claimed-and-confirmed", True, True),
            _sample("claimed-but-wrong", True, False),
            _sample("quiet-but-confirmed", False, True),
        ]
    )
    assert (result.numerator, result.denominator, result.value) == (1, 2, 0.5)
    assert result.evaluated_without_claim == 1
    assert result.total_tasks == 3


def test_environment_failures_are_excluded_but_reported() -> None:
    """手算：3 个可判定的自认成功里 2 个确认 → 2/3；1 个无结论的任务另行计数。"""
    result = self_verified_precision(
        [
            _sample("a", True, True),
            _sample("b", True, True),
            _sample("c", True, False),
            _sample("d", True, None),
            _sample("e", False, None),
        ]
    )
    assert (result.numerator, result.denominator) == (2, 3)
    assert result.value == pytest.approx(2 / 3)
    assert result.claimed_without_verdict == 1
    assert result.total_tasks == 5


def test_ten_task_worksheet_matches_hand_computation() -> None:
    """一个可手算的完整样本集，报告口径与实现必须逐项一致。"""
    verdicts: list[tuple[bool, bool | None]] = [
        (True, True),  # 1 命中
        (True, False),  # 2 误报
        (True, True),  # 3 命中
        (False, True),  # 4 评测通过但未宣称
        (True, None),  # 5 无结论
        (False, False),
        (True, False),  # 7 误报
        (False, True),  # 8 未宣称
        (True, True),  # 9 命中
        (False, None),
    ]
    result = self_verified_precision(
        [
            _sample(f"task-{index}", claims, verdict)
            for index, (claims, verdict) in enumerate(verdicts, 1)
        ]
    )
    # 自认成功：1,2,3,5,7,9 共 6 个；其中 5 号无结论被排除 → 分母 5，分子 3。
    assert (result.numerator, result.denominator) == (3, 5)
    assert result.value == 0.6
    assert result.claimed_without_verdict == 1
    assert result.evaluated_without_claim == 2
    assert result.total_tasks == 10
    assert "3/5" in result.describe()


def test_duplicate_task_ids_are_rejected_rather_than_double_counted() -> None:
    with pytest.raises(ValueError, match="重复的 task_id"):
        self_verified_precision([_sample("a", True, True), _sample("a", True, False)])


def test_blank_task_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="task_id"):
        SelfVerifiedSample(task_id=" ", agent_claims_success=True, evaluation_passed=True)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.SUCCEEDED, True),
        (RunStatus.PROVISIONALLY_VERIFIED, True),
        (RunStatus.RUNNING, False),
        (RunStatus.FAILED, False),
        (RunStatus.STOPPED, False),
        (RunStatus.ERROR, False),
        (RunStatus.PATCH_REJECTED, False),
        (RunStatus.TESTS_FAILED, False),
        (RunStatus.SANDBOX_ERROR, False),
        (RunStatus.TOOL_ERROR, False),
        (RunStatus.BUDGET_EXHAUSTED, False),
        ("provisionally_verified", True),
        ("failed", False),
    ],
)
def test_agent_self_claim_translation_covers_every_terminal_status(
    status: RunStatus | str, expected: bool
) -> None:
    assert agent_claims_success(status) is expected


def test_agent_claims_success_rejects_an_unknown_status() -> None:
    with pytest.raises(ValueError):
        agent_claims_success("fixed_promise")  # type: ignore[arg-type]


def test_metric_is_a_pure_function_without_runtime_dependencies() -> None:
    """03 §19：指标必须可以在没有 LLM、Docker 与文件系统的进程里算出来。"""
    import inspect

    import codepilot.eval.metrics as metrics_module

    imported: set[str] = set()
    for node in ast.parse(inspect.getsource(metrics_module)).body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".", 1)[0])
    assert imported <= {"collections", "dataclasses", "codepilot", "__future__"}

    samples = [_sample("a", True, True)]
    assert self_verified_precision(samples) == self_verified_precision(samples)
