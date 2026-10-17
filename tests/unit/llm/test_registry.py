"""L-13 的证明：角色 → provider 的装配；回放模式不需要密钥；占位符不放行。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from codepilot.llm.calls import CallRecorder
from codepilot.llm.config import KNOWN_ROLES, LLMConfig
from codepilot.llm.errors import (
    ProviderError,
    ProviderNotAllowedError,
    ProviderNotConfiguredError,
)
from codepilot.llm.registry import LLMMode, LLMRegistry
from codepilot.llm.types import LLMRequest, Message
from codepilot.observability.events import EventSink

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


def test_record_mode_defers_the_calls_path_until_a_run_is_bound(key: None) -> None:
    """record 模式的落点可以晚绑定，但没绑定就**不许**发请求。

    provider 在派发前就装配好了，而 ``llm_calls.jsonl`` 必须落在每个 run 自己的
    artifact 目录里，所以构造期不能要求路径。反过来，若允许"先发了再说"，
    就会出现一次花了钱却没有任何录制结果的调用。
    """
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return _response("已定位")

    with LLMRegistry(
        _config(), mode=LLMMode.RECORD, client_factory=_factory(httpx.MockTransport(handler))
    ) as registry:
        provider = registry.for_role("coder")
        with pytest.raises(RuntimeError, match="还没有绑定"):
            provider.complete(REQUEST)
        assert sent == [], "未绑定落点时不得把请求发出去"


def test_bind_run_late_wires_recording_events_and_per_call_log(key: None, tmp_path: Path) -> None:
    """``bind_run`` 之后：录制落盘、事件带 usage、逐请求日志带角色与序号。"""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return _response("已定位")

    calls_path = tmp_path / "llm_calls.jsonl"
    requests_path = tmp_path / "llm_requests.jsonl"
    events_path = tmp_path / "events.jsonl"
    sink = EventSink(run_id="run-1", task_id="task-1", jsonl_path=events_path)
    recorder = CallRecorder()
    with LLMRegistry(
        _config(), mode=LLMMode.RECORD, client_factory=_factory(httpx.MockTransport(handler))
    ) as registry:
        registry.for_role("coder")
        registry.bind_run(
            run_id="run-1",
            task_id="task-1",
            sink=sink,
            calls_path=calls_path,
            requests_path=requests_path,
            recorder=recorder,
        )
        registry.for_role("coder").complete(REQUEST)
        registry.for_role("planner").complete(REQUEST)
        sink.close()

    assert len(sent) == 2
    recorded = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == 2, "bind_run 后每次调用都要落一份完整录制"
    assert all("fingerprint" in item for item in recorded)
    metadata = [json.loads(line) for line in requests_path.read_text(encoding="utf-8").splitlines()]
    assert len(metadata) == 2, "逐请求元数据是另一个文件，不与完整录制混写"
    assert [item["role"] for item in metadata] == ["coder", "planner"]
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    llm_events = [item for item in events if item["type"] == "llm_call"]
    assert [item["node"] for item in llm_events] == ["coder", "planner"]
    assert all(item["usage"] is not None for item in llm_events)
    assert llm_events[0]["usage"]["input_tokens"] == 100
    assert recorder.seq == 2
    summary = recorder.summary()
    assert summary["total_requests"] == 2
    assert set(summary["by_role"]) == {"coder", "planner"}


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
