"""Agent 层的数据契约（02 §10.1，C-01）。

``IssueAnalysis`` 的**权威定义在 L0 契约层** ``codepilot.schemas.issue``。
§1.3 把 ``codepilot.agents.schemas`` 列为 agent 层入口，所以这里按那个入口
重新导出同一个类型，而不是复制一份字段——两份定义早晚会漂移。

本模块只补 agent 层才需要的两个配套类型：

* :class:`RuleEvidence` —— C-02 规则抽取的中间结果。它的每个字段都是 Issue
  文本里能逐字核对的证据，``query`` 保留 V-01 解析出的完整查询，检索与定位
  直接复用它，不需要再反推。
* :class:`IssueSemanticDraft` —— C-03 允许 LLM 产出的字段集合。它刻意**没有**
  ``traceback_frames`` / ``mentioned_files`` / ``mentioned_symbols`` /
  ``error_messages`` 字段，模型返回的这些键在校验阶段就被丢弃：规则证据不被
  覆盖，这是类型层面的保证，不依赖 prompt 里"请不要修改"的恳求。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from codepilot.schemas.common import TraceFrame
from codepilot.schemas.diagnosis import Diagnosis, Evidence, EvidenceKind
from codepilot.schemas.enums import IssueType
from codepilot.schemas.issue import IssueAnalysis
from codepilot.schemas.localization import FileDecision, LocalizationResult, SymbolDecision
from codepilot.schemas.plan import PlannedChange, RepairPlan
from codepilot.schemas.reproduction import (
    ReproductionAttempt,
    ReproductionFailureKind,
    ReproductionResult,
    ReproductionStatus,
)
from codepilot.schemas.retrieval import RankedSymbol, RetrievalQuery

__all__ = [
    "Diagnosis",
    "Evidence",
    "EvidenceKind",
    "FileDecision",
    "IssueAnalysis",
    "IssueSemanticDraft",
    "IssueType",
    "LocalizationResult",
    "PlannedChange",
    "RankedSymbol",
    "RepairPlan",
    "ReproductionAttempt",
    "ReproductionFailureKind",
    "ReproductionResult",
    "ReproductionStatus",
    "RuleEvidence",
    "SymbolDecision",
    "TraceFrame",
]


class RuleEvidence(BaseModel):
    """规则层能确证的 Issue 事实（C-02）。

    这些字段的取值只来自 Issue 文本本身：路径是文中写出来的，异常名是文中
    出现的，帧是正则匹配到的。抽取不出来就留空，绝不猜测——所以"字段非空"
    永远意味着"文本里确实有"。

    ``issue_type`` 允许为 ``None``：分类是语义判断，规则只有在词面证据足够时
    才表态，剩下的交给 LLM（C-03），而不是默认成 ``bug`` 假装已经判过。
    """

    mentioned_files: list[str] = Field(default_factory=list)
    """正文、代码块与 traceback 里出现的 ``*.py`` 路径，已按出现顺序去重。"""

    mentioned_symbols: list[str] = Field(default_factory=list)
    """显式提及的标识符（反引号词、下划线/驼峰/点号词、异常名、帧函数名）。"""

    error_messages: list[str] = Field(default_factory=list)
    """异常行原文与异常消息，以及被引号包住的诊断文本。"""

    traceback_frames: list[TraceFrame] = Field(default_factory=list)
    """结构化帧；``in_repo`` 由仓库文件清单判定，是 C-04 置顶的唯一依据。"""

    reproduction_steps: list[str] = Field(default_factory=list)
    """复现小节里的有序条目，逐字保留。"""

    keywords: list[str] = Field(default_factory=list)
    """V-01 的检索词元（含标识符切分），已过滤停用词。"""

    issue_type: IssueType | None = None

    query: RetrievalQuery
    """V-01 解析出的完整查询。检索与 V-18 置顶直接吃它，避免二次转换。"""


class IssueSemanticDraft(BaseModel):
    """LLM 的结构化输出（C-03）。

    ``extra="ignore"``：模型顺手补一个 ``traceback_frames`` 是最常见的失败模式，
    多出来的键在这里直接丢弃，永远不会进入 :class:`~codepilot.schemas.issue.IssueAnalysis`
    的可验证字段。``None`` / 空串表示"模型没意见"，由调用方回退到规则值。
    """

    model_config = ConfigDict(extra="ignore")

    issue_type: IssueType | None = None
    summary: str = ""
    expected_behavior: str | None = None
    observed_behavior: str | None = None
    reproduction_steps: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
