"""L-13 的证明：角色 → provider 的装配；回放模式不需要密钥；占位符不放行。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from codepilot.llm.config import KNOWN_ROLES, LLMConfig
from codepilot.llm.errors import (
    ProviderError,
    ProviderNotAllowedError,
    ProviderNotConfiguredError,
)
from codepilot.llm.registry import LLMMode, LLMRegistry
from codepilot.llm.types import LLMRequest, Message

REQUEST = LLMRequest(messages=[Message.user("你好")], purpose="plan")


def _response(content: str = "回答") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    )


def _factory(transport: httpx.MockTransport) -> Any:
    def factory(provider_spec: Any, api_key: str) -> httpx.Client:
        return httpx.Client(transport=transport, base_url=provider_spec.base_url)

    return factory


def _config(**overrides: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "providers": {
            "p1": {"name": "p1", "base_url": "https://fake.test/v1", "api_key_env": "REG_KEY"}
        },
        "models": {
            "m1": {"alias": "m1", "provider": "p1", "model": "real-model-id"},
        },
        "roles": dict.fromkeys(KNOWN_ROLES, "m1"),
        "exchange_rates": {"CNY": 1.0},
    }
    base.update(overrides)
    return LLMConfig.model_validate(base)


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REG_KEY", "sk-test")


# --------------------------------------------------------------- 装配


def test_for_role_returns_working_provider(key: None) -> None:
    transport = httpx.MockTransport(lambda request: _response("已定位"))
    registry = LLMRegistry(_config(), client_factory=_factory(transport))

    assert registry.for_role("localizer").complete(REQUEST).content == "已定位"
    registry.close()


def test_provider_is_cached_per_role(key: None) -> None:
    """每次调用都新建 client 会反复做 TLS 握手，在 benchmark 里是可观开销。"""
    transport = httpx.MockTransport(lambda request: _response())
    registry = LLMRegistry(_config(), client_factory=_factory(transport))
    assert registry.for_role("coder") is registry.for_role("coder")
    registry.close()


def test_close_releases_every_provider(key: None) -> None:
    transport = httpx.MockTransport(lambda request: _response())
    registry = LLMRegistry(_config(), client_factory=_factory(transport))
    registry.for_role("coder")
    registry.close()
    assert registry._cache == {}


def test_context_manager_closes(key: None) -> None:
    transport = httpx.MockTransport(lambda request: _response())
    with LLMRegistry(_config(), client_factory=_factory(transport)) as registry:
        registry.for_role("coder")
    assert registry._cache == {}


# --------------------------------------------------------------- 配置问题


def test_unknown_role_raises(key: None) -> None:
    registry = LLMRegistry(
        _config(roles={}), client_factory=_factory(httpx.MockTransport(_response))
    )
    with pytest.raises(ProviderNotConfiguredError):
        registry.for_role("coder")


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REG_KEY", raising=False)
    registry = LLMRegistry(
        _config(), client_factory=_factory(httpx.MockTransport(lambda r: _response()))
    )
    with pytest.raises(ProviderNotConfiguredError, match="REG_KEY"):
        registry.for_role("coder")


def test_placeholder_model_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """占位符必须是硬错误：带着它跑 benchmark 只会得到一批无意义的失败。"""
    monkeypatch.setenv("REG_KEY", "sk-test")
    config = _config(models={"m1": {"alias": "m1", "provider": "p1", "model": "<官方模型 ID>"}})
    registry = LLMRegistry(
        config, client_factory=_factory(httpx.MockTransport(lambda r: _response()))
    )
    with pytest.raises(ProviderNotConfiguredError, match="占位符"):
        registry.for_role("coder")


def test_allowlist_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REG_KEY", "sk-test")
    registry = LLMRegistry(
        _config(allowed_providers=["other"]),
        client_factory=_factory(httpx.MockTransport(lambda r: _response())),
    )
    with pytest.raises(ProviderNotAllowedError):
        registry.for_role("coder")


# --------------------------------------------------------------- 用量统计


def test_usage_is_accumulated(key: None) -> None:
    transport = httpx.MockTransport(lambda request: _response())
    registry = LLMRegistry(_config(), client_factory=_factory(transport))
    registry.for_role("coder").complete(REQUEST)
    registry.for_role("coder").complete(REQUEST)

    assert registry.usage.llm_calls == 2
    assert registry.usage.input_tokens == 200
    assert registry.usage.output_tokens == 40
    registry.close()


def test_usage_is_shared_across_roles(key: None) -> None:
    """一次运行的总花费必须是全局的，不能按 Agent 各记一份然后漏掉。"""
    transport = httpx.MockTransport(lambda request: _response())
    registry = LLMRegistry(_config(), client_factory=_factory(transport))
    registry.for_role("coder").complete(REQUEST)
    registry.for_role("planner").complete(REQUEST)
    assert registry.usage.llm_calls == 2
    registry.close()


def test_usage_records_failed_calls(key: None) -> None:
    """失败的调用同样消耗了 token，不计入会让预算失控。"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": "invalid temperature"})
    )
    registry = LLMRegistry(_config(), client_factory=_factory(transport))
    with pytest.raises(ProviderError):
        registry.for_role("coder").complete(REQUEST)
    assert registry.usage.llm_calls == 1
    registry.close()


# --------------------------------------------------------------- 录制与回放


def test_record_then_replay_roundtrip(key: None, tmp_path: Path) -> None:
    path = tmp_path / "llm_calls.jsonl"
    transport = httpx.MockTransport(lambda request: _response("原始结果"))

    with LLMRegistry(
        _config(), mode=LLMMode.RECORD, calls_path=path, client_factory=_factory(transport)
    ) as registry:
        assert registry.for_role("coder").complete(REQUEST).content == "原始结果"

    assert path.exists()

    # 回放不注入 client_factory：这条路径根本不该碰网络。
    with LLMRegistry(_config(), mode=LLMMode.REPLAY, calls_path=path) as replay:
        assert replay.for_role("coder").complete(REQUEST).content == "原始结果"


def test_replay_mode_needs_no_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """离线回放要能在没有任何密钥的机器上跑，否则 CI 上根本没法验证逻辑。"""
    monkeypatch.setenv("REG_KEY", "sk-test")
    path = tmp_path / "llm_calls.jsonl"
    transport = httpx.MockTransport(lambda request: _response("原始结果"))
    with LLMRegistry(
        _config(), mode=LLMMode.RECORD, calls_path=path, client_factory=_factory(transport)
    ) as registry:
        registry.for_role("coder").complete(REQUEST)

    monkeypatch.delenv("REG_KEY", raising=False)

    with LLMRegistry(_config(), mode=LLMMode.REPLAY, calls_path=path) as replay:
        assert replay.for_role("coder").complete(REQUEST).content == "原始结果"


def test_replay_mode_ignores_placeholder_model(key: None, tmp_path: Path) -> None:
    """回放不需要真实模型 ID——它只读文件。"""
    path = tmp_path / "llm_calls.jsonl"
    transport = httpx.MockTransport(lambda request: _response("原始结果"))
    with LLMRegistry(
        _config(), mode=LLMMode.RECORD, calls_path=path, client_factory=_factory(transport)
    ) as registry:
        registry.for_role("coder").complete(REQUEST)

    config = _config(models={"m1": {"alias": "m1", "provider": "p1", "model": "<官方模型 ID>"}})
    with LLMRegistry(config, mode=LLMMode.REPLAY, calls_path=path) as replay:
        assert replay.for_role("coder").complete(REQUEST).content == "原始结果"


def test_record_mode_requires_a_path(key: None) -> None:
    with (
        LLMRegistry(_config(), mode=LLMMode.RECORD) as registry,
        pytest.raises(ValueError, match="calls_path"),
    ):
        registry.for_role("coder")


def test_replay_mode_requires_a_path() -> None:
    with (
        LLMRegistry(_config(), mode=LLMMode.REPLAY) as registry,
        pytest.raises(ValueError, match="calls_path"),
    ):
        registry.for_role("coder")


# --------------------------------------------------------------- 限流共享


def test_same_provider_shares_one_limiter(key: None) -> None:
    """限流是账号级别的。按模型各建一个会让实际并发变成 N 倍。"""
    config = _config(
        models={
            "m1": {"alias": "m1", "provider": "p1", "model": "real-1"},
            "m2": {"alias": "m2", "provider": "p1", "model": "real-2"},
        },
        roles={"coder": "m1", "planner": "m2"},
    )
    transport = httpx.MockTransport(lambda request: _response())
    registry = LLMRegistry(config, client_factory=_factory(transport))
    registry.for_role("coder")
    registry.for_role("planner")
    assert len(registry._limiters) == 1
    registry.close()
