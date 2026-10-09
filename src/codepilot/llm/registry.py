"""Provider 注册表（02 §5.3，L-13）。

Agent 拿到 provider 的唯一入口。它负责把"角色"翻译成"配好密钥、
限流、用量统计、录制/回放的 provider 对象"，并缓存起来——
每次调用都新建一个 ``httpx.Client`` 会反复做 TLS 握手，
在 benchmark 里这是可观的开销。

三种模式：

``live``   真实调用。需要 API key。
``record`` 真实调用 + 落盘，为之后的离线回放准备素材。
``replay`` 只读录制文件，**不需要 API key**。这条很重要：离线回放要能在
           一台没有任何密钥的机器上跑，否则 CI 上根本没法验证 agent 逻辑。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from codepilot.llm.base import LLMProvider
from codepilot.llm.config import LLMConfig, ModelSpec
from codepilot.llm.errors import ProviderNotConfiguredError
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.record import RecordingLLM
from codepilot.llm.replay import ReplayLLM
from codepilot.llm.retry import ConcurrencyLimiter, RetryPolicy
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.observability.events import EventSink
from codepilot.schemas.usage import Usage

__all__ = ["LLMMode", "LLMRegistry"]


class LLMMode(StrEnum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


class LLMRegistry:
    """按角色构造并缓存 ``LLMProvider``。"""

    def __init__(
        self,
        config: LLMConfig,
        *,
        mode: LLMMode = LLMMode.LIVE,
        usage: Usage | None = None,
        sink: EventSink | None = None,
        calls_path: Path | None = None,
        budget_currency: str = "CNY",
        policy: RetryPolicy | None = None,
        client_factory: Any = None,
    ) -> None:
        """
        Args:
            calls_path: ``record`` 模式的写入路径，或 ``replay`` 模式的读取路径。
            client_factory: 测试注入点，签名 ``(ProviderSpec, api_key) -> httpx.Client``。
                生产环境不传，用默认实现。
        """
        self.config = config
        self.mode = mode
        self.usage = usage if usage is not None else Usage()
        self.sink = sink
        self.calls_path = calls_path
        self.budget_currency = budget_currency
        self.policy = policy or RetryPolicy()
        self._client_factory = client_factory

        self._cache: dict[str, LLMProvider] = {}
        self._limiters: dict[str, ConcurrencyLimiter] = {}
        # replay 模式下不建真实客户端，因此不需要密钥。
        self._replay: ReplayLLM | None = None

    # ---------------------------------------------------------------- 构造

    def for_role(self, role: str) -> LLMProvider:
        """取某个 Agent 角色对应的 provider。"""
        cached = self._cache.get(role)
        if cached is not None:
            return cached

        spec = self._resolve(role)
        provider = self._build(spec, role)
        self._cache[role] = provider
        return provider

    def complete(self, role: str, request: Any) -> Any:
        """快捷方式：``registry.complete("coder", request)``。"""
        return self.for_role(role).complete(request)

    def close(self) -> None:
        for provider in self._cache.values():
            provider.close()
        self._cache.clear()

    def __enter__(self) -> LLMRegistry:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------- 内部

    def _resolve(self, role: str) -> ModelSpec:
        try:
            return self.config.resolve_role(role)
        except KeyError as exc:
            raise ProviderNotConfiguredError(str(exc)) from exc

    def _build(self, spec: ModelSpec, role: str) -> LLMProvider:
        provider_spec = self.config.providers.get(spec.provider)
        if provider_spec is None:
            raise ProviderNotConfiguredError(
                f"模型 {spec.alias!r} 指向了不存在的 provider {spec.provider!r}"
            )

        if self.mode == LLMMode.REPLAY:
            # 回放路径完全不碰网络，也就不需要密钥与 base_url。
            inner: LLMProvider = self._replay_provider()
        else:
            if not spec.model_id_configured:
                raise ProviderNotConfiguredError(
                    f"模型别名 {spec.alias!r} 的 model 字段仍是占位符。"
                    "请在 configs/llm.yaml 中填写官方模型 ID"
                    "（uv run codepilot config check 可查看待填项）。"
                )

            api_key = _api_key_for(provider_spec.name, provider_spec.api_key_env)
            client = (
                self._client_factory(provider_spec, api_key)
                if self._client_factory is not None
                else _default_client(provider_spec, api_key)
            )

            inner = OpenAICompatibleProvider(
                spec=spec,
                provider=provider_spec,
                api_key=api_key,
                client=client,
                policy=self.policy,
                limiter=self._limiter_for(provider_spec.name, provider_spec.max_concurrency),
                exchange_rates=self.config.exchange_rates,
                budget_currency=self.budget_currency,
                allowed_providers=self.config.allowed_providers,
            )

            if self.mode == LLMMode.RECORD:
                inner = RecordingLLM(inner, self._require_calls_path())

        # 统计层放在最外面：录制与回放本身不产生 token，
        # 但无论哪条路径，用量的归属都必须一致。
        return UsageTrackingProvider(
            inner,
            usage=self.usage,
            sink=self.sink,
            node=role,
        )

    def _replay_provider(self) -> ReplayLLM:
        if self._replay is None:
            self._replay = ReplayLLM(self._require_calls_path())
        return self._replay

    def _require_calls_path(self) -> Path:
        if self.calls_path is None:
            raise ValueError(f"{self.mode} 模式必须提供 calls_path")
        return self.calls_path

    def _limiter_for(self, provider_name: str, limit: int) -> ConcurrencyLimiter:
        """同一 provider 的多个模型共用限流器。

        限流是服务商账号级别的，按模型各建一个会让实际并发变成 N 倍。
        """
        limiter = self._limiters.get(provider_name)
        if limiter is None:
            limiter = ConcurrencyLimiter(limit)
            self._limiters[provider_name] = limiter
        return limiter


def _api_key_for(provider_name: str, env_name: str) -> str:
    from codepilot.config import read_secret_env

    key = read_secret_env(env_name)
    if key is None:
        raise ProviderNotConfiguredError(
            f"环境变量 {env_name} 未设置（provider={provider_name}）。"
            "请写入 .env 文件——.env 不会被提交，也绝不要把密钥写进 configs/。",
            provider=provider_name,
        )
    return key


def _default_client(provider_spec: Any, api_key: str) -> httpx.Client:
    """只配 base_url 与超时。

    鉴权头由 provider 逐次请求带上（见 ``OpenAICompatibleProvider``），
    写在这里会让人以为"注入 client 就等于配好了鉴权"。
    """
    del api_key
    return httpx.Client(
        base_url=provider_spec.base_url,
        timeout=provider_spec.timeout_seconds,
    )
