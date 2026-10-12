"""定位结果（02 §10.2）。

文档引用了 ``RankedFile`` / ``RankedSymbol`` 但没有定义，这里复用
``schemas.retrieval`` 中的同名类型：检索候选项与定位选中项本来就是同一种东西
（同一个 symbol），只是定位之后多了行范围和理由。分开定义会导致
"检索说 file A，定位说 file A"两套对象无法直接比较。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from codepilot.schemas.retrieval import RankedFile, RankedSymbol

__all__ = ["FileDecision", "LocalizationResult", "SymbolDecision"]


class FileDecision(BaseModel):
    """LLM inference, separate from RankedFile's original retrieval evidence."""

    stage: Literal["files"] = "files"
    candidate_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SymbolDecision(BaseModel):
    """Proposed subrange; RankedSymbol keeps the actual indexed full range."""

    stage: Literal["symbols"] = "symbols"
    symbol_id: str = Field(min_length=1)
    file: str = Field(min_length=1)
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)
    file_line_count: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=1)


class LocalizationResult(BaseModel):
    """文件级 → symbol 级的定位结果。

    Ranked items preserve retrieval evidence; decisions record model inference.
    Nonempty results require one verified decision per selected identity.
    """

    files: list[RankedFile] = Field(default_factory=list, max_length=3)
    symbols: list[RankedSymbol] = Field(default_factory=list, max_length=5)
    file_decisions: list[FileDecision] = Field(default_factory=list, max_length=3)
    symbol_decisions: list[SymbolDecision] = Field(default_factory=list, max_length=5)

    retrieval_snapshot_id: str = ""
    """指向完整检索结果。定位错了的时候，要能回看当时喂给模型的是什么。"""

    @model_validator(mode="after")
    def _verified_membership(self) -> LocalizationResult:
        paths = [file.path for file in self.files]
        ids = [symbol.symbol_id for symbol in self.symbols]
        if len(set(paths)) != len(paths) or len(set(ids)) != len(ids):
            raise ValueError("Duplicate localization identity")
        if [decision.path for decision in self.file_decisions] != paths:
            raise ValueError("Each file requires its own localization decision")
        if len({decision.candidate_id for decision in self.file_decisions}) != len(paths):
            raise ValueError("Duplicate file candidate identity")
        if [decision.symbol_id for decision in self.symbol_decisions] != ids:
            raise ValueError("Each symbol requires its own localization decision")
        if (paths or ids) and not self.retrieval_snapshot_id.strip():
            raise ValueError("Nonempty localization requires a retrieval snapshot")
        decisions: list[FileDecision | SymbolDecision] = [
            *self.file_decisions,
            *self.symbol_decisions,
        ]
        for decision in decisions:
            if not decision.reason.strip():
                raise ValueError("Localization reasons must be nonempty")
        for symbol, decision in zip(self.symbols, self.symbol_decisions, strict=True):
            if symbol.file not in paths or decision.file != symbol.file:
                raise ValueError("Symbol is not in a selected file")
            start, end = symbol.start_line, symbol.end_line
            if start is None or end is None or not 1 <= start <= end <= decision.file_line_count:
                raise ValueError("Invalid indexed symbol range")
            if not start <= decision.start_line <= decision.end_line <= end:
                raise ValueError("Proposed range is outside the indexed symbol")
        return self
