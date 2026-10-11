"""Issue 分析（02 §10.1，C-02 / C-03 / C-04）。

两层职责，顺序不可颠倒：

1. **规则层**（C-02）从原文里抠出能逐字核对的事实——traceback、路径、标识符、
   异常消息、复现步骤。它复用 Phase 2 的 V-01 解析（``retrieval.query.build_query``），
   所以同一份 Issue 在检索侧和定位侧看到的是同一套证据，不会出现两套解释。
2. **LLM 层**（C-03）只补语义：类型、摘要、期望/实际行为、补充关键词。

合并时规则字段永远赢（:class:`~codepilot.agents.schemas.IssueSemanticDraft` 从类型上
就没有 traceback 字段，多余的键在 pydantic 校验阶段被丢弃）。LLM 猜出来的文件、
行号、根因不会变成"已验证事实"，这是本模块存在的核心理由：模型顺手编一段
traceback 的概率不低，而那段 traceback 会直接决定检索置顶，进而决定后面所有
步骤看哪些代码。

C-04 的仓库内种子同样复用 Phase 2 的表示：解析出的帧带 ``in_repo``，交给 V-18 的
``pin_traceback`` 得到 ``pinned=True`` 的 ``RankedSymbol``，不新造种子类型。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import cast

from codepilot.agents.schemas import IssueAnalysis, IssueSemanticDraft, RuleEvidence
from codepilot.llm.base import LLMProvider
from codepilot.llm.structured import STRUCTURED_MAX_ATTEMPTS, complete_structured
from codepilot.llm.types import LLMRequest, Message
from codepilot.repo.models import Symbol
from codepilot.retrieval.fusion import pin_traceback
from codepilot.retrieval.query import build_query
from codepilot.schemas.common import TraceFrame
from codepilot.schemas.enums import IssueType
from codepilot.schemas.retrieval import RankedSymbol, RetrievalQuery

__all__ = ["ISSUE_ANALYSIS_PURPOSE", "IssueAnalyzer"]

ISSUE_ANALYSIS_PURPOSE = "issue_analysis"

#: 回退摘要的长度上限。它只是"没有 LLM 时也要有个能读的东西"，不是压缩正文。
_SUMMARY_LIMIT = 240

#: 写进 prompt 的规则关键词上限：再多只会挤占 Issue 原文本身的空间。
_MAX_EVIDENCE_KEYWORDS = 20

#: 复现小节标题的最长长度。超过它说明这行是正文而不是标题。
_HEADING_MAX_LENGTH = 60
_HEADING_MAX_WORDS = 4
#: 纯中文标题的字数上限。超过它就更像一句正文，而不是"复现步骤"这种小节名。
_CJK_HEADING_LIMIT = 8

_REPRO_HEADINGS = (
    "reproduce",
    "reproduction",
    "repro",
    "steps to reproduce",
    "steps",
    "how to reproduce",
    "复现",
    "重现",
)

_LIST_ITEM = re.compile(r"^[ \t]*(?:(?P<number>\d+)[.)]|[-*+])[ \t]+(?P<text>\S.*)$")
_HEADING = re.compile(r"^\s{0,3}(?:#{1,6}\s+\S|\*\*[^*\n]+\*\*[ \t]*[:：]?\s*$)")
_CODE_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")
_MARKDOWN_PREFIX = re.compile(r"^[#>*\-+\s]+")
_MARKDOWN_SUFFIX = re.compile(r"[\s:：*#]+$")
_ASCII_LETTER = re.compile(r"[a-z]")
_TRACEBACKISH = re.compile(r'^\s*(?:Traceback \(most recent|File "[^"]+", line|\^+$)')
_EXCEPTIONISH = re.compile(r"^\s*[\w.]+(?:Error|Exception|Warning):", re.MULTILINE)

_SPECIFIC_TYPE_CUES: tuple[tuple[IssueType, re.Pattern[str]], ...] = (
    (
        IssueType.REGRESSION,
        re.compile(
            r"\bregressions?\b|\bregressed\b|used to (?:work|be)|\bbroke\b|stopped working"
            r"|no longer works?|回退|回归|失效"
            r"|(?:以前|之前|原先|原来)(?:是|还|可以|能|没)"
            r"|(?:升级|更新|合并)后",
            re.IGNORECASE,
        ),
    ),
    (
        IssueType.PERFORMANCE,
        re.compile(
            r"\bperformance\b|\bslow(?:ly)?\b|\blatency\b|memory leak"
            r"|性能|(?:太|很|越来越)慢|卡顿|内存泄漏",
            re.IGNORECASE,
        ),
    ),
    (
        IssueType.FEATURE,
        re.compile(
            r"feature request|add(?:ing|ed)? support for|could you (?:add|support)|please add"
            r"|new feature|would be (?:great|nice|useful)|新功能|希望(?:增加|添加|支持)"
            r"|支持一下|建议(?:增加|支持|添加)",
            re.IGNORECASE,
        ),
    ),
)

_BUG_CUE = re.compile(
    r"\bbug(?:s)?\b|\bcrash(?:es|ed|ing)?\b|\bexception\b|\btraceback\b|\berrors?\b"
    r"|\bfails?\b|\bfailure\b|incorrect|\bwrong\b|报错|异常|崩溃|错误|失败|不正确|不对",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """你是自动修复流程中的 Issue 分析器，只负责语义理解，不负责取证。

你只做四件事：
1. 判断 issue_type（bug / feature / regression / performance / other）；
2. 用一句话写 summary；
3. 分别说明 expected_behavior（期望行为）与 observed_behavior（观察到的行为）；
4. 补充几个有助于检索的关键词。

硬性约束：
- 只能使用 Issue 原文与"已确认的技术事实"里出现的信息。不要补充你没看到的文件名、
  行号、函数名，也不要推断根因。
- 不要输出 traceback、文件路径、符号清单或错误信息。这些字段不在你的输出 Schema 里，
  由规则层从原文逐字抽取；你给出的同类内容会被直接丢弃。
- 判断不了的字段：可选字段填 null，summary 填空字符串。不要用"无""unknown"这类
  通用词占位。
- reproduction_steps 只在原文没有复现小节时补充，且必须逐字来自原文。
- 只输出一个 JSON 对象，不要解释，不要代码块。"""


class IssueAnalyzer:
    """把自然语言 Issue 变成 :class:`IssueAnalysis`。

    ``provider=None`` 时是纯规则分析器（确定性、零成本）；给出 provider 后按
    C-03 补语义，规则字段仍然不可被覆盖。测试用 ``FakeLLM`` 注入即可，
    不需要真实 API Key。
    """

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        purpose: str = ISSUE_ANALYSIS_PURPOSE,
        max_attempts: int = STRUCTURED_MAX_ATTEMPTS,
    ) -> None:
        self._provider = provider
        self.purpose = purpose
        self._max_attempts = max_attempts

    # ------------------------------------------------------------------ C-02

    def extract(
        self,
        issue: str,
        *,
        repo_files: Iterable[str] = (),
        repo_root: Path | None = None,
    ) -> RuleEvidence:
        """规则抽取。同一输入（同一 ``repo_files``）必然得到同一结果。

        ``repo_files`` 决定哪些 traceback 帧算"仓库内部帧"。不给它，所有帧都是
        ``in_repo=False``，C-04 也就没有任何种子——宁可不置顶，也不把第三方栈
        当成仓库代码。
        """
        query = build_query(issue, repo_files=repo_files, repo_root=repo_root)
        return RuleEvidence(
            mentioned_files=list(query.file_paths),
            mentioned_symbols=[w for w in query.identifiers if not _PY_TOKEN.search(w)],
            error_messages=list(query.error_messages),
            traceback_frames=list(query.parsed_frames),
            reproduction_steps=_reproduction_steps(issue),
            keywords=list(query.keywords),
            issue_type=_rule_issue_type(issue, query.parsed_frames),
            query=query,
        )

    # ------------------------------------------------------------------ C-03

    def analyze(
        self,
        issue: str,
        *,
        repo_files: Iterable[str] = (),
        repo_root: Path | None = None,
        evidence: RuleEvidence | None = None,
    ) -> IssueAnalysis:
        """规则抽取（+ 可选的 LLM 语义补充）后的结构化分析结果。

        ``evidence`` 让流水线复用一次 :meth:`extract` 的结果，不必把同一份 Issue
        解析两遍；它必须来自同一个 ``issue``，这条契约由调用方保证。
        """
        facts = (
            evidence
            if evidence is not None
            else self.extract(issue, repo_files=repo_files, repo_root=repo_root)
        )
        draft = None if self._provider is None else self._draft(self._provider, issue, facts)
        return _merge(facts, draft)

    def _draft(self, provider: LLMProvider, issue: str, facts: RuleEvidence) -> IssueSemanticDraft:
        request = LLMRequest(
            messages=[
                Message.system(_SYSTEM_PROMPT),
                Message.user(_user_prompt(issue, facts)),
            ],
            response_schema=IssueSemanticDraft,
            purpose=self.purpose,
        )
        response = complete_structured(provider, request, max_attempts=self._max_attempts)
        return cast("IssueSemanticDraft", response.parsed)

    # ------------------------------------------------------------------ C-04

    def retrieval_query(
        self,
        issue: str,
        *,
        repo_files: Iterable[str] = (),
        repo_root: Path | None = None,
    ) -> RetrievalQuery:
        """V-01 视角的查询对象，可直接交给 ``codepilot.retrieval``。"""
        return self.extract(issue, repo_files=repo_files, repo_root=repo_root).query

    def traceback_seeds(
        self, query: RetrievalQuery, symbols: Sequence[Symbol]
    ) -> list[RankedSymbol]:
        """仓库内 traceback 帧 → V-18 置顶种子（``pinned=True`` 的 ``RankedSymbol``）。

        身份匹配完全交给 Phase 2 的 ``pin_traceback``：路径必须是仓库内已索引文件，
        行号必须落在符号区间内，函数名必须与符号名或限定名对得上。第三方库路径、
        路径逃逸、行号越界、只同名而无身份匹配，都不成为种子。行号区间与
        ``reason`` 保留下来，能回答"它为什么被置顶"。
        """
        return pin_traceback(query, symbols)


_PY_TOKEN = re.compile(r"\.py\b", re.IGNORECASE)


def _reproduction_steps(issue: str) -> list[str]:
    """抽取复现步骤。

    先认"复现小节"（标题行命中且这行基本只有标题），在小节内收集列表项；没有
    小节时只认**从 1 开始连续编号**的有序列表。普通的 ``- 期望/实际`` 散列表不算
    步骤：那是另一类信息，冒充步骤会让后面的复现/定位环节读错。
    """
    lines = issue.splitlines()
    for index, line in enumerate(lines):
        if _is_repro_heading(line):
            return _items_in_section(lines[index + 1 :])
    return _numbered_sequence(lines)


def _is_repro_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _HEADING_MAX_LENGTH:
        return False
    cleaned = _MARKDOWN_SUFFIX.sub("", _MARKDOWN_PREFIX.sub("", stripped.lower()))
    if not cleaned or len(cleaned.split()) > _HEADING_MAX_WORDS:
        return False
    if not _ASCII_LETTER.search(cleaned) and len(cleaned) > _CJK_HEADING_LIMIT:
        return False
    return any(cleaned.startswith(phrase) for phrase in _REPRO_HEADINGS)


def _items_in_section(body: Sequence[str]) -> list[str]:
    """小节内的列表项。

    第一条列表项的缩进量决定"步骤"的层级：更深的是对某一步的补充说明，跳过；
    更浅的说明小节列表已经结束，停止。空行允许，代码块或下一个小节标题也停止。
    """
    steps: list[str] = []
    base_indent: int | None = None
    for line in body:
        if not line.strip():
            continue
        if _CODE_FENCE.match(line) or _HEADING.match(line):
            break
        match = _LIST_ITEM.match(line)
        if match is None:
            continue
        indent = len(line) - len(line.lstrip())
        if base_indent is None:
            base_indent = indent
        elif indent > base_indent:
            continue
        elif indent < base_indent:
            break
        steps.append(match["text"].strip())
    return list(dict.fromkeys(steps))


def _numbered_sequence(lines: Sequence[str]) -> list[str]:
    """编号从 1 起、逐条连续递增时，才把这组有序列表当作步骤。

    列表之间夹正文行允许（markdown 里很常见）；一旦编号不连续就停止——把
    changelog 或并列说明当成复现步骤，后面的环节会拿到一组毫不相关的动作。
    """
    found: list[tuple[int, str]] = []
    for line in lines:
        if _CODE_FENCE.match(line) or _HEADING.match(line):
            break
        match = _LIST_ITEM.match(line)
        if match is None or match["number"] is None:
            continue
        number = int(match["number"])
        if number != (found[-1][0] + 1 if found else 1):
            break
        found.append((number, match["text"].strip()))
    if len(found) < 2:
        return []
    return list(dict.fromkeys([text for _, text in found]))


def _rule_issue_type(issue: str, frames: Sequence[TraceFrame]) -> IssueType | None:
    """词面证据足够才表态，否则交回 ``None`` 让 LLM 判断。

    具体类别先于 ``bug``：一段带 traceback 的"升级后慢了三倍"是 performance，
    归成 bug 会让后续策略选错方向。有 traceback 帧或成形的异常行时，``bug``
    不需要词面线索——那已经是缺陷发生的直接证据。
    """
    for issue_type, pattern in _SPECIFIC_TYPE_CUES:
        if pattern.search(issue):
            return issue_type
    if frames or _EXCEPTIONISH.search(issue):
        return IssueType.BUG
    return IssueType.BUG if _BUG_CUE.search(issue) else None


def _user_prompt(issue: str, facts: RuleEvidence) -> str:
    lines = [
        "已确认的技术事实（规则层从原文逐字抽取，供你理解语义；不需要你重复，也不允许你修改）：",
        f"- 文件: {_join(facts.mentioned_files)}",
        f"- 标识符: {_join(facts.mentioned_symbols)}",
        f"- 错误信息: {_join(facts.error_messages)}",
        f"- 关键词: {_join(facts.keywords[:_MAX_EVIDENCE_KEYWORDS])}",
        f"- traceback 帧数: {len(facts.traceback_frames)}",
        "",
        "Issue 原文：",
        issue.strip() or "（空）",
    ]
    return "\n".join(lines)


def _join(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "（无）"


def _merge(facts: RuleEvidence, draft: IssueSemanticDraft | None) -> IssueAnalysis:
    """合并规则证据与 LLM 语义。可验证字段只取规则值，语义字段只取模型值。"""
    resolved = draft if draft is not None else IssueSemanticDraft()
    steps = list(facts.reproduction_steps) or list(resolved.reproduction_steps)
    return IssueAnalysis(
        issue_type=resolved.issue_type or facts.issue_type or IssueType.OTHER,
        summary=_text(resolved.summary) or _fallback_summary(facts),
        expected_behavior=_text(resolved.expected_behavior),
        observed_behavior=_text(resolved.observed_behavior),
        reproduction_steps=steps,
        mentioned_files=list(facts.mentioned_files),
        mentioned_symbols=list(facts.mentioned_symbols),
        error_messages=list(facts.error_messages),
        traceback_frames=list(facts.traceback_frames),
        keywords=list(dict.fromkeys([*facts.keywords, *resolved.keywords])),
    )


def _text(value: str | None) -> str | None:
    """把可选字符串收敛成"有内容"或 ``None``。

    空串和纯空白对检索没有信息量，留着只会让"模型说了点什么"看起来成立；
    归一成 ``None`` 后下游能看出这一格其实没有内容。
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _fallback_summary(facts: RuleEvidence) -> str:
    """没有 LLM（或 LLM 没给摘要）时，从原文挑一句最像问题描述的话。

    跳过 traceback、异常行与代码块：那些内容已经在 ``traceback_frames`` /
    ``error_messages`` 里，摘要再抄一遍只会挤掉真正的人类描述。
    """
    prose: list[str] = []
    in_fence = False
    for line in facts.query.raw_issue.splitlines():
        if _CODE_FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip() or _non_prose(line):
            continue
        cleaned = _MARKDOWN_PREFIX.sub("", line.strip())
        if cleaned:
            prose.append(cleaned)
        if len(prose) == 2:
            break
    if prose:
        return " ".join(prose)[:_SUMMARY_LIMIT]
    if facts.error_messages:
        return facts.error_messages[0].strip()[:_SUMMARY_LIMIT]
    if facts.traceback_frames:
        frame = facts.traceback_frames[0]
        return f"{frame.file}:{frame.line} in {frame.function}"[:_SUMMARY_LIMIT]
    return ""


def _non_prose(line: str) -> bool:
    return bool(_CODE_FENCE.match(line) or _TRACEBACKISH.match(line) or _EXCEPTIONISH.match(line))
