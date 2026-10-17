"""逐请求 LLM 可观测性（Harness Hardening）。

``Usage`` 只回答"一共花了多少"。真实实验里要回答的是另外几个问题：

* 这 7 次调用分别是谁发的（角色）、在哪个图节点、第几轮修复？
* 哪一次是纠正重试，哪一次是服务商 HTTP 重试？
* 哪个 finish_reason 把输出砍断了（``length`` 与 ``stop`` 的含义完全不同）？
* 这笔费用是服务商回传的确定值，还是"没配单价所以记 0"？

首次真实 GLM 运行把这些问题全部问倒了：``events.jsonl`` 里 16 条事件的 ``usage``
全是 ``null``（注册表从来没拿到 EventSink），``by_model`` 只有一个聚合项，
``llm_calls.jsonl`` 因为 ``settings.record_llm`` 没接进真实执行路径而根本不存在。
于是"模型到底有没有被问过第二次"这种问题只能靠猜。

这里只写**元数据**：请求 id、角色、节点、轮次、用量、费用是否确定、错误类型、
工具调用条数。Prompt 正文、工具参数原值、``reasoning_content`` 都不在这里——
需要完整请求体时由显式开启的 :class:`~codepilot.llm.record.RecordingLLM`
（``settings.record_llm``）负责，它自带脱敏。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from codepilot.observability.redaction import redact

__all__ = ["CallRecorder", "LLMCallRecord", "LLMRunContext"]

#: 一次运行的逐请求记录文件名。与 ``llm_calls.jsonl``（完整请求体）刻意分开：
#: 前者默认就写、只含元数据；后者必须显式授权，且会带上请求内容。
LLM_REQUESTS_JSONL = "llm_requests.jsonl"


@dataclass(slots=True)
class LLMRunContext:
    """ "这次调用发生在哪儿"的当前视图，由图节点在进入时更新。

    刻意与 provider 分离：provider 在装配期就构造好了（那时还不知道 run_id），
    而角色/节点/轮次只有在节点执行的当下才有意义。一个 run 有一套 Deps、
    也就有一套上下文对象，并发跑多个任务时不会互相串账。
    """

    run_id: str = ""
    task_id: str = ""
    node: str | None = None
    iteration: int = 0


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """一行 ``llm_requests.jsonl``：一次**实际发出**的模型请求。"""

    call_id: str
    run_id: str
    task_id: str
    seq: int
    role: str
    node: str
    iteration: int
    purpose: str
    provider: str | None
    model: str
    attempt: int
    correction: bool
    status: str
    """``ok`` / ``error`` / ``uncertain``（请求已发出但响应没拿回来）。"""

    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    cost: float | None = None
    cost_known: bool = True
    currency: str = "CNY"
    error_type: str | None = None
    http_status: int | None = None
    tool_calls: int = 0
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CallRecorder:
    """把 :class:`LLMCallRecord` 追加进 JSONL，并分配 run 内的请求序号。"""

    path: Path | None = None
    context: LLMRunContext = field(default_factory=LLMRunContext)
    seq: int = 0
    _records: list[LLMCallRecord] = field(default_factory=list, repr=False)

    def bind(self, context: LLMRunContext, path: Path | None) -> None:
        """绑定 run 身份与落盘位置。

        已有文件的行数会续成起始序号：恢复进程重新从 1 开始编号，会让同一个
        ``llm_requests.jsonl`` 里出现两套 ``seq``，事后无法判断哪些请求真的发生过。
        """
        self.context = context
        self.path = path
        if path is not None and Path(path).exists() and self.seq == 0:
            with Path(path).open(encoding="utf-8") as handle:
                self.seq = sum(1 for line in handle if line.strip())

    def record(self, record: LLMCallRecord) -> LLMCallRecord:
        self.seq += 1
        written = LLMCallRecord(**{**record.as_dict(), "seq": self.seq})
        self._records.append(written)
        if self.path is not None:
            payload = redact(written.as_dict())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return written

    def history(self) -> tuple[LLMCallRecord, ...]:
        return tuple(self._records)

    def by_role(self) -> dict[str, list[LLMCallRecord]]:
        grouped: dict[str, list[LLMCallRecord]] = {}
        for item in self._records:
            grouped.setdefault(item.role, []).append(item)
        return grouped

    def summary(self) -> dict[str, Any]:
        """按角色聚合的请求条数与 token 数，用于与 :class:`Usage` 总账对账。"""
        out: dict[str, Any] = {}
        for role, items in sorted(self.by_role().items()):
            out[role] = {
                "requests": len(items),
                "corrections": sum(1 for item in items if item.correction),
                "uncertain": sum(1 for item in items if item.status == "uncertain"),
                "errors": sum(1 for item in items if item.status == "error"),
                "input_tokens": sum(item.input_tokens for item in items),
                "output_tokens": sum(item.output_tokens for item in items),
                "cached_tokens": sum(item.cached_tokens for item in items),
                "cost_confirmed": round(
                    sum(item.cost or 0.0 for item in items if item.cost_known), 8
                ),
                "cost_unknown_requests": sum(1 for item in items if not item.cost_known),
                "currency": next((item.currency for item in items), "CNY"),
            }
        return {
            "total_requests": len(self._records),
            "records_present": True,
            "by_role": out,
        }
