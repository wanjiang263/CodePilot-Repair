"""影响面分析（02 §10.3）。

文档给出了风险分级规则但没有定义结果类型，这里补齐。风险等级**不阻止修改**，
它影响 Review 的严格程度和回归测试的范围（02 §10.3 末句），因此这里记录的是
"为什么是这一级"，便于事后核对是否判错。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from codepilot.schemas.enums import RiskLevel

__all__ = ["ImpactAnalysis", "ImpactedSymbol", "ImpactedTest", "Relation"]


class Relation(StrEnum):
    """受影响 symbol 与改动点的关系。"""

    CALLER = "caller"
    CALLEE = "callee"
    SUBCLASS = "subclass"
    BASE = "base"
    SAME_FILE = "same_file"


class ImpactedSymbol(BaseModel):
    symbol_id: str = ""
    qualified_name: str
    file: str
    relation: Relation
    distance: int = 1
    edge_kind: str | None = None
    confidence: str | None = None
    evidence: str = ""
    """图上距离。V1 只取深度 1（02 §10.3）。"""


class ImpactedTest(BaseModel):
    path: str
    symbol_id: str | None = None
    qualified_name: str | None = None
    confidence: str | None = None
    source: str
    evidence: str = ""


class ImpactAnalysis(BaseModel):
    """纯规则产出，不调用 LLM。"""

    changed_symbols: list[str] = Field(default_factory=list)
    """本轮 patch 实际改到的 symbol（限定在定位给出的符号范围内）。"""

    callers: list[ImpactedSymbol] = Field(default_factory=list)
    callees: list[ImpactedSymbol] = Field(default_factory=list)
    subclasses: list[ImpactedSymbol] = Field(default_factory=list)
    related_tests: list[str] = Field(default_factory=list)
    related_test_evidence: list[ImpactedTest] = Field(default_factory=list)

    risk: RiskLevel = RiskLevel.LOW
    risk_reasons: list[str] = Field(default_factory=list)
    risk_estimated: bool = True
    missing_symbols: list[str] = Field(default_factory=list)
