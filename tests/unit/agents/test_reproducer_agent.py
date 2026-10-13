"""D-02 + D-04：ReproductionAgent 的重试计数、证据累积与失败降级。

这里盯的是三条容易出错的地方：

* ``attempts`` 必须由循环真实累加，重试计数不能 off-by-one；
* 每一轮的失败证据都要留下来并喂回下一轮，历史不能被覆盖；
* 环境故障不能烧掉 LLM 重试预算，而复现失败不能中断后续修复流程。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codepilot.agents.reproducer import (
    BaseWorkspaceValidator,
    DirectoryBaseProvider,
    ReproductionAgent,
)
from codepilot.artifacts import ArtifactStore
from codepilot.llm.fake import FakeLLM
from codepilot.sandbox.base import EnvironmentBuildResult
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import ExecutionResult, TestCaseResult
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.localization import FileDecision, LocalizationResult, SymbolDecision
from codepilot.schemas.reproduction import (
    MAX_REPRO_ATTEMPTS,
    REPRO_TEST_DIRECTORY,
    ReproductionFailureKind,
    ReproductionStatus,
)
from codepilot.schemas.retrieval import RankedFile, RankedSymbol
from codepilot.tools.base import WorkspacePathPolicy

REPRO = f"{REPRO_TEST_DIRECTORY}/test_repro_boundary.py"
NODE = f"{REPRO}::test_normalize_returns_the_input"
FALLBACK = ("tests/test_core.py::test_normalize", "tests/test_core.py::test_rounding")

GOOD_TEST = """from pkg.core import normalize


def test_normalize_returns_the_input() -> None:
    assert normalize(3) == 3
"""


def _draft(content: str = GOOD_TEST, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "filename": "test_repro_boundary.py",
        "content": content,
        "expected_failure": "AssertionError: normalize(3) 返回 2",
    }
    payload.update(overrides)
    return payload


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src/pkg").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src/pkg/core.py").write_text(
        "def normalize(value: int) -> int:\n    return value - 1\n", encoding="utf-8"
    )
    (root / "tests/test_core.py").write_text(
        "def test_normalize() -> None:\n    assert True\n", encoding="utf-8"
    )
    return root


def _base(tmp_path: Path) -> Path:
    root = tmp_path / "base-copy"
    (root / "src/pkg").mkdir(parents=True)
    (root / "src/pkg/core.py").write_text(
        "def normalize(value: int) -> int:\n    return value - 1\n", encoding="utf-8"
    )
    return root


def _localization() -> LocalizationResult:
    files = [RankedFile(path="src/pkg/core.py", score=1.0)]
    symbols = [
        RankedSymbol(
            symbol_id="sym-1",
            qualified_name="pkg.core.normalize",
            file="src/pkg/core.py",
            score=1.0,
            start_line=1,
            end_line=2,
        )
    ]
    return LocalizationResult(
        files=files,
        symbols=symbols,
        file_decisions=[
            FileDecision(candidate_id="file-1", path="src/pkg/core.py", reason="top file")
        ],
        symbol_decisions=[
            SymbolDecision(
                symbol_id="sym-1",
                file="src/pkg/core.py",
                start_line=1,
                end_line=2,
                file_line_count=2,
                reason="the off-by-one body",
            )
        ],
        retrieval_snapshot_id="retrieval-sha256-0000000000000000000000000000000000000000000000000000000000000000",
    )


def _analysis() -> IssueAnalysis:
    return IssueAnalysis(
        summary="normalize(3) 返回 2，期望返回 3",
        observed_behavior="normalize 少返回 1",
        expected_behavior="normalize(3) == 3",
        mentioned_files=["src/pkg/core.py"],
        mentioned_symbols=["normalize"],
    )


def _execution(
    outcome: TestOutcome,
    *,
    status: ExecutionStatus = ExecutionStatus.FAIL,
    failure_type: str | None = None,
    message: str | None = None,
    node_id: str = NODE,
) -> ExecutionResult:
    return scripted_result(
        status,
        tests=[
            TestCaseResult(
                node_id=node_id,
                outcome=outcome,
                duration=0.1,
                failure_type=failure_type,
                message=message,
            )
        ],
    )


class _Harness:
    def __init__(self, tmp_path: Path, replies: list[Any], results: list[ExecutionResult]) -> None:
        self.workspace = _workspace(tmp_path)
        self.base = _base(tmp_path)
        self.llm = FakeLLM(replies)
        self.sandbox = FakeSandbox(results)
        self.artifacts = ArtifactStore(tmp_path / "artifacts", "phase6-unit")
        self.policy = WorkspacePathPolicy(self.workspace, repro_write=True)
        self.validator = BaseWorkspaceValidator(
            sandbox=self.sandbox,
            env_spec=EnvSpec(install=("python -m pip install pytest",)),
            provider=DirectoryBaseProvider(self.base, repository="repo", base_commit="base"),
        )

    def agent(self, **kwargs: Any) -> ReproductionAgent:
        return ReproductionAgent(
            self.llm,
            policy=self.policy,
            validator=self.validator,
            artifacts=self.artifacts,
            **kwargs,
        )

    def run(self, **kwargs: Any) -> Any:
        defaults: dict[str, Any] = {
            "issue_text": "normalize(3) should return 3 but returns 2",
            "analysis": _analysis(),
            "localization": _localization(),
            "fallback_tests": FALLBACK,
        }
        defaults.update(kwargs)
        return self.agent().reproduce(**defaults)


def test_first_valid_attempt_reproduces_and_writes_only_into_the_repro_directory(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        [_draft()],
        [_execution(TestOutcome.FAILED, failure_type="AssertionError", message="assert 2 == 3")],
    )
    outcome = harness.run()

    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert outcome.result.attempts == 1 and outcome.result.retries == 0
    assert outcome.result.test_path == REPRO
    assert outcome.degraded is False
    assert outcome.target_tests == (NODE,)
    assert (harness.workspace / REPRO).read_text(encoding="utf-8") == GOOD_TEST
    assert not (harness.workspace / "tests/test_other.py").exists()
    assert len(harness.llm.calls) == 1
    assert len(harness.sandbox.run_calls) == 1
    assert outcome.result.kind is ReproductionFailureKind.VALID_ASSERTION


def test_reference_tests_and_localized_source_reach_the_prompt(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path, [_draft()], [_execution(TestOutcome.FAILED, failure_type="AssertionError")]
    )
    harness.run(reference_tests=["tests/test_core.py", "src/pkg/core.py", "tests/nope.py"])

    prompt = harness.llm.prompt_texts()[0]
    assert "已有测试参考 tests/test_core.py" in prompt
    assert "normalize(3) should return 3" in prompt
    assert "pkg.core.normalize" in prompt
    # 非测试文件不会作为"已有测试参考"进入上下文。
    assert "已有测试参考 src/pkg/core.py" not in prompt
    assert "已有测试参考 tests/nope.py" not in prompt


def test_invalid_first_attempt_then_valid_second_attempt(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        [_draft(), _draft()],
        [
            _execution(TestOutcome.PASSED, status=ExecutionStatus.PASS),
            _execution(TestOutcome.FAILED, failure_type="AssertionError", message="assert 2 == 3"),
        ],
    )
    outcome = harness.run()

    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert outcome.result.attempts == 2
    assert [item.kind for item in outcome.result.history] == [
        ReproductionFailureKind.INVALID_PASS,
        ReproductionFailureKind.VALID_ASSERTION,
    ]
    assert [item.index for item in outcome.result.history] == [1, 2]
    # 第二轮的 prompt 必须带上第一轮的**真实**失败证据。
    second = harness.llm.prompt_texts()[1]
    assert "上一轮的真实失败证据" in second
    assert "invalid_pass" in second
    # 每一轮的产物都留在 artifact 里，不被覆盖。
    assert harness.artifacts.exists("reproduction/attempt-01-outcome.json")
    assert harness.artifacts.exists("reproduction/attempt-02-outcome.json")
    assert harness.artifacts.exists("reproduction/attempt-02-candidate.py")


def test_retry_budget_is_exhausted_and_the_flow_degrades_to_existing_tests(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        [_draft()] * MAX_REPRO_ATTEMPTS,
        [_execution(TestOutcome.PASSED, status=ExecutionStatus.PASS)] * MAX_REPRO_ATTEMPTS,
    )
    outcome = harness.run()

    assert outcome.result.status is ReproductionStatus.FAILED
    assert outcome.result.attempts == MAX_REPRO_ATTEMPTS
    assert outcome.result.retries == MAX_REPRO_ATTEMPTS - 1
    assert outcome.degraded is True
    assert outcome.target_tests == FALLBACK
    assert outcome.result.test_path is None
    assert not (harness.workspace / REPRO_TEST_DIRECTORY).exists()
    assert len(harness.llm.calls) == MAX_REPRO_ATTEMPTS
    assert len(harness.sandbox.run_calls) == MAX_REPRO_ATTEMPTS
    assert "retries_exhausted" in outcome.result.fail_reason or "invalid_pass" in (
        outcome.result.fail_reason or ""
    )


def test_max_retries_zero_means_exactly_one_attempt(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path, [_draft()], [_execution(TestOutcome.PASSED, status=ExecutionStatus.PASS)]
    )
    outcome = harness.agent(max_retries=0).reproduce(
        issue_text="normalize(3) should return 3", analysis=_analysis(), fallback_tests=FALLBACK
    )
    assert outcome.result.attempts == 1
    assert len(harness.llm.calls) == 1


@pytest.mark.parametrize("bad", [-1, 4, 99])
def test_retry_budget_cannot_be_widened_beyond_the_contract(tmp_path: Path, bad: int) -> None:
    harness = _Harness(tmp_path, [], [])
    with pytest.raises(ValueError, match="max_retries"):
        harness.agent(max_retries=bad)


def test_pseudo_reproduction_is_rejected_without_wasting_a_sandbox_run(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        [
            _draft(content="def test_normalize_returns_the_input() -> None:\n    assert False\n"),
            _draft(),
        ],
        [_execution(TestOutcome.FAILED, failure_type="AssertionError")],
    )
    outcome = harness.run()

    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert outcome.result.attempts == 2
    assert outcome.result.history[0].kind is ReproductionFailureKind.INVALID_PSEUDO_REPRODUCTION
    assert len(harness.sandbox.run_calls) == 1
    assert not (harness.base / REPRO_TEST_DIRECTORY).exists()


def test_candidate_that_ignores_the_localization_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        [
            _draft(
                content="def test_normalize_returns_the_input() -> None:\n    assert len([1]) == 1\n"
            ),
            _draft(),
        ],
        [_execution(TestOutcome.FAILED, failure_type="AssertionError")],
    )
    outcome = harness.run()

    assert outcome.result.history[0].kind is (
        ReproductionFailureKind.INVALID_UNRELATED_TO_LOCALIZATION
    )
    assert len(harness.sandbox.run_calls) == 1
    assert outcome.result.status is ReproductionStatus.REPRODUCED


def test_invalid_llm_output_counts_an_attempt_but_never_a_false_success(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        ["这不是 JSON", "还 不是 JSON", "依然不是 JSON", _draft()],
        [_execution(TestOutcome.FAILED, failure_type="AssertionError")],
    )
    outcome = harness.run()

    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert outcome.result.attempts == 2
    assert outcome.result.history[0].kind is ReproductionFailureKind.LLM_OUTPUT_ERROR
    # complete_structured 内部有 3 次纠正尝试，所以第一个 generation 用掉 3 次调用。
    assert len(harness.llm.calls) == 4


def test_environment_failure_short_circuits_without_consuming_retry_budget(
    tmp_path: Path,
) -> None:
    sandbox_result = scripted_result(ExecutionStatus.ENV_ERROR, exit_code=None)
    harness = _Harness(tmp_path, [_draft()], [sandbox_result])
    outcome = harness.run()

    assert outcome.result.status is ReproductionStatus.ENV_ERROR
    assert outcome.result.kind is ReproductionFailureKind.ENV_ERROR
    assert outcome.result.attempts == 1
    assert len(harness.llm.calls) == 1
    assert len(harness.sandbox.run_calls) == 1
    assert outcome.degraded is True
    assert outcome.target_tests == FALLBACK


def test_failed_environment_build_is_reported_without_running_any_test(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path, [_draft()], [])
    harness.sandbox._build_status = ExecutionStatus.ENV_ERROR
    outcome = harness.run()
    assert outcome.result.status is ReproductionStatus.ENV_ERROR
    assert outcome.result.kind is ReproductionFailureKind.ENV_ERROR
    assert harness.sandbox.run_calls == []


def test_history_keeps_every_attempt_and_never_rewrites_the_earlier_ones(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path,
        [_draft()] * MAX_REPRO_ATTEMPTS,
        [
            _execution(TestOutcome.PASSED, status=ExecutionStatus.PASS),
            _execution(
                TestOutcome.FAILED, failure_type="ZeroDivisionError", message="division by zero"
            ),
            _execution(TestOutcome.SKIPPED, status=ExecutionStatus.PASS, message="no reason"),
            _execution(
                TestOutcome.FAILED,
                failure_type="ImportError",
                message="ImportError: cannot import name 'missing'",
            ),
        ],
    )
    outcome = harness.run()

    assert outcome.result.status is ReproductionStatus.FAILED
    assert [item.kind for item in outcome.result.history] == [
        ReproductionFailureKind.INVALID_PASS,
        ReproductionFailureKind.INVALID_UNDECLARED_EXCEPTION,
        ReproductionFailureKind.INVALID_SKIPPED,
        ReproductionFailureKind.INVALID_IMPORT_ERROR,
    ]
    assert [item.index for item in outcome.result.history] == [1, 2, 3, 4]
    assert outcome.result.history[1].failure_type == "ZeroDivisionError"
    assert len({item.detail for item in outcome.result.history}) == 4


def test_reproduction_is_deterministic_for_the_same_script(tmp_path: Path) -> None:
    first = _Harness(
        tmp_path / "first",
        [_draft()],
        [_execution(TestOutcome.FAILED, failure_type="AssertionError")],
    ).run()
    second = _Harness(
        tmp_path / "second",
        [_draft()],
        [_execution(TestOutcome.FAILED, failure_type="AssertionError")],
    ).run()
    assert first.result == second.result
    assert first.target_tests == second.target_tests


def test_accepted_reproduction_is_readable_back_through_the_same_policy(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path, [_draft()], [_execution(TestOutcome.FAILED, failure_type="AssertionError")]
    )
    outcome = harness.run()
    assert outcome.result.test_path is not None
    resolved = harness.policy.resolve(outcome.result.test_path, must_exist=True)
    assert "normalize(3) == 3" in resolved.read_text(encoding="utf-8")


def test_agent_without_a_reproduction_write_grant_degrades_instead_of_lying(
    tmp_path: Path,
) -> None:
    harness = _Harness(
        tmp_path, [_draft()], [_execution(TestOutcome.FAILED, failure_type="AssertionError")]
    )
    harness.policy = WorkspacePathPolicy(harness.workspace)  # 没有复现凭证
    outcome = harness.agent().reproduce(
        issue_text="normalize(3) should return 3",
        analysis=_analysis(),
        localization=_localization(),
        fallback_tests=FALLBACK,
    )
    assert outcome.result.status is ReproductionStatus.ENV_ERROR
    assert outcome.result.kind is ReproductionFailureKind.WORKSPACE_ERROR
    assert not (harness.workspace / REPRO_TEST_DIRECTORY).exists()


def test_injected_environment_reuses_a_prebuilt_image(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path, [_draft()], [_execution(TestOutcome.FAILED, failure_type="AssertionError")]
    )
    harness.validator = BaseWorkspaceValidator(
        sandbox=harness.sandbox,
        env_spec=EnvSpec(install=("python -m pip install pytest",)),
        provider=DirectoryBaseProvider(harness.base, repository="repo", base_commit="base"),
        environment=EnvironmentBuildResult(
            status=ExecutionStatus.PASS, image_ref="prebuilt:1", cache_key="prebuilt"
        ),
    )
    outcome = harness.agent().reproduce(
        issue_text="normalize(3) should return 3", analysis=_analysis(), fallback_tests=FALLBACK
    )
    assert outcome.result.status is ReproductionStatus.REPRODUCED
    assert harness.sandbox.build_calls == []


def test_budget_argument_is_capped_to_the_reproduction_context(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, [], [])
    with pytest.raises(ValueError, match="预算"):
        harness.agent(budget=40_000)
