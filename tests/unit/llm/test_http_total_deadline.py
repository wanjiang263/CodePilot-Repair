"""HTTP 请求总 deadline：用本机回环假服务验证，不打真实服务商。

覆盖：延迟响应头、持续慢速响应体、分阶段累计超时、正常完整响应、
派发前取消、派发后超时、一个请求取消不影响另一个、无隐藏重试、
超时后无遗留工作、settled/unknown/cancelled admission 分类。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from codepilot.llm.config import ModelPrice, ModelSpec, ProviderSpec
from codepilot.llm.dispatch_budget import DispatchBudgetStoppedError
from codepilot.llm.errors import ProviderError
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.types import LLMRequest, Message
from codepilot.observability.dispatch import (
    Deadline,
    DispatchScope,
    DispatchStoppedError,
    dispatch_scope,
)
from codepilot.observability.resources import ResourceCancelledError

#: dispatch_timeout 取整数秒；比目标总时限略大，确保 min() 后正好是目标值。
DEADLINE_SECONDS = 2
SCOPE_SECONDS = DEADLINE_SECONDS + 0.8

BODY = json.dumps(
    {
        "id": "fake",
        "choices": [
            {"message": {"content": "ok"}, "finish_reason": "stop"},
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
).encode()


class _ServerState:
    def __init__(self, behavior: str, by_purpose: dict[str, str] | None = None) -> None:
        self.behavior = behavior
        self.by_purpose = by_purpose or {}
        self.requests = 0
        self.started = threading.Event()
        self.finished = threading.Event()
        self.disconnected = threading.Event()
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: Any) -> None:  # 不把假服务日志混进 pytest 输出
        return

    def do_POST(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        purpose = self.headers.get("x-codepilot-purpose") or "generic"
        behavior = state.by_purpose.get(purpose, state.behavior)
        with state.lock:
            state.requests += 1
        state.started.set()
        try:
            if behavior == "slow_headers":
                # 客户端总时限 2s：头还没到就应被判超时，随后写头会因对端已断开而失败。
                time.sleep(2.6)
            if behavior == "ok" or behavior == "slow_headers":
                self._send_all(BODY)
            elif behavior == "drip":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(BODY) * 1000))
                self.end_headers()
                try:
                    for _ in range(4000):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    state.disconnected.set()
            elif behavior == "staged":
                time.sleep(0.8)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                for chunk in (BODY[:1], BODY[1:2], BODY[2:3], BODY[3:]):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.5)
            else:  # pragma: no cover - 测试自身配置错误
                raise AssertionError(f"unknown behavior {behavior}")
        except (BrokenPipeError, ConnectionResetError):
            state.disconnected.set()
        finally:
            state.finished.set()

    def _send_all(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


class _FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, behavior: str, by_purpose: dict[str, str] | None = None) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = _ServerState(behavior, by_purpose)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


@contextmanager
def fake_server(
    behavior: str = "ok", by_purpose: dict[str, str] | None = None
) -> Iterator[_FakeServer]:
    server = _FakeServer(behavior, by_purpose)
    server.thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server.thread.join(timeout=5)


def model_spec() -> ModelSpec:
    return ModelSpec(
        alias="fake-model",
        provider="fake",
        model="fake-model-id",
        context_window=8000,
        max_output_tokens=512,
        price=ModelPrice(currency="CNY", input_per_mtok=1.0, output_per_mtok=2.0),
    )


def provider_for(
    base_url: str,
    *,
    policy: RetryPolicy | None = None,
    max_concurrency: int = 2,
    sleep: Any = time.sleep,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        spec=model_spec(),
        provider=ProviderSpec(
            name="fake",
            base_url=base_url,
            api_key_env="FAKE_KEY",
            timeout_seconds=30,
            max_concurrency=max_concurrency,
            trust_env=False,
        ),
        api_key="test-key-not-real",
        policy=policy,
        sleep=sleep,
    )


def request(purpose: str = "generic") -> LLMRequest:
    return LLMRequest(
        messages=[Message.system("你是助手"), Message.user("修复这个 bug")],
        purpose=purpose,
        max_output_tokens=512,
    )


@contextmanager
def deadline_scope(
    seconds: float = SCOPE_SECONDS, cancel: Any = None, admit: Any = None
) -> Iterator[DispatchScope]:
    scope = DispatchScope(deadline=Deadline(time.monotonic() + seconds), cancel=cancel, admit=admit)
    with dispatch_scope(scope):
        yield scope


def call_under_deadline(provider: OpenAICompatibleProvider, purpose: str = "generic") -> None:
    with deadline_scope():
        provider.complete(request(purpose))


def test_total_deadline_bounds_a_continuous_slow_body() -> None:
    with fake_server("drip") as server:
        provider = provider_for(server.base_url, policy=RetryPolicy(max_attempts=1))
        started = time.monotonic()
        with pytest.raises(ProviderError) as caught:
            call_under_deadline(provider)
        elapsed = time.monotonic() - started
        assert caught.value.uncertain is True
        # 持续慢速响应体：分块从不让单次 read 超时，总时限必须生效。
        assert DEADLINE_SECONDS - 0.2 <= elapsed <= DEADLINE_SECONDS + 1.2
        assert provider.attempts == 1
        assert provider.unacknowledged == 1
        assert server.state.requests == 1
        assert server.state.disconnected.wait(2.0), "客户端超时后没有断开连接"
        assert server.state.finished.wait(2.0), "服务端 handler 未退出，请求被遗弃"
        provider.close()


def test_total_deadline_bounds_delayed_headers() -> None:
    with fake_server("slow_headers") as server:
        provider = provider_for(server.base_url, policy=RetryPolicy(max_attempts=1))
        started = time.monotonic()
        with pytest.raises(ProviderError) as caught:
            call_under_deadline(provider)
        elapsed = time.monotonic() - started
        assert caught.value.uncertain is True
        assert elapsed <= DEADLINE_SECONDS + 1.0
        assert server.state.requests == 1
        provider.close()


def test_total_deadline_bounds_staged_response() -> None:
    with fake_server("staged") as server:
        provider = provider_for(server.base_url, policy=RetryPolicy(max_attempts=1))
        started = time.monotonic()
        with pytest.raises(ProviderError) as caught:
            call_under_deadline(provider)
        elapsed = time.monotonic() - started
        assert caught.value.uncertain is True
        # 0.8s 头 + 每 0.5s 一个分块：每段都短于单阶段 timeout，累计必须被截断。
        assert DEADLINE_SECONDS - 0.2 <= elapsed <= DEADLINE_SECONDS + 1.2
        assert server.state.requests == 1
        provider.close()


def test_complete_response_under_the_deadline_succeeds() -> None:
    with fake_server("ok") as server:
        provider = provider_for(server.base_url)
        with deadline_scope():
            result = provider.complete(request())
        assert result.content == "ok"
        assert result.input_tokens == 10
        assert provider.attempts == 1
        assert provider.unacknowledged == 0
        provider.close()


def test_no_hidden_retry_after_the_deadline_expires() -> None:
    with fake_server("drip") as server:
        # 默认策略本来会重试“无 status 的 ProviderError”；但总时限已耗尽，
        # 第二次派发必须在 mark_model_dispatch 之前被 deadline 挡住。
        # 而且拦下它的 DispatchStoppedError 不能覆盖“已派发、结果未知”的原始错误，
        # 否则预算层会把这次请求当成未派发而退款。
        provider = provider_for(server.base_url, sleep=lambda _s: None)
        started = time.monotonic()
        with pytest.raises(ProviderError) as caught:
            call_under_deadline(provider)
        elapsed = time.monotonic() - started
        assert caught.value.uncertain is True
        assert isinstance(caught.value.__cause__, DispatchStoppedError)
        assert provider.attempts == 1
        assert server.state.requests == 1
        # 没有在截止后再睡一轮退避：总时限到点就结束。
        assert elapsed <= DEADLINE_SECONDS + 1.0
        provider.close()


def test_one_request_timeout_does_not_affect_another() -> None:
    with fake_server("ok", by_purpose={"slow": "drip", "fast": "ok"}) as server:
        provider = provider_for(server.base_url)
        results: dict[str, Any] = {}

        def run(purpose: str) -> None:
            try:
                with deadline_scope():
                    results[purpose] = provider.complete(request(purpose))
            except BaseException as exc:
                results[purpose] = exc

        slow = threading.Thread(target=run, args=("slow",))
        slow.start()
        time.sleep(0.3)  # 让慢请求先进入流式读取
        fast = threading.Thread(target=run, args=("fast",))
        fast.start()
        fast.join(timeout=5)
        slow.join(timeout=DEADLINE_SECONDS + 5)

        assert not slow.is_alive() and not fast.is_alive()
        assert getattr(results["fast"], "content", None) == "ok"
        assert isinstance(results["slow"], ProviderError)
        assert results["slow"].uncertain is True
        # 共享 client 没被慢请求的取消关掉：快请求正常完成，服务端收到两次请求。
        assert server.state.requests == 2
        provider.close()


def test_cancel_before_dispatch_sends_nothing_and_is_not_unknown(tmp_path: Path) -> None:
    with fake_server("ok") as server:
        provider = provider_for(server.base_url)
        with pytest.raises(ResourceCancelledError), deadline_scope(cancel=lambda: True):
            provider.complete(request())
        assert server.state.requests == 0
        assert provider.attempts == 0
        assert provider.unacknowledged == 0
        provider.close()


def _ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import codepilot.llm.dispatch_budget as dispatch_budget

    path = tmp_path / "ledger.json"
    monkeypatch.setattr(dispatch_budget, "_guard", None)
    monkeypatch.setenv("CODEPILOT_DISPATCH_BUDGET_CNY", "20")
    monkeypatch.setenv("CODEPILOT_DISPATCH_LEDGER", str(path))
    return path


def test_settled_admission_uses_trusted_complete_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path, monkeypatch)
    with fake_server("ok") as server:
        provider = provider_for(server.base_url)
        with deadline_scope():
            result = provider.complete(request())
        assert result.input_tokens == 10
        provider.close()
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["frozen"] is False
    assert len(payload["requests"]) == 1
    record = payload["requests"][0]
    assert record["status"] == "settled"
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 5
    assert record["cost_cny"] > 0
    assert payload["cancelled_admissions"] == []


def test_timeout_after_dispatch_is_unknown_and_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path, monkeypatch)
    with fake_server("drip") as server:
        provider = provider_for(server.base_url, policy=RetryPolicy(max_attempts=1))
        with pytest.raises(DispatchBudgetStoppedError) as caught:
            call_under_deadline(provider)
        assert caught.value.dispatched is True
        provider.close()
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["frozen"] is True
    assert len(payload["requests"]) == 1
    record = payload["requests"][0]
    assert record["status"] == "unknown_frozen"
    assert record["timeout_phase"] == "ReadTimeout"
    assert record["cost_cny"] is None


def test_cancelled_after_reservation_before_http_is_refunded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path, monkeypatch)
    with fake_server("ok") as server:
        provider = provider_for(server.base_url)

        def admit(resource: str) -> None:
            # 预留记录已落盘、但 HTTP 还没发出：这里模拟取消。
            if not ledger.exists():
                return
            text = ledger.read_text(encoding="utf-8")
            if '"status": "reserved"' in text:
                raise ResourceCancelledError("cancelled after reservation")

        with pytest.raises(ResourceCancelledError), deadline_scope(admit=admit):
            provider.complete(request())
        assert server.state.requests == 0
        assert provider.attempts == 0
        provider.close()
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["frozen"] is False
    assert payload["requests"] == []
    assert len(payload["cancelled_admissions"]) == 1
    admission = payload["cancelled_admissions"][0]
    assert admission["status"] == "settled"
    assert admission["dispatch_status"] == "not_dispatched"
    assert admission["reserved_cny"] == 0.0
    assert payload["accounted_or_reserved"] == 0.0
