"""录制 LLM 调用（02 §12，L-12）。

录下来的 ``llm_calls.jsonl`` 有两个用途，第二个才是真正重要的：

1. 排查"模型当时到底收到了什么"。
2. **让整个流程可以离线重放**。Benchmark 的回归测试不可能每次都真调接口：
   那样既贵又不可复现。有了录制文件，CI 上跑的是同一批输入，
   得到的是同一批输出。

失败调用也会记录（``error`` 字段非空）。只录成功会让回放遇到失败分支时
直接断掉，而失败分支恰恰是最需要回归的部分。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from codepilot.clock import utcnow
from codepilot.llm.base import LLMProvider
from codepilot.llm.errors import LLMError
from codepilot.llm.types import LLMRequest, LLMResponse, ToolCall, request_fingerprint
from codepilot.observability.redaction import redact

__all__ = ["CallRecord", "RecordingLLM", "request_to_jsonable", "response_to_jsonable"]


def request_to_jsonable(request: LLMRequest) -> dict[str, Any]:
    """把请求转成可 JSON 序列化的形式。

    ``response_schema`` 是一个**类型对象**，不能序列化，回放时也不需要它
    （指纹里已经包含了它的 Schema）。这里只留下类名，纯粹为了让人能读懂。
    """
    return {
        "purpose": request.purpose,
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "schema_name": (request.response_schema.__name__ if request.response_schema else None),
        "tools": [t.name for t in request.tools] if request.tools else None,
        "messages": [
            {
                "role": m.role.value,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "name": m.name,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
                ],
            }
            for m in request.messages
        ],
    }


def response_to_jsonable(response: LLMResponse) -> dict[str, Any]:
    """把响应转成可 JSON 序列化的形式。

    ``parsed`` 与 ``raw`` 不进文件：``parsed`` 回放时会由
    ``complete_structured`` 重新校验得到，``raw`` 体积大且只在排查
    兼容性问题时才需要。
    """
    return {
        "content": response.content,
        "tool_calls": [
            {
                "id": tc.id,
                "name": tc.name,
                "arguments": tc.arguments,
                "raw_arguments": tc.raw_arguments,
                "arguments_error": tc.arguments_error,
            }
            for tc in response.tool_calls
        ],
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "cached_input_tokens": response.cached_input_tokens,
        "cost": response.cost,
        "cost_known": response.cost_known,
        "model_alias": response.model_alias,
        "latency_ms": response.latency_ms,
        "reasoning_content": response.reasoning_content,
    }


@dataclass(frozen=True, slots=True)
class CallRecord:
    """一行 ``llm_calls.jsonl``。"""

    seq: int
    timestamp: str
    fingerprint: str
    purpose: str
    model_alias: str
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "timestamp": self.timestamp,
                "fingerprint": self.fingerprint,
                "purpose": self.purpose,
                "model_alias": self.model_alias,
                "request": self.request,
                "response": self.response,
                "error": self.error,
                "error_type": self.error_type,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> CallRecord:
        data = json.loads(line)
        return cls(
            seq=data["seq"],
            timestamp=data["timestamp"],
            fingerprint=data["fingerprint"],
            purpose=data.get("purpose", ""),
            model_alias=data.get("model_alias", ""),
            request=data.get("request") or {},
            response=data.get("response"),
            error=data.get("error"),
            error_type=data.get("error_type"),
        )

    def to_response(self) -> LLMResponse:
        """重建 ``LLMResponse``。``parsed`` 为空，由结构化输出流程重新填充。"""
        if self.response is None:
            raise ValueError("该记录是一次失败调用，没有响应可重建")
        data = dict(self.response)
        data["tool_calls"] = [ToolCall(**tc) for tc in data.get("tool_calls") or []]
        return LLMResponse(**data)


class RecordingLLM:
    """把每次调用追加写入 JSONL 的装饰器。

    ``path`` 允许先为空再晚绑定（:meth:`set_path`）：真实 benchmark 的 provider 在
    派发前就装配好了，而录制文件必须落在每个 run 自己的 artifact 目录里。
    未绑定就发请求会显式报错——静默丢掉一份录制，等于事后没有任何证据可回放。
    """

    def __init__(
        self,
        inner: LLMProvider,
        path: Path | None = None,
        *,
        clock: Any = utcnow,
        redact_payloads: bool = True,
    ) -> None:
        self._inner = inner
        self._path = Path(path) if path is not None else None
        self._clock = clock
        self._redact = redact_payloads
        self._seq = (
            _count_lines(self._path) if self._path is not None and self._path.exists() else 0
        )
        self._closed = False

    def complete(self, request: LLMRequest) -> LLMResponse:
        target = self._path
        if target is None:
            # 还没绑定落点就**不发**这次请求：一次录不到、但要付钱的调用
            # 比一次直接失败的调用难排查得多。
            raise RuntimeError(
                "record 模式还没有绑定 llm_calls.jsonl 路径；"
                "调用 LLMRegistry.bind_run(calls_path=...) 后才能发请求。"
            )
        try:
            response = self._inner.complete(request)
        except Exception as exc:
            self._append(request, response=None, error=exc, timestamp=self._clock(), path=target)
            raise
        self._append(request, response=response, error=None, timestamp=self._clock(), path=target)
        return response

    def close(self) -> None:
        self._closed = True
        self._inner.close()

    @property
    def inner(self) -> LLMProvider:
        """被包装者；见 ``UsageTrackingProvider.inner`` 的说明。"""
        return self._inner

    @property
    def path(self) -> Path | None:
        return self._path

    def set_path(self, path: Path) -> None:
        """晚绑定落盘位置；已有计数按新文件重建。"""
        target = Path(path)
        self._path = target
        self._seq = _count_lines(target) if target.exists() else 0

    @property
    def recorded(self) -> int:
        return self._seq

    def _append(
        self,
        request: LLMRequest,
        *,
        response: LLMResponse | None,
        error: Exception | None,
        timestamp: datetime,
        path: Path | None = None,
    ) -> None:
        target = path if path is not None else self._path
        if target is None:
            raise RuntimeError(
                "record 模式还没有绑定 llm_calls.jsonl 路径；"
                "调用 LLMRegistry.bind_run(calls_path=...) 后才能写记录。"
            )
        self._seq += 1
        record = CallRecord(
            seq=self._seq,
            timestamp=timestamp.isoformat(),
            fingerprint=request_fingerprint(request),
            purpose=request.purpose,
            model_alias=response.model_alias if response else "",
            request=request_to_jsonable(request),
            response=response_to_jsonable(response) if response else None,
            error=str(error) if error else None,
            error_type=type(error).__name__ if error else None,
        )

        payload: dict[str, Any] = json.loads(record.to_json())
        if self._redact:
            # 02 §25：落盘前一律过脱敏。错误体里常有回显的请求头。
            payload = redact(payload)

        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _count_lines(path: Path) -> int:
    """已有文件的行数，用于恢复 ``seq``。

    只数行、不解析：解析整个文件会让启动开销随录制文件线性增长，
    而录制文件在长任务里可以到几十兆。
    """
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def error_from_record(record: CallRecord) -> LLMError:
    """把录制的失败还原成异常对象。

    只在 ``errors`` 模块里找同名类，找不到就退回 ``LLMError``——
    任意类名都不该能让回放凭空构造出一个对象。
    """
    from codepilot.llm import errors as errors_module

    message = record.error or "录制的失败调用"
    cls = getattr(errors_module, record.error_type or "", None)
    if isinstance(cls, type) and issubclass(cls, LLMError):
        return cls(message)
    return LLMError(message)
