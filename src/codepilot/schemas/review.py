"""Patch 复审（02 §10.6）。"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from codepilot.schemas.enums import ReviewSeverity, RiskLevel

__all__ = ["ReviewIssue", "ReviewResult"]


class ReviewIssue(BaseModel):
    """一条复审意见。

    ``severity`` 是控制流程的字段：只有 ``HIGH`` 会触发一次额外修复
    （02 §10.6），其他等级只记录在报告里。
    """

    model_config = ConfigDict(extra="forbid")

    severity: ReviewSeverity
    file: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1, strict=True)
    description: str = ""

    @field_validator("description")
    @classmethod
    def nonempty_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review evidence description must be nonempty")
        return value

    @field_validator("file")
    @classmethod
    def valid_path(cls, value: str) -> str:
        path = PurePosixPath(value.replace("\\", "/"))
        if (
            path.is_absolute()
            or PureWindowsPath(value).anchor
            or ".." in path.parts
            or not value.strip()
        ):
            raise ValueError("review file must be repository relative")
        return path.as_posix()


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage_known: bool = True
    approved: bool = Field(strict=True)
    risk: RiskLevel = RiskLevel.LOW
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = ""

    @property
    def has_high_issues(self) -> bool:
        """决定是否需要额外修复轮。"""
        return any(i.severity is ReviewSeverity.HIGH for i in self.issues)
