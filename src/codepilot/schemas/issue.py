"""Issue 分析结果（02 §10.1）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codepilot.schemas.common import TraceFrame
from codepilot.schemas.enums import IssueType

__all__ = ["IssueAnalysis"]


class IssueAnalysis(BaseModel):
    """Issue 的结构化理解。

    前四个字段由 LLM 负责，其余由规则抽取。规则抽取的字段**不允许**被 LLM
    覆盖（02 §10.1）：LLM 很容易"顺手编一个" traceback，而正则抽出来的是
    Issue 里真实存在的内容，后者才可信。
    """

    issue_type: IssueType = IssueType.BUG
    summary: str = ""
    expected_behavior: str | None = None
    observed_behavior: str | None = None

    reproduction_steps: list[str] = Field(default_factory=list)
    mentioned_files: list[str] = Field(default_factory=list)
    mentioned_symbols: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    traceback_frames: list[TraceFrame] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
