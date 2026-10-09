"""跨模块复用的基础类型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["TraceFrame"]


class TraceFrame(BaseModel):
    """traceback 中的一帧（02「ParsedFailure」）。

    ``in_repo`` 是把仓库帧与第三方帧区分开的关键字段：诊断只保留仓库内帧，
    定位只把仓库内帧当作强种子，回归判定也不看第三方帧。
    """

    model_config = ConfigDict(frozen=True)

    file: str
    line: int
    function: str
    in_repo: bool
    # 文档写作 `code: str | None`。多数情况下 traceback 里拿不到源码行
    # （pytest 的 short traceback 格式尤其如此），所以给默认值 None，
    # 避免构造时必须显式传 None。
    code: str | None = None
