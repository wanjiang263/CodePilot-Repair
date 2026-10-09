"""测试执行结果（02 §13.4 / §13.5）。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from codepilot.schemas.enums import ExecutionStatus, TestOutcome

__all__ = ["ExecutionResult", "TestBaseline", "TestCaseResult"]


class TestCaseResult(BaseModel):
    """单个测试用例的结果，来自 junit XML。"""

    # 类名以 Test 开头是设计文档定的（junit 语义），但 pytest 会把任何
    # 名字匹配 Test* 的类当成测试类收集。显式关掉，否则每个 import 了它的
    # 测试模块都会收到 PytestCollectionWarning。
    __test__: ClassVar[bool] = False

    node_id: str
    """形如 ``tests/test_auth.py::test_login``。"""

    outcome: TestOutcome
    duration: float
    message: str | None = None
    traceback: str | None = None


class ExecutionResult(BaseModel):
    """一次测试执行的完整结果。

    ``stdout_tail`` / ``stderr_tail`` 是**截断后**的尾部，完整内容写在
    ``artifact_dir`` 里。这样 state 不会因为一个超长日志而膨胀。
    """

    status: ExecutionStatus
    exit_code: int | None = None
    duration_seconds: float = 0.0
    tests: list[TestCaseResult] = Field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    artifact_dir: str = ""

    @property
    def failed_tests(self) -> list[TestCaseResult]:
        """返回 failed 与 error 两类用例。

        判定"目标测试是否通过"必须把 error 也算作失败，否则一个报
        AttributeError 的测试会被当成"没失败"。
        """
        return [t for t in self.tests if t.outcome in (TestOutcome.FAILED, TestOutcome.ERROR)]

    @property
    def is_environment_problem(self) -> bool:
        """环境问题不进入诊断修复循环（02 §13.4 末段）。"""
        return self.status in (ExecutionStatus.ENV_ERROR, ExecutionStatus.SANDBOX_ERROR)


class TestBaseline(BaseModel):
    """base commit 上运行两次全量测试得到的基线。

    后续所有回归判定都以它为参照：只有 ``passed`` → ``failed`` / ``error``
    才算新增回归，base 上本来就失败的测试不算。
    """

    __test__: ClassVar[bool] = False

    base_commit: str
    outcomes: dict[str, TestOutcome] = Field(default_factory=dict)
    flaky: set[str] = Field(default_factory=set)
    """两次运行结果不一致的测试。这些测试永远不计入回归。"""
