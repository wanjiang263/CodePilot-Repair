"""检索结果（02 §8）。

文档只给出了融合函数，没有定义结果类型，这里补齐。设计目标是**可解释**：
每个候选都要能回答"它为什么在这里、每个通道各给了多少分"，
否则 Phase 2 的通道消融和 Phase 12 的失败归因都做不了。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

__all__ = [
    "ChannelScore",
    "FusionMethod",
    "RankedFile",
    "RankedSymbol",
    "RetrievalQuery",
    "RetrievalResult",
]


class FusionMethod(StrEnum):
    """02 §8.3 要求对比两种融合方式，选更好的作为默认。"""

    WEIGHTED = "weighted"
    RRF = "rrf"


class ChannelScore(BaseModel):
    """某个候选在某个通道上的得分。"""

    channel: str
    raw: float
    """通道内原始分（BM25 分、余弦相似度、图扩展衰减分……）。"""

    normalized: float
    """在本次查询候选集内 min-max 归一化后的分。"""


class RankedSymbol(BaseModel):
    """函数级候选。

    同一类型同时服务于检索结果与定位结果：``Localizer`` 选出的 symbol 会补上
    ``start_line`` / ``end_line`` / ``reason``（02 §10.2）。
    """

    symbol_id: str
    qualified_name: str
    file: str
    score: float
    kind: str | None = None
    channel_scores: list[ChannelScore] = Field(default_factory=list)

    pinned: bool = False
    """traceback 中直接出现的仓库内 symbol。置顶且不参与融合（02 §8.3）。"""

    start_line: int | None = None
    end_line: int | None = None
    reason: str | None = None


class RankedFile(BaseModel):
    """文件级候选。

    文件分 = 该文件内 symbol 融合分的最大值 + 0.1 × 其余 Top-3 symbol 分之和
    （02 §8.4）。
    """

    path: str
    score: float
    top_symbols: list[str] = Field(default_factory=list)
    reason: str | None = None


class RetrievalQuery(BaseModel):
    """从 Issue 构造出的查询（02 §8.1）。保留下来是为了事后分析"检索为什么错"。"""

    raw_issue: str
    identifiers: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    file_paths: list[str] = Field(default_factory=list)
    traceback_frames: list[str] = Field(default_factory=list)
    test_names: list[str] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    """一次检索的完整结果。"""

    query: RetrievalQuery
    symbols: list[RankedSymbol] = Field(default_factory=list)
    files: list[RankedFile] = Field(default_factory=list)

    channel_weights: dict[str, float] = Field(default_factory=dict)
    fusion_method: FusionMethod = FusionMethod.WEIGHTED
    enabled_channels: list[str] = Field(default_factory=list)

    snapshot_id: str = ""
    """指向持久化的完整检索结果，供 LocalizationResult 事后引用。"""
