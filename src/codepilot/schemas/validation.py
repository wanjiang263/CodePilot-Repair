"""Patch 校验结果（02 §12）。

文档在 ``Attempt`` 中引用了 ``ValidationResult`` 但没有给出定义，这里补齐。
字段按"能否直接喂回给 Coding Agent"来设计：``errors`` 是给人/模型看的拒绝
原因，会被附回下一轮消息；``checks`` 与 ``cheat_findings`` 是给测试和报告
用的结构化记录。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from codepilot.schemas.enums import RiskLevel

__all__ = [
    "CheatFinding",
    "CheatPattern",
    "CheckName",
    "ValidationCheck",
    "ValidationResult",
]


class CheckName(StrEnum):
    """02 §12 主表中的六项检查，顺序即执行顺序。"""

    PATH = "path"
    SIZE = "size"
    SYNTAX = "syntax"
    NON_EMPTY = "non_empty"
    TEST_PROTECTION = "test_protection"
    CHEAT = "cheat"


class CheatPattern(StrEnum):
    """02 §12 作弊模式表中的六种模式。"""

    DELETED_TEST = "deleted_test"
    SKIPPED_TEST = "skipped_test"
    WEAKENED_ASSERTION = "weakened_assertion"
    SWALLOWED_EXCEPTION = "swallowed_exception"
    HARDCODED_EXPECTATION = "hardcoded_expectation"
    ENVIRONMENT_PROBE = "environment_probe"


class ValidationCheck(BaseModel):
    """一项检查的结果。即使通过也保留，便于报告里逐项展示。"""

    name: CheckName
    passed: bool
    reason: str | None = None


class CheatFinding(BaseModel):
    """一条作弊模式命中记录。

    ``HARDCODED_EXPECTATION`` 是启发式的，只标记给 Reviewer，不直接拒绝
    （02 §12 表末行），因此需要 ``reject`` / ``warn`` 区分。
    """

    pattern: CheatPattern
    rule_id: str = ""
    file: str
    line: int | None = None
    detail: str
    reject: bool = True


class ValidationResult(BaseModel):
    """一次 patch 校验的完整结果。

    ``passed`` 为 False 时计入迭代次数并把 ``errors`` 返回给 Coding Agent。
    """

    passed: bool
    checks: list[ValidationCheck] = Field(default_factory=list)
    cheat_findings: list[CheatFinding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    files_changed: int = 0
    lines_added: int = 0
    lines_deleted: int = 0
    risk: RiskLevel | None = None
    evidence: list[str] = Field(default_factory=list)

    @property
    def lines_changed(self) -> int:
        """规模预算比较用的是增删行数之和（02 §12「规模」）。"""
        return self.lines_added + self.lines_deleted

    @property
    def rejecting_findings(self) -> list[CheatFinding]:
        return [f for f in self.cheat_findings if f.reject]
