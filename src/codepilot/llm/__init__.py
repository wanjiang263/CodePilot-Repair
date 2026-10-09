"""LLM 调用层。

对外的边界只有三个东西：``LLMProvider`` 协议、``LLMRequest`` / ``LLMResponse``
两个类型、以及把角色翻译成 provider 的 ``LLMRegistry``。

**Agent 代码不许 import 这个包里的其他模块**，尤其不许 import
``openai_compatible`` —— 那意味着业务逻辑里会开始出现服务商特有的处理。
需要区分能力差异时，用 ``config.ModelSpec`` 上的能力字段。
"""

from __future__ import annotations

from codepilot.llm.base import LLMProvider
from codepilot.llm.config import (
    KNOWN_ROLES,
    ConfigReport,
    LLMConfig,
    ModelPrice,
    ModelSpec,
    ProviderSpec,
    load_llm_config,
)
from codepilot.llm.errors import (
    ContextTooLongError,
    LLMError,
    ProviderError,
    ProviderNotAllowedError,
    ProviderNotConfiguredError,
    RateLimitError,
    ReplayMissError,
    StructuredOutputError,
)
from codepilot.llm.fake import FakeLLM
from codepilot.llm.record import RecordingLLM
from codepilot.llm.registry import LLMMode, LLMRegistry
from codepilot.llm.replay import ReplayLLM
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.structured import complete_structured
from codepilot.llm.types import LLMRequest, LLMResponse, Message, ToolCall, ToolSpec

__all__ = [
    "KNOWN_ROLES",
    "ConfigReport",
    "ContextTooLongError",
    "FakeLLM",
    "LLMConfig",
    "LLMError",
    "LLMMode",
    "LLMProvider",
    "LLMRegistry",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "ModelPrice",
    "ModelSpec",
    "ProviderError",
    "ProviderNotAllowedError",
    "ProviderNotConfiguredError",
    "ProviderSpec",
    "RateLimitError",
    "RecordingLLM",
    "ReplayLLM",
    "ReplayMissError",
    "RetryPolicy",
    "StructuredOutputError",
    "ToolCall",
    "ToolSpec",
    "complete_structured",
    "load_llm_config",
]
