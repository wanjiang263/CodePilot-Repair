"""失败诊断（02 §14.6）。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from codepilot.schemas.enums import FailureCategory, RepairStrategy

__all__ = ["Diagnosis", "Evidence", "EvidenceKind"]


class EvidenceKind(StrEnum):
    TRACEBACK = "traceback"
    CODE = "code"
    TEST = "test"


class Evidence(BaseModel):
    """一条诊断证据。

    ``file:line`` 必须真实存在：DiagnosisAgent 会删除不存在的条目并按删除比例
    降低 confidence（02 §14.6）。LLM 编造行号是很常见的失败模式。
    """

    kind: EvidenceKind
    file: str
    line: int | None = None
    text: str = ""


class Diagnosis(BaseModel):
    failure_category: FailureCategory = FailureCategory.UNKNOWN
    root_cause: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    suspected_symbols: list[str] = Field(default_factory=list)

    is_patch_caused: bool = False
    """失败是本轮 patch 引入的，还是原问题未被修复。

    这个字段会触发强制策略切换：``is_patch_caused=True`` 且连续两轮 ``refine``
    无进展时，强制改为 ``revert_and_retry``（02 §15.6）。
    """

    repair_strategy: RepairStrategy = RepairStrategy.REFINE

    confidence: float = 0.0
    """**只用于日志和分析，不参与控制流程**（02 §14.6 末句）。

    LLM 自报的置信度没有校准，用它做决策会引入无法调试的行为。
    """
