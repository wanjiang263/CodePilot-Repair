"""``LLMProvider`` 协议（02 §5.1，L-01）。

刻意定得很小：只有 ``complete`` 和 ``close``。这条边界的价值在于
**装饰器可以任意叠加**——录制、回放、用量统计、缓存都是包装器，
它们只需要转发 ``complete``，不需要知道被包的 provider 是哪一家。

一旦往 Protocol 里加 ``spec``、``provider_name`` 之类的东西，所有包装器
都得写一遍转发属性，加第 5 个包装器时就会有人漏掉其中一个。
需要这些信息的场合（成本计算、日志）改为让 provider 在 ``LLMResponse``
里回报，或由 registry 在构造时单独持有。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from codepilot.llm.types import LLMRequest, LLMResponse

__all__ = ["LLMProvider"]


@runtime_checkable
class LLMProvider(Protocol):
    """一次 LLM 调用。"""

    def complete(self, request: LLMRequest) -> LLMResponse:
        """执行一次调用。

        Raises:
            LLMError 的各个子类。上层按异常类型分流，不按 provider 分流。
        """
        ...

    def close(self) -> None:
        """释放底层连接。包装器必须转发给自己的被包装者。"""
        ...
