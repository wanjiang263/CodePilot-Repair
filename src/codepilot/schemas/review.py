"""Patch 复审（02 §10.6）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codepilot.schemas.enums import ReviewSeverity, RiskLevel

__all__ = ["ReviewIssue", "ReviewResult"]


class ReviewIssue(BaseModel):
    """一条复审意见。

    ``severity`` 是控制流程的字段：只有 ``HIGH`` 会触发一次额外修复
    （02 §10.6），其他等级只记录在报告里。
    """

    severity: ReviewSeverity
    file: str
    line: int | None = None
    description: str = ""


class ReviewResult(BaseModel):
    approved: bool
    risk: RiskLevel = RiskLevel.LOW
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = ""

    @property
    def has_high_issues(self) -> bool:
        """决定是否需要额外修复轮。"""
        return any(i.severity is ReviewSeverity.HIGH for i in self.issues)
