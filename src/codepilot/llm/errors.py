"""LLM 层异常（02 §5.3 错误映射表）。

每家服务商的错误码和限流方式都不一样，统一映射到这几个类型之后，
上层只需要处理五种情况，不需要知道用的是哪一家。

``body`` 里可能含有回显的请求头，**打印前必须过 redact()**。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ContextTooLongError",
    "LLMError",
    "ProviderError",
    "ProviderNotAllowedError",
    "ProviderNotConfiguredError",
    "RateLimitError",
    "ReplayMissError",
    "StructuredOutputError",
]


class LLMError(Exception):
    """所有 LLM 层异常的基类。"""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model_alias: str | None = None,
        status: int | None = None,
        body: Any = None,
        retry_after: float | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model_alias = model_alias
        self.status = status
        self.body = body
        # 服务端在 429 里给的等待秒数。重试策略会优先采纳它
        # （见 retry.delay_for），因为它反映的是真实的配额窗口。
        self.retry_after = retry_after
        # 请求已经离开本机、但响应没拿回来（超时、连接被重置）。服务端可能已经
        # 生成并计费。没有这个标记，一次"结果未知"的调用会在归因里被当成
        # 确定的服务商错误，费用也无法交代。
        self.uncertain = uncertain

    def __str__(self) -> str:
        parts = [self.message]
        if self.provider:
            parts.append(f"provider={self.provider}")
        if self.model_alias:
            parts.append(f"model={self.model_alias}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.uncertain:
            parts.append("结果未知（请求已发出，未收到响应）")
        return " | ".join(parts)


class RateLimitError(LLMError):
    """HTTP 429。可重试。"""


class ProviderError(LLMError):
    """HTTP 5xx 与其他服务端错误。可重试。"""


class ContextTooLongError(LLMError):
    """上下文超限。

    不重试——同样长度的请求再发一次结果一样。触发的是 Context Builder
    按 50% 预算重建一次（02 §9 / §5.3）。
    """


class StructuredOutputError(LLMError):
    """结构化输出重试耗尽。

    携带 ``last_error`` 与 ``last_content``，因为"模型返回了什么"是排查
    这类失败的唯一线索。

    ``last_stage`` 区分最后一步失败在哪一层：``schema`` 是 JSON/Pydantic 不合法，
    ``semantic`` 是形状对但业务语义不合法（例如把还没写出来的测试填进了
    ``tests_to_run``）。归因层需要这个差别——两者都是模型的错，但不是同一种错。
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        last_error: str | None = None,
        last_content: str | None = None,
        last_stage: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.attempts = attempts
        self.last_error = last_error
        self.last_content = last_content
        self.last_stage = last_stage


class ReplayMissError(LLMError):
    """回放时找不到对应请求。

    绝不静默回退到真实接口：那会让一次"离线回放"在无人察觉的情况下
    变成一次真实的、要花钱的、结果不可复现的调用。
    """

    def __init__(self, message: str, *, fingerprint: str = "", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.fingerprint = fingerprint


class ProviderNotConfiguredError(LLMError):
    """模型别名未在 ``configs/llm.yaml`` 中定义，或缺 API key。"""


class ProviderNotAllowedError(LLMError):
    """provider 不在 ``allowed_providers`` 白名单内（02 §5.3 数据外发）。

    这条检查的意义是：私有仓库的代码片段不能因为一次误配就被发到未授权的服务商。
    """
