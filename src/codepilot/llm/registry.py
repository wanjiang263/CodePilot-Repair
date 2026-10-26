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

from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from codepilot.llm.anthropic_messages import AnthropicMessagesProvider
from codepilot.llm.base import LLMProvider
from codepilot.llm.bedrock_converse import BedrockConverseProvider
from codepilot.llm.calls import CallRecorder, LLMRunContext
from codepilot.llm.config import LLMConfig, ModelSpec, ProviderSpec
from codepilot.llm.errors import ProviderNotConfiguredError
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.record import RecordingLLM
from codepilot.llm.replay import ReplayLLM
from codepilot.llm.retry import ConcurrencyLimiter, RetryPolicy
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.net import build_http_client
from codepilot.observability.events import EventSink
from codepilot.schemas.usage import Usage

__all__ = ["LLMMode", "LLMRegistry", "transport_provider"]


def transport_provider(chain: LLMProvider) -> Any:
    """剥开装饰器，拿到真正发 HTTP 的那一层。

    装饰器链（用量统计 → 录制 → provider）是刻意对上层透明的，但有两件事只有
    最内层知道：**一共发了几次请求**、以及**有没有请求已发出但结果未知**。
    冒烟脚本要靠这两个数字如实报告费用确定性，所以这里显式地把它取出来，
    而不是给 Protocol 加一个所有装饰器都要转发的属性。
    """
    current: Any = chain
    seen: set[int] = set()
    while True:
        if isinstance(current, OpenAICompatibleProvider):
            return current
        inner = getattr(current, "inner", None)
        if inner is None or id(current) in seen:
            raise TypeError(f"provider 链里没有 OpenAICompatibleProvider: {type(current).__name__}")
        seen.add(id(current))
        current = inner


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
        budget_currency: str | None = None,
        policy: RetryPolicy | None = None,
        client_factory: Any = None,
        bedrock_client_factory: Any = None,
        recorder: CallRecorder | None = None,
    ) -> None:
        """
        Args:
            calls_path: ``record`` 模式的写入路径，或 ``replay`` 模式的读取路径。
                ``record`` 模式允许先不传：真实 benchmark 里 provider 在派发前就装好了，
                而 ``llm_calls.jsonl`` 要落在每个 run 自己的 artifact 目录里，那时才有路径。
                未绑定就发请求会显式报错，而不是静默丢掉一份录制。
            client_factory: 测试注入点，签名 ``(ProviderSpec, api_key) -> httpx.Client``。
                生产环境不传，用默认实现。
            bedrock_client_factory: 测试注入点，返回一个 boto3 ``bedrock-runtime`` 形状的
                客户端。生产环境由 AWS SDK 凭据链构造。
            recorder: 逐请求元数据记录器（``llm_requests.jsonl``）。同一 run 内所有角色
                共用一个，序号才是全局单调的。
        """
        self.config = config
        self.mode = mode
        self.usage = usage if usage is not None else Usage()
        self.sink = sink
        self.calls_path = calls_path
        # 币种以运行参数为准，其次是配置里的 budget_currency。两处都能声明是为了
        # "同一个 provider 配置跑在不同币种预算下"；都不给时才落到 CNY 默认值。
        self.budget_currency = budget_currency or config.budget_currency
        self.policy = policy or RetryPolicy()
        self._client_factory = client_factory
        self._bedrock_client_factory = bedrock_client_factory
        self.recorder = recorder

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

    def bind_run(
        self,
        *,
        run_id: str,
        task_id: str,
        sink: EventSink | None = None,
        recorder: CallRecorder | None = None,
        calls_path: Path | None = None,
        requests_path: Path | None = None,
        context: LLMRunContext | None = None,
    ) -> LLMRunContext:
        """把 run 身份、事件流与落盘路径绑到已构造的 provider 链上。

        两个路径是两件事，绝不能合成一个：``calls_path`` 是
        :class:`~codepilot.llm.record.RecordingLLM` 的**完整请求体**录制（只在显式
        record 模式写），``requests_path`` 是默认就写的**逐请求元数据**。把它们写到
        同一个文件里，会让一份元数据日志看起来像一次带密钥风险的完整录制。

        返回这次运行的 :class:`LLMRunContext`（节点进入时更新 node/iteration）。
        """
        run_context = context or LLMRunContext(run_id=run_id, task_id=task_id)
        run_context.run_id = run_id
        run_context.task_id = task_id
        active_recorder = recorder or self.recorder
        if active_recorder is None:
            active_recorder = CallRecorder(path=requests_path, context=run_context)
        else:
            active_recorder.bind(
                run_context, requests_path if requests_path is not None else active_recorder.path
            )
        self.sink = sink if sink is not None else self.sink
        self.recorder = active_recorder
        if calls_path is not None:
            self.calls_path = calls_path
        for provider in self._cache.values():
            _bind_provider_chain(
                provider, sink=self.sink, recorder=active_recorder, calls_path=calls_path
            )
        return run_context

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

            if provider_spec.protocol == "bedrock_converse":
                inner = BedrockConverseProvider(
                    spec=spec,
                    provider=provider_spec,
                    client=(
                        self._bedrock_client_factory(provider_spec)
                        if self._bedrock_client_factory is not None
                        else None
                    ),
                    exchange_rates=self.config.exchange_rates,
                    budget_currency=self.budget_currency,
                )
            else:
                api_key = _api_key_for(provider_spec)
                client = (
                    self._client_factory(provider_spec, api_key)
                    if self._client_factory is not None
                    else _default_client(provider_spec, api_key)
                )
                if provider_spec.protocol == "anthropic_messages":
                    inner = AnthropicMessagesProvider(
                        spec=spec,
                        provider=provider_spec,
                        api_key=api_key,
                        client=client,
                        policy=self._policy_for(provider_spec),
                        limiter=self._limiter_for(
                            provider_spec.name, provider_spec.max_concurrency
                        ),
                        exchange_rates=self.config.exchange_rates,
                        budget_currency=self.budget_currency,
                        allowed_providers=self.config.allowed_providers,
                    )
                else:
                    inner = OpenAICompatibleProvider(
                        spec=spec,
                        provider=provider_spec,
                        api_key=api_key,
                        client=client,
                        policy=self._policy_for(provider_spec),
                        limiter=self._limiter_for(
                            provider_spec.name, provider_spec.max_concurrency
                        ),
                        exchange_rates=self.config.exchange_rates,
                        budget_currency=self.budget_currency,
                        allowed_providers=self.config.allowed_providers,
                    )

            if self.mode == LLMMode.RECORD:
                inner = RecordingLLM(inner, self._calls_path_or_pending())

        # 统计层放在最外面：录制与回放本身不产生 token，
        # 但无论哪条路径，用量的归属都必须一致。
        return UsageTrackingProvider(
            inner,
            usage=self.usage,
            sink=self.sink,
            node=role,
            role=role,
            provider=provider_spec.name,
            recorder=self.recorder,
        )

    def _replay_provider(self) -> ReplayLLM:
        if self._replay is None:
            self._replay = ReplayLLM(self._require_calls_path())
        return self._replay

    def _policy_for(self, provider_spec: ProviderSpec) -> RetryPolicy:
        """provider 配了 ``max_retries`` 就覆盖全局策略的尝试次数。

        只改次数，退避基数与抖动仍来自调用方传入的策略——它们是本地调参，
        而重试次数是服务商配额决定的。
        """
        if provider_spec.max_retries is None:
            return self.policy
        return replace(self.policy, max_attempts=provider_spec.max_retries + 1)

    def _require_calls_path(self) -> Path:
        if self.calls_path is None:
            raise ValueError(f"{self.mode} 模式必须提供 calls_path")
        return self.calls_path

    def _calls_path_or_pending(self) -> Path | None:
        """``record`` 模式允许晚绑定；``replay`` 模式必须当场有文件可读。"""
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


def _bind_provider_chain(
    provider: Any,
    *,
    sink: EventSink | None,
    recorder: CallRecorder,
    calls_path: Path | None,
) -> None:
    """沿 ``inner`` 链晚绑定：外层记事件与逐请求用量，内层决定录制落点。"""
    if hasattr(provider, "bind_sink"):
        provider.bind_sink(sink, recorder=recorder)
    current = getattr(provider, "inner", None)
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, RecordingLLM) and calls_path is not None:
            current.set_path(calls_path)
        current = getattr(current, "inner", None)


def _api_key_for(provider_spec: ProviderSpec) -> str:
    key = provider_spec.api_key()
    if key is None:
        names = [provider_spec.api_key_env]
        if provider_spec.api_key_env_fallback:
            names.append(provider_spec.api_key_env_fallback)
        raise ProviderNotConfiguredError(
            f"环境变量 {' 或 '.join(names)} 均未设置（provider={provider_spec.name}）。"
            "请写入 .env 文件——.env 不会被提交，也绝不要把密钥写进 configs/。",
            provider=provider_spec.name,
        )
    return key


def _default_client(provider_spec: Any, api_key: str) -> httpx.Client:
    """只配 base_url 与超时。

    鉴权头由 provider 逐次请求带上（见 ``OpenAICompatibleProvider``），
    写在这里会让人以为"注入 client 就等于配好了鉴权"。
    """
    del api_key
    return build_http_client(
        base_url=provider_spec.base_url,
        timeout=httpx.Timeout(
            provider_spec.timeout_seconds,
            connect=provider_spec.connect_timeout_seconds or provider_spec.timeout_seconds,
            write=provider_spec.write_timeout_seconds or provider_spec.timeout_seconds,
            pool=provider_spec.pool_timeout_seconds or provider_spec.timeout_seconds,
        ),
        trust_env=provider_spec.trust_env,
    )
