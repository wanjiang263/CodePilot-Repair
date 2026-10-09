"""定位结果（02 §10.2）。

文档引用了 ``RankedFile`` / ``RankedSymbol`` 但没有定义，这里复用
``schemas.retrieval`` 中的同名类型：检索候选项与定位选中项本来就是同一种东西
（同一个 symbol），只是定位之后多了行范围和理由。分开定义会导致
"检索说 file A，定位说 file A"两套对象无法直接比较。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from codepilot.schemas.retrieval import RankedFile, RankedSymbol

__all__ = ["LocalizationResult"]


class LocalizationResult(BaseModel):
    """文件级 → symbol 级的定位结果。

    文件级最多 3 个、symbol 级最多 5 个（02 §10.2）。上限由 Localizer 保证，
    这里只记录结果。
    """

    files: list[RankedFile] = Field(default_factory=list)
    symbols: list[RankedSymbol] = Field(default_factory=list)

    retrieval_snapshot_id: str = ""
    """指向完整检索结果。定位错了的时候，要能回看当时喂给模型的是什么。"""
