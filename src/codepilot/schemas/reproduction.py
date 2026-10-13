"""复现结果（02 §10.5）。

复现测试是 Agent 自用的"目标测试"：SWE-bench 的判分测试在推理阶段不可见，
所以必须自己造一个能复现问题的测试，否则"修好了"没有可验证的判据。

本模块是复现契约的**唯一定义处**（docs/05 D-01），并集中了两个必须全局一致的
常量：复现目录与尝试次数上限。目录常量放在这里而不是 ``agents/reproducer.py``，
是因为 ``patch/diff.py`` 与 ``patch/validator.py`` 都要用它做剔除与权限判定，
而 L0 契约层不允许反向依赖 agent 层。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ENVIRONMENT_FAILURE_KINDS",
    "MAX_REPRO_ATTEMPTS",
    "MAX_REPRO_RETRIES",
    "REPRO_TEST_DIRECTORY",
    "ReproductionAttempt",
    "ReproductionFailureKind",
    "ReproductionResult",
    "ReproductionStatus",
    "is_reproduction_test_path",
    "reproduction_node_id",
    "validate_reproduction_test_path",
]

#: 复现测试的专用目录。**大小写与层级都必须精确匹配**——伪造
#: ``tests/_CODEPILOT_REPRO/`` 或 ``tests/_codepilot_repro_extra/`` 不算复现目录，
#: 因此既拿不到写入例外，也不会被从最终 patch 中剔除。
REPRO_TEST_DIRECTORY = "tests/_codepilot_repro"
_REPRO_PARTS = ("tests", "_codepilot_repro")

#: 复现目录内也不允许触碰的配置名：复现夹具不能反过来改掉测试框架的行为。
_REPRO_FORBIDDEN_NAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "__init__.py",
    }
)

#: docs/05 D-04 的"最多重试 3 次"实现为 **1 次初始尝试 + 最多 3 次重试**。
#: ``attempts`` 记录的是总尝试次数（生成 + 在 base 上校验），因此上限是 4。
#: 计数规则写在这里是为了消除 off-by-one：``retries = attempts - 1``。
MAX_REPRO_RETRIES = 3
MAX_REPRO_ATTEMPTS = MAX_REPRO_RETRIES + 1


class ReproductionStatus(StrEnum):
    REPRODUCED = "reproduced"
    """复现测试在 base commit 上按预期失败。"""

    FAILED = "failed"
    """尝试若干次仍未拿到有效复现，流程继续但只能依赖已有测试。"""

    SKIPPED = "skipped"
    """未尝试复现（例如 Issue 描述里没有可验证的行为）。"""

    ENV_ERROR = "env_error"
    """base Workspace 或 Sandbox 不可用，无法判定复现。

    与 ``FAILED`` 分开是刻意的：环境坏了的时候"重试 3 次"只会重复拿到同一个
    坏结果，还烧掉 LLM 预算；调用方应当按既有 Sandbox 故障路由处理它。
    """


class ReproductionFailureKind(StrEnum):
    """一次候选复现尝试的真实结局。

    这些取值不是为了好看：D-08 的失败类别分布必须能回答"到底是因为什么没复现
    成功"，而把所有原因都塞进一个 ``failed`` 就无法区分"测试写错了"和
    "容器起不来"——前者该重试，后者不该。
    """

    VALID_ASSERTION = "valid_assertion"
    """base 上以断言类失败结束（AssertionError 或 pytest 断言族）。"""

    VALID_ISSUE_EXCEPTION = "valid_issue_exception"
    """base 上以 Issue 明确点名的异常类型失败。"""

    INVALID_PASS = "invalid_pass"
    """base 上通过了：没有复现。"""

    INVALID_SKIPPED = "invalid_skipped"
    """base 上被跳过：没有执行，不能作为证据。"""

    INVALID_NOT_EXECUTED = "invalid_not_executed"
    """指定的 pytest NodeID 没有出现在结果里。"""

    INVALID_UNDECLARED_EXCEPTION = "invalid_undeclared_exception"
    """失败的异常类型与 Issue 没有可追溯的对应关系。"""

    INVALID_IMPORT_ERROR = "invalid_import_error"
    """由候选测试自身的导入失败造成，属于伪复现。"""

    INVALID_SYNTAX_ERROR = "invalid_syntax_error"
    """由候选测试自身的语法错误造成，属于伪复现。"""

    INVALID_FIXTURE_ERROR = "invalid_fixture_error"
    """夹具/setup 阶段报错，测试体没有真正执行。"""

    INVALID_UNRELATED_TO_LOCALIZATION = "invalid_unrelated_to_localization"
    """候选测试没有引用任何定位到的文件，与 Issue 的联系不可追溯。"""

    INVALID_PSEUDO_REPRODUCTION = "invalid_pseudo_reproduction"
    """静态筛查命中已知伪复现模式（无条件 assert False、pytest.fail 等）。"""

    INVALID_NO_ASSERTION = "invalid_no_assertion"
    """候选测试没有任何断言或对期望行为的显式声明。"""

    LLM_OUTPUT_ERROR = "llm_output_error"
    """结构化输出在所有纠正尝试后仍不合法。"""

    COLLECTION_ERROR = "collection_error"
    """pytest 收集阶段失败，没有任何测试被执行。"""

    TIMEOUT = "timeout"
    """执行超时。Issue 没有超时证据时不作为有效复现。"""

    ENV_ERROR = "env_error"
    SANDBOX_ERROR = "sandbox_error"
    WORKSPACE_ERROR = "workspace_error"
    """独立 base Workspace 无法创建、写入或清理。"""


#: 这些结局不是"复现没成功"，而是执行环境本身不可信，因此不消耗 LLM 重试预算。
ENVIRONMENT_FAILURE_KINDS: frozenset[ReproductionFailureKind] = frozenset(
    {
        ReproductionFailureKind.ENV_ERROR,
        ReproductionFailureKind.SANDBOX_ERROR,
        ReproductionFailureKind.WORKSPACE_ERROR,
    }
)

_VALID_REPRODUCTION_KINDS = frozenset(
    {
        ReproductionFailureKind.VALID_ASSERTION,
        ReproductionFailureKind.VALID_ISSUE_EXCEPTION,
    }
)


class ReproductionAttempt(BaseModel):
    """一次尝试的审计记录：状态只能追加，不被后续尝试覆盖（D-04）。"""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, le=MAX_REPRO_ATTEMPTS, strict=True)
    kind: ReproductionFailureKind
    detail: str = Field(min_length=1, max_length=2000)
    """进对象之前必须已脱敏并截断；上限存在是为了让 state 不被日志撑爆。"""

    test_path: str | None = None
    node_id: str | None = None
    failure_type: str | None = None
    exit_code: int | None = None

    @field_validator("test_path")
    @classmethod
    def _checked_path(cls, value: str | None) -> str | None:
        return validate_reproduction_test_path(value)

    @model_validator(mode="after")
    def _consistent(self) -> ReproductionAttempt:
        if self.kind in _VALID_REPRODUCTION_KINDS and self.test_path is None:
            raise ValueError("有效复现尝试必须记录复现文件路径")
        return self


class ReproductionResult(BaseModel):
    """Reproduction Agent 的对外结论（02 §10.5）。

    字段集合就是 docs/05 D-01 的契约，另外追加了两个**可选**审计字段
    （``kind`` / ``history``）；它们带默认值，所以既有
    ``RepairState.reproduction`` 的读写与 round-trip 不受影响。
    """

    model_config = ConfigDict(extra="forbid")

    status: ReproductionStatus

    test_path: str | None = None
    """复现测试文件路径，形如 ``tests/_codepilot_repro/test_repro.py``。

    该文件只用于 Agent 内部判断，最终 patch 中会被剔除（02 §10.5）。
    """

    fail_reason: str | None = Field(default=None, max_length=2000)
    """base commit 上的失败信息。用于确认失败原因与 Issue 描述相关。"""

    attempts: int = Field(default=0, ge=0, le=MAX_REPRO_ATTEMPTS, strict=True)
    """真实尝试次数（1 次初始 + 重试），由校验循环逐次累加，不允许手写虚构。"""

    kind: ReproductionFailureKind | None = None
    """结局类别。``REPRODUCED`` 时是有效类型，其余是失败或环境原因。"""

    history: list[ReproductionAttempt] = Field(default_factory=list)
    """每次尝试的证据，按 ``index`` 递增且互不覆盖。"""

    @field_validator("test_path")
    @classmethod
    def _checked_path(cls, value: str | None) -> str | None:
        return validate_reproduction_test_path(value)

    @model_validator(mode="after")
    def _consistent(self) -> ReproductionResult:
        indices = [item.index for item in self.history]
        if indices != list(range(1, len(indices) + 1)):
            raise ValueError("attempts 历史必须是自 1 开始、逐次递增的连续记录")
        if len(self.history) != self.attempts:
            raise ValueError(
                f"attempts={self.attempts} 与历史条目数 {len(self.history)} 不一致；"
                "尝试次数只能由真实执行累加"
            )

        if self.status is ReproductionStatus.REPRODUCED:
            if self.test_path is None:
                raise ValueError("REPRODUCED 必须给出复现测试路径")
            if not (self.fail_reason or "").strip():
                raise ValueError("REPRODUCED 必须给出 base 上的失败证据")
            if self.kind not in _VALID_REPRODUCTION_KINDS:
                raise ValueError("REPRODUCED 的结局必须是有效复现类型")
            return self

        if self.test_path is not None:
            raise ValueError(f"{self.status.value} 不能声称拥有复现测试文件")

        if self.status is ReproductionStatus.SKIPPED:
            if self.attempts or self.history:
                raise ValueError("SKIPPED 表示没有尝试，attempts 必须为 0")
            return self

        if self.attempts < 1:
            raise ValueError(f"{self.status.value} 必须至少有一次真实尝试的证据")
        if not (self.fail_reason or "").strip():
            raise ValueError(f"{self.status.value} 必须记录实际失败证据")
        if self.kind is None:
            raise ValueError(f"{self.status.value} 必须记录失败结局类别")
        if self.status is ReproductionStatus.ENV_ERROR and (
            self.kind not in ENVIRONMENT_FAILURE_KINDS
        ):
            raise ValueError("ENV_ERROR 状态只能由环境类结局触发，不能是普通复现失败")
        if self.status is ReproductionStatus.FAILED and self.kind in ENVIRONMENT_FAILURE_KINDS:
            raise ValueError("环境类结局必须以 ENV_ERROR 状态记录，不能伪装成复现失败")
        return self

    @property
    def retries(self) -> int:
        """重试次数 = 总尝试次数 - 1（计数规则见 ``MAX_REPRO_RETRIES``）。"""
        return max(self.attempts - 1, 0)

    @property
    def degraded(self) -> bool:
        """是否需要降级到仓库已有测试作为目标测试。"""
        return self.status in {
            ReproductionStatus.FAILED,
            ReproductionStatus.SKIPPED,
            ReproductionStatus.ENV_ERROR,
        }


def validate_reproduction_test_path(value: str | None) -> str | None:
    """复现路径必须是可信 Workspace 内的规范相对路径。

    刻意**不**用 ``startswith()``：绝对路径、盘符、UNC、反斜杠、``..``、伪造
    目录名与大小写变体都在这里被拒，目录前缀必须逐段精确匹配。
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise ValueError("复现路径不能是空白字符串")
    posix = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.anchor)
        or "\\" in text
        or ".." in posix.parts
        or posix.as_posix() != text
    ):
        raise ValueError(f"复现路径必须是 Workspace 内的规范相对路径: {value!r}")
    if len(posix.parts) != len(_REPRO_PARTS) + 1:
        raise ValueError(f"复现文件必须直接位于 {REPRO_TEST_DIRECTORY}/ 下一层: {value!r}")
    if posix.parts[: len(_REPRO_PARTS)] != _REPRO_PARTS:
        raise ValueError(
            f"复现路径必须逐段精确位于 {REPRO_TEST_DIRECTORY}/ 内（伪造目录名与"
            f"大小写变体都不算）: {value!r}"
        )
    leaf = posix.name
    if leaf.casefold() in _REPRO_FORBIDDEN_NAMES:
        raise ValueError(f"复现路径不得指向受保护配置: {value!r}")
    if not (leaf.startswith("test_") and leaf.endswith(".py")) or leaf == "test_.py":
        raise ValueError(f"复现文件必须是 {REPRO_TEST_DIRECTORY}/ 下的 test_*.py: {value!r}")
    return text


def is_reproduction_test_path(relative: str) -> bool:
    """是否位于专用复现目录内（精确匹配，供 diff 剔除与统计隔离使用）。"""
    try:
        return validate_reproduction_test_path(relative) is not None
    except ValueError:
        return False


def reproduction_node_id(test_path: str, test_name: str) -> str:
    """复现测试的 pytest NodeID。"""
    validated = validate_reproduction_test_path(test_path)
    if validated is None:
        raise ValueError("复现 NodeID 需要具体文件路径")
    if not test_name or not test_name.replace("_", "").isalnum():
        raise ValueError(f"非法测试函数名: {test_name!r}")
    return f"{validated}::{test_name}"
