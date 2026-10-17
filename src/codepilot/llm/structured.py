"""结构化输出（02 §5.3，L-09）。

三次尝试的流程：

1. 首轮调用（支持 JSON 模式的模型会用上 ``response_format``）。
2. 校验失败时，把**上次输出原文 + 具体校验错误**一起发回去重试。
   这一步是关键：只说"请重新输出合法 JSON"模型会原样再错一遍；
   告诉它"字段 ``line`` 应该是 int，你给了 'abc'"，它才改得对。
3. 仍失败则抛 ``StructuredOutputError``，带上最后一次的原文。

失败时抛异常而不是返回 ``None``：``None`` 会被调用方误当成"模型没意见"，
从而在没有任何诊断信息的情况下静默降级。

``semantic_validator`` 是 Harness Hardening 加的第二阶段：JSON 与 Pydantic 只保证
"形状对"，而"字段 ``tests_to_run`` 里写了一个还不存在的测试"形状完全合法、语义上
不可执行。以前这类校验在 ``complete_structured`` **之后**才跑，于是模型哪怕只错一个
前缀，也已经花掉了纠正预算之外的所有机会，并且被节点边界归因成 Harness 崩溃。
把语义校验放进同一个有界纠正环里，才能"再问一次"而不是"直接崩"。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from codepilot.errors import AgentFailureError
from codepilot.llm.base import LLMProvider
from codepilot.llm.errors import StructuredOutputError
from codepilot.llm.types import LLMRequest, LLMResponse, Message
from codepilot.observability.redaction import redact_text

__all__ = ["STRUCTURED_MAX_ATTEMPTS", "complete_structured", "extract_json"]

STRUCTURED_MAX_ATTEMPTS = 3

TModel = TypeVar("TModel", bound=BaseModel)

#: 回灌给模型的上一轮输出上限。原文里可能含源码片段，超长既不必要也不该反复计费。
_PREVIOUS_LIMIT = 2000

_CORRECTION_PROMPT = """\
你上一次的输出不符合要求的 JSON Schema。

校验错误：
{error}

你上一次的输出：
{previous}

请重新输出。只输出一个 JSON 对象，不要解释，不要代码块。"""


def extract_json(text: str) -> Any:
    """从模型输出里抠出 JSON。

    模型经常会包一层 ``` 代码块，或在 JSON 前后写一句"好的，这是结果："。
    这些都不该让解析失败——它们的语义是明确的。

    解析不出来时抛 ``ValueError``，由调用方转成一次重试。
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("输出为空")

    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        # 去掉可能的语言标注行（```json）。
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        stripped = stripped.rstrip("`").strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 退一步：取最外层的花括号或方括号内容。
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise ValueError(f"输出中找不到合法 JSON（前 200 字符：{stripped[:200]!r}）")


def complete_structured(
    provider: LLMProvider,
    request: LLMRequest,
    *,
    max_attempts: int = STRUCTURED_MAX_ATTEMPTS,
    on_attempt: Callable[[int, str | None], None] | None = None,
    semantic_validator: Callable[[Any], Any] | None = None,
) -> LLMResponse:
    """调用并把 ``content`` 解析成 ``request.response_schema``。

    返回的 ``LLMResponse.parsed`` 是校验过的实例。

    Args:
        semantic_validator: 可选的第二阶段（业务语义）校验。它对**已通过 Pydantic
            校验**的模型调用，抛 :class:`~codepilot.errors.AgentFailureError` 即转入
            同一轮的纠正重试；返回 ``None`` 视为通过。它**不得**修改对象，只做校验。

    Raises:
        ValueError: 请求没有指定 ``response_schema``。
        StructuredOutputError: 所有尝试都失败。
    """
    schema_cls = request.response_schema
    if schema_cls is None:
        raise ValueError("complete_structured 需要一个带 response_schema 的请求")

    messages: list[Message] = list(request.messages)
    last_error: str | None = None
    last_content: str | None = None
    last_stage = "schema"

    for attempt in range(1, max_attempts + 1):
        current = request.model_copy(
            update={"messages": messages, "attempt": attempt, "correction": attempt > 1}
        )
        response = provider.complete(current)
        last_content = response.content

        if on_attempt is not None:
            on_attempt(attempt, response.content)

        try:
            if response.content is None:
                raise ValueError("模型没有返回任何内容")
            data = extract_json(response.content)
            parsed = schema_cls.model_validate(data)
        except (ValueError, ValidationError) as exc:
            last_stage = "schema"
            last_error = _format_error(exc)
        else:
            if semantic_validator is None:
                return response.model_copy(update={"parsed": parsed})
            try:
                semantic_validator(parsed)
            except AgentFailureError as exc:
                if not exc.retryable:
                    # 权限拒绝、服务商故障、程序 bug 都不是"再问一次模型"能解决的。
                    # 让它们原样冒到节点边界去归因，而不是被伪装成一次纠正机会。
                    raise
                last_stage = "semantic"
                last_error = _format_semantic_error(exc)
            else:
                return response.model_copy(update={"parsed": parsed})

        messages = [
            *messages,
            Message.assistant(response.content or ""),
            Message.user(_correction(last_error or "", last_stage, last_content)),
        ]

    raise StructuredOutputError(
        f"结构化输出连续 {max_attempts} 次失败（最后一次是{last_stage}校验）",
        attempts=max_attempts,
        last_error=last_error,
        last_content=last_content,
        model_alias=request.purpose,
        last_stage=last_stage,
    )


def _correction(error: str, stage: str, previous: str | None) -> str:
    """纠正提示：语义错误用错误自己带的可执行说明，Schema 错误沿用模板。"""
    if stage == "semantic" and "\n" in error:
        # 语义错误对象已经把"该改哪个字段、改成什么"写好了；再套一层模板只会稀释它。
        return redact_text(error)[: _PREVIOUS_LIMIT * 2]
    return redact_text(
        _CORRECTION_PROMPT.format(
            error=error, previous=redact_text(previous or "")[:_PREVIOUS_LIMIT]
        )
    )


def _format_semantic_error(exc: Exception) -> str:
    """语义校验异常的文本形态。"""
    prompt = getattr(exc, "correction_prompt", None)
    if callable(prompt):
        return str(prompt())
    return _format_error(exc)


def _format_error(exc: Exception) -> str:
    """把校验错误压成模型读得懂的紧凑文本。

    ``ValidationError`` 的默认 ``str()`` 带大段文件位置与文档链接，
    既费 token 又容易让模型抓错重点。
    """
    if isinstance(exc, ValidationError):
        parts: list[str] = []
        for error in exc.errors()[:10]:
            location = ".".join(str(p) for p in error.get("loc", ())) or "<根>"
            parts.append(f"- 字段 {location}: {error.get('msg')}")
        return "\n".join(parts) if parts else str(exc)
    return str(exc)
