"""D-03：在 **base commit** 上校验复现有效性。

这一组测试盯的是两件事：

* 有效/无效的边界不能靠 stdout 猜——失败类型必须来自 JUnit 的结构化字段；
* 环境故障（装不上依赖、容器起不来）绝不能被算成"复现失败"，否则重试预算会被
  一个和代码无关的问题烧光。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codepilot.agents.reproducer import (
    BaseReproductionCheck,
    BaseWorkspaceValidator,
    DirectoryBaseProvider,
    WorkspaceManagerBaseProvider,
    classify_base_execution,
    write_reproduction_test,
)
from codepilot.repo.gitcmd import run_git
from codepilot.repo.workspace import WorkspaceManager
from codepilot.sandbox.env_spec import EnvSpec
from codepilot.sandbox.fake import FakeSandbox, scripted_result
from codepilot.schemas.enums import ExecutionStatus, TestOutcome
from codepilot.schemas.execution import TestCaseResult
from codepilot.schemas.reproduction import REPRO_TEST_DIRECTORY, ReproductionFailureKind
from codepilot.tools.base import WorkspacePathPolicy
from support.gitfixture import init_repo

REPRO = f"{REPRO_TEST_DIRECTORY}/test_repro_boundary.py"
NODE = f"{REPRO}::test_boundary_second_is_still_valid"
DECLARED = frozenset({"ValueError", "KeyError"})


def _case(
    outcome: TestOutcome,
    *,
    failure_type: str | None = None,
    message: str | None = None,
    node_id: str = NODE,
) -> TestCaseResult:
    return TestCaseResult(
        node_id=node_id,
        outcome=outcome,
        duration=0.1,
        failure_type=failure_type,
        message=message,
        traceback=None if failure_type is None else f"src/pkg/core.py:7: {failure_type}",
    )


def _classify(
    status: ExecutionStatus,
    cases: list[TestCaseResult],
    *,
    declared: frozenset[str] = DECLARED,
    exit_code: int | None = None,
) -> BaseReproductionCheck:
    execution = scripted_result(status, tests=cases, exit_code=exit_code)
    return classify_base_execution(
        execution=execution,
        test_path=REPRO,
        node_ids=(NODE,),
        declared_exception_types=declared,
    )


def test_assertion_failure_on_base_is_a_valid_reproduction() -> None:
    check = _classify(
        ExecutionStatus.FAIL,
        [_case(TestOutcome.FAILED, failure_type="AssertionError", message="assert 1 == 2")],
        exit_code=1,
    )
    assert check.valid is True
    assert check.kind is ReproductionFailureKind.VALID_ASSERTION
    assert check.node_id == NODE
    assert check.failure_type == "AssertionError"
    assert check.test_path == REPRO
    assert "assert 1 == 2" in check.detail


def test_exception_type_named_in_the_issue_is_a_valid_reproduction() -> None:
    check = _classify(
        ExecutionStatus.FAIL,
        [_case(TestOutcome.FAILED, failure_type="KeyError", message="KeyError: 'ghost'")],
        exit_code=1,
    )
    assert check.valid is True
    assert check.kind is ReproductionFailureKind.VALID_ISSUE_EXCEPTION


def test_exception_type_not_named_in_the_issue_is_not_valid() -> None:
    check = _classify(
        ExecutionStatus.FAIL,
        [_case(TestOutcome.FAILED, failure_type="ZeroDivisionError", message="division by zero")],
        exit_code=1,
    )
    assert check.valid is False
    assert check.kind is ReproductionFailureKind.INVALID_UNDECLARED_EXCEPTION


def test_pytest_raises_that_did_not_raise_is_tied_to_the_issue_exception() -> None:
    raised = _classify(
        ExecutionStatus.FAIL,
        [
            _case(
                TestOutcome.FAILED,
                failure_type="Failed",
                message="Failed: DID NOT RAISE <class 'ValueError'>",
            )
        ],
        exit_code=1,
    )
    assert raised.kind is ReproductionFailureKind.VALID_ISSUE_EXCEPTION
    assert raised.valid is True

    unrelated = _classify(
        ExecutionStatus.FAIL,
        [
            _case(
                TestOutcome.FAILED,
                failure_type="Failed",
                message="Failed: DID NOT RAISE <class 'OSError'>",
            )
        ],
        exit_code=1,
    )
    # 仍然是一次断言族失败（测试声明的期望行为没发生），但不是 Issue 点名的异常。
    assert unrelated.kind is ReproductionFailureKind.VALID_ASSERTION


def test_passing_on_base_means_the_issue_was_not_reproduced() -> None:
    check = _classify(ExecutionStatus.PASS, [_case(TestOutcome.PASSED)])
    assert check.valid is False
    assert check.kind is ReproductionFailureKind.INVALID_PASS


def test_skipped_test_is_never_counted_as_a_reproduction() -> None:
    check = _classify(
        ExecutionStatus.PASS,
        [_case(TestOutcome.SKIPPED, message="no")],
    )
    assert check.valid is False
    assert check.kind is ReproductionFailureKind.INVALID_SKIPPED


def test_import_error_from_the_candidate_itself_is_invalid() -> None:
    check = _classify(
        ExecutionStatus.FAIL,
        [
            _case(
                TestOutcome.FAILED,
                failure_type="ModuleNotFoundError",
                message="ModuleNotFoundError: No module named 'miniauth'",
            )
        ],
        exit_code=1,
    )
    assert check.kind is ReproductionFailureKind.INVALID_IMPORT_ERROR
    assert check.valid is False


def test_syntax_error_from_the_candidate_itself_is_invalid() -> None:
    check = _classify(
        ExecutionStatus.COLLECTION_ERROR,
        [
            TestCaseResult(
                node_id=REPRO,
                outcome=TestOutcome.ERROR,
                duration=0.0,
                message="SyntaxError: invalid syntax",
                traceback="tests/_codepilot_repro/test_repro.py:3: SyntaxError",
                failure_type="Collecting",
            )
        ],
        exit_code=2,
    )
    assert check.kind is ReproductionFailureKind.INVALID_SYNTAX_ERROR
    assert check.valid is False


def test_collection_error_without_a_candidate_signature_is_still_not_a_reproduction() -> None:
    check = _classify(
        ExecutionStatus.COLLECTION_ERROR,
        [
            TestCaseResult(
                node_id="tests/test_other.py",
                outcome=TestOutcome.ERROR,
                duration=0.0,
                message="fixture 'backend' not found",
            )
        ],
        exit_code=2,
    )
    assert check.kind is ReproductionFailureKind.COLLECTION_ERROR
    assert check.valid is False
    assert check.test_path is None


def test_requested_node_id_that_never_ran_is_not_evidence() -> None:
    check = _classify(
        ExecutionStatus.PASS,
        [
            TestCaseResult(
                node_id="tests/test_other.py::test_x", outcome=TestOutcome.PASSED, duration=0.0
            )
        ],
        exit_code=0,
    )
    assert check.kind is ReproductionFailureKind.INVALID_NOT_EXECUTED
    assert "没有执行记录" in check.detail


def test_setup_error_is_a_fixture_problem_not_a_reproduction() -> None:
    check = _classify(
        ExecutionStatus.FAIL,
        [_case(TestOutcome.ERROR, failure_type="ValueError", message="fixture blew up")],
        exit_code=1,
    )
    assert check.kind is ReproductionFailureKind.INVALID_FIXTURE_ERROR


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (ExecutionStatus.ENV_ERROR, ReproductionFailureKind.ENV_ERROR),
        (ExecutionStatus.SANDBOX_ERROR, ReproductionFailureKind.SANDBOX_ERROR),
    ],
)
def test_environment_failures_are_kept_apart_from_reproduction_failures(
    status: ExecutionStatus, kind: ReproductionFailureKind
) -> None:
    check = _classify(status, [], exit_code=None)
    assert check.kind is kind
    assert check.environment_failure is True
    assert check.valid is False


def test_timeout_without_issue_timeout_evidence_is_invalid() -> None:
    check = _classify(ExecutionStatus.TIMEOUT, [], exit_code=None)
    assert check.kind is ReproductionFailureKind.TIMEOUT
    assert check.environment_failure is False


def test_classification_never_uses_stdout_text_for_the_failure_type() -> None:
    """JUnit 结构字段缺失时宁可判无效，也不去 stdout 里找关键词。"""
    execution = scripted_result(
        ExecutionStatus.FAIL,
        tests=[
            TestCaseResult(
                node_id=NODE,
                outcome=TestOutcome.FAILED,
                duration=0.0,
                message="boom",
                traceback=None,
                failure_type=None,
            )
        ],
        exit_code=1,
    )
    execution = execution.model_copy(update={"stdout_tail": "AssertionError: 看起来像断言失败"})
    check = classify_base_execution(
        execution=execution, test_path=REPRO, node_ids=(NODE,), declared_exception_types=DECLARED
    )
    assert check.kind is ReproductionFailureKind.INVALID_UNDECLARED_EXCEPTION


# --------------------------------------------------------------------- 执行侧


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    (root / "src/pkg").mkdir(parents=True)
    (root / "src/pkg/core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return root


def _validator(sandbox: FakeSandbox, provider: DirectoryBaseProvider) -> BaseWorkspaceValidator:
    return BaseWorkspaceValidator(
        sandbox=sandbox,
        env_spec=EnvSpec(install=("python -m pip install pytest",)),
        provider=provider,
    )


def test_validator_runs_the_exact_reproduction_node_ids_in_the_base_workspace(
    base_dir: Path,
) -> None:
    sandbox = FakeSandbox(
        [
            scripted_result(
                ExecutionStatus.FAIL,
                tests=[
                    _case(
                        TestOutcome.FAILED, failure_type="AssertionError", message="assert 1 == 2"
                    )
                ],
                exit_code=1,
            )
        ]
    )
    validator = _validator(
        sandbox, DirectoryBaseProvider(base_dir, repository="repo", base_commit="snapshot")
    )

    check = validator.check(
        filename="test_repro_boundary.py",
        content="def test_boundary_second_is_still_valid():\n    assert 1 == 2\n",
        attempt=1,
        test_names=("test_boundary_second_is_still_valid",),
        declared_exception_types=DECLARED,
    )

    assert check.valid is True
    assert sandbox.build_calls == [("repo", "snapshot", str(base_dir))]
    workspace, tests, iteration = sandbox.run_calls[0]
    assert Path(workspace) == base_dir
    assert tests == (NODE,)
    assert iteration == 1
    # 校验结束后 base 目录里不能留下复现文件。
    assert not (base_dir / REPRO_TEST_DIRECTORY).exists()


def test_environment_build_failure_short_circuits_before_any_test_run(base_dir: Path) -> None:
    sandbox = FakeSandbox([], build_status=ExecutionStatus.ENV_ERROR)
    validator = _validator(
        sandbox, DirectoryBaseProvider(base_dir, repository="r", base_commit="c")
    )
    check = validator.check(
        filename="test_repro_boundary.py",
        content="def test_boundary_second_is_still_valid():\n    assert True\n",
        attempt=1,
        test_names=("test_boundary_second_is_still_valid",),
    )
    assert check.kind is ReproductionFailureKind.ENV_ERROR
    assert sandbox.run_calls == []


def test_sandbox_exception_is_reported_as_a_sandbox_error(base_dir: Path) -> None:
    class Exploding(FakeSandbox):
        def run_tests(self, **kwargs: object) -> object:
            raise RuntimeError("container vanished")

    sandbox = Exploding([scripted_result(ExecutionStatus.PASS)])
    validator = _validator(
        sandbox, DirectoryBaseProvider(base_dir, repository="r", base_commit="c")
    )
    check = validator.check(
        filename="test_repro_boundary.py",
        content="def test_boundary_second_is_still_valid():\n    assert True\n",
        attempt=1,
        test_names=("test_boundary_second_is_still_valid",),
    )
    assert check.kind is ReproductionFailureKind.SANDBOX_ERROR
    assert check.environment_failure is True


def test_missing_base_directory_is_a_workspace_error(tmp_path: Path) -> None:
    sandbox = FakeSandbox([scripted_result(ExecutionStatus.PASS)])
    validator = _validator(
        sandbox,
        DirectoryBaseProvider(tmp_path / "nope", repository="r", base_commit="c"),
    )
    check = validator.check(
        filename="test_repro_boundary.py",
        content="def test_boundary_second_is_still_valid():\n    assert True\n",
        attempt=1,
        test_names=("test_boundary_second_is_still_valid",),
    )
    assert check.kind is ReproductionFailureKind.WORKSPACE_ERROR


def test_environment_image_is_built_once_per_base_identity(base_dir: Path) -> None:
    sandbox = FakeSandbox(
        [
            scripted_result(ExecutionStatus.PASS, exit_code=0),
            scripted_result(ExecutionStatus.PASS, exit_code=0),
        ]
    )
    validator = _validator(
        sandbox, DirectoryBaseProvider(base_dir, repository="r", base_commit="c")
    )
    for attempt in (1, 2):
        validator.check(
            filename="test_repro_boundary.py",
            content="def test_boundary_second_is_still_valid():\n    assert True\n",
            attempt=attempt,
            test_names=("test_boundary_second_is_still_valid",),
        )
    assert len(sandbox.build_calls) == 1
    assert len(sandbox.run_calls) == 2


def test_write_reproduction_test_is_limited_to_the_canonical_directory(
    base_dir: Path,
) -> None:
    granted = WorkspacePathPolicy(base_dir, repro_write=True)
    plain = WorkspacePathPolicy(base_dir)
    relative = write_reproduction_test(
        granted, "test_repro_boundary.py", "def test_x():\n    pass\n"
    )
    assert relative == REPRO
    assert (base_dir / REPRO).is_file()

    # 没有复现凭证的策略连规范目录都写不进去。
    with pytest.raises(ValueError, match="trusted reproduction context"):
        write_reproduction_test(plain, "test_repro_boundary.py", "def test_x():\n    pass\n")
    with pytest.raises(ValueError, match="not a reproduction test file"):
        write_reproduction_test(granted, "helper.py", "x = 1\n")
    with pytest.raises(ValueError, match="protected configuration"):
        write_reproduction_test(granted, "conftest.py", "x = 1\n")


def test_workspace_manager_provider_uses_an_independent_base_workspace(tmp_path: Path) -> None:
    """原始仓库的内容与 Git 状态在复现校验前后必须完全一致（D-03 最后一条）。"""
    original = tmp_path / "original"
    (original / "src/pkg").mkdir(parents=True)
    (original / "src/pkg/core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    repo = init_repo(original, {"src/pkg/core.py": "def f():\n    return 1\n"})
    before_status = run_git(["status", "--porcelain"], cwd=repo.path).stdout
    before_head = repo.head

    provider = WorkspaceManagerBaseProvider(
        WorkspaceManager(cache_root=tmp_path / "cache"),
        source=str(repo.path),
        base_commit=before_head,
        root=tmp_path / "repro-workspaces",
    )
    base = provider.acquire(attempt=1)
    try:
        assert base.path != repo.path
        assert base.base_commit == before_head
        assert (base.path / "src/pkg/core.py").read_text(
            encoding="utf-8"
        ) == "def f():\n    return 1\n"
        write_reproduction_test(
            WorkspacePathPolicy(base.path, repro_write=True),
            "test_repro_boundary.py",
            "def test_x():\n    pass\n",
        )
        assert (base.path / REPRO).is_file()
        # 复现文件只存在于独立 workspace，不会回写用户仓库。
        assert not (repo.path / REPRO).exists()
    finally:
        provider.release(base)

    assert not base.path.exists()
    assert run_git(["status", "--porcelain"], cwd=repo.path).stdout == before_status
    assert repo.git("rev-parse", "HEAD").strip() == before_head
    assert (repo.path / "src/pkg/core.py").read_text(encoding="utf-8") == "def f():\n    return 1\n"
