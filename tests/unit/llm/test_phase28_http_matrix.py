"""Phase28 HTTP 兼容矩阵：本机回环 socket 验证，不打真实服务商。

矩阵（docs/notes/phase28-http-matrix.md 的证据来源），openai_compatible 与
native anthropic_messages 两个 adapter 都参数化覆盖：

1. 正常完整响应 + usage 解析（native 另有 ledger 结算断言）。
2. 延迟响应头（接近总 deadline 才回头）。
3. 持续慢体（总 deadline 触发 ReadTimeout）。
4. 分阶段累计超总时限（头 + 体分别 sleep，单段都不超时）。
5. 派发前取消（cancel / admit 两条 admission 拒绝路径，不产生 unknown）。
6. 派发后取消（RequestCancelled → uncertain，无遗留线程）。
7. 一个请求取消不影响另一个和后续请求。
8. 无隐藏重试：服务端计数器恰好 1 次线上请求。
9. 反复超时后无线程遗留（threading.enumerate 无 codepilot-http）。
10. gzip 正常解码语义 + gzip 慢流超时。
11. MockTransport 同步注入可用（含注入路径上的取消/超时语义）。
12. 自定义同步 transport（非 MockTransport / HTTPTransport）被 TypeError 拒绝。
13. 代理环境：HTTP_PROXY/HTTPS_PROXY 走回环假代理、NO_PROXY 放行直连、
    trust_env=False 忽略代理（全程 monkeypatch，不碰进程级配置）。
14. 连接复用观测：连续 5 次请求的服务端对端端口与逐请求耗时。
15. Bedrock 边界声明：执行路径不经过 http_lifecycle（结构 + 运行时双重断言）。

一切 HTTP 走 127.0.0.1 随机端口（bind 0）；密钥全部是假值；零外部网络。
"""

from __future__ import annotations

import gzip
import inspect
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

import codepilot.llm.anthropic_messages as anthropic_messages_module
import codepilot.llm.dispatch_budget as dispatch_budget
import codepilot.llm.http_lifecycle as http_lifecycle_module
import codepilot.llm.openai_compatible as openai_compatible_module
from codepilot.llm.anthropic_messages import AnthropicMessagesProvider
from codepilot.llm.bedrock_converse import BedrockConverseProvider
from codepilot.llm.config import ModelPrice, ModelSpec, ProviderSpec
from codepilot.llm.errors import ProviderError
from codepilot.llm.http_lifecycle import RequestCancelled, send_request
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

#: dispatch_timeout 取整数秒；scope 比目标总时限略大，min() 后总时限正好 2s。
DEADLINE_SECONDS = 2
SCOPE_SECONDS = DEADLINE_SECONDS + 0.8
PROTOCOLS = ["openai", "anthropic"]

# 一个响应体同时满足两个协议的解析器：
# openai 读 choices[].message/finish_reason/usage.prompt_tokens；
# native 读 content[]、stop_reason、usage.input_tokens。
USAGE = {
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "input_tokens": 10,
    "output_tokens": 5,
}
BODY = json.dumps(
    {
        "id": "phase28-fake-id",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    }
).encode()

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class _ServerState:
    def __init__(self, behavior: str, by_purpose: dict[str, str] | None = None) -> None:
        self.behavior = behavior
        self.by_purpose = by_purpose or {}
        self.requests = 0
        self.started = threading.Event()
        self.finished = threading.Event()
        self.disconnected = threading.Event()
        self.peer_ports: list[int] = []
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
                # 总时限 2s：头还没到就应被判超时，随后写头会因对端已断开而失败。
                time.sleep(2.6)
            if behavior in ("gzip", "gzip_drip"):
                compressed = gzip.compress(BODY)
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(compressed)))
                self.end_headers()
                if behavior == "gzip":
                    self.wfile.write(compressed)
                    self.wfile.flush()
                else:
                    for byte in compressed:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.05)
            elif behavior == "staged":
                # 头 0.8s + 4 个体分块各 0.5s：每段都远短于单阶段 timeout，
                # 但累计超过总时限，必须被绝对 deadline 截断。
                time.sleep(0.8)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                for chunk in (BODY[:1], BODY[1:2], BODY[2:3], BODY[3:]):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    time.sleep(0.5)
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
            elif behavior in ("ok", "slow_headers"):
                self._send_all(BODY)
            else:  # pragma: no cover - 测试自身配置错误
                raise AssertionError(f"unknown behavior {behavior}")
        except (BrokenPipeError, ConnectionResetError, OSError):
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


class _KeepAliveHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 keep-alive：用对端端口区分每条 TCP 连接（连接复用观测）。"""

    protocol_version = "HTTP/1.1"
    timeout = 5  # 客户端私有 client 每请求后即关闭；悬空 handler 最多等 5s

    def log_message(self, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        with state.lock:
            state.requests += 1
            state.peer_ports.append(self.client_address[1])
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)
        self.wfile.flush()


class _ProxyHandler(BaseHTTPRequestHandler):
    """回环假代理：记录请求并直接代答（http:// 经代理是绝对 URI 的普通 POST）。"""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        with state.lock:
            state.requests += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)
        self.wfile.flush()


class _FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        behavior: str = "ok",
        by_purpose: dict[str, str] | None = None,
        handler: type[BaseHTTPRequestHandler] = _Handler,
    ) -> None:
        super().__init__(("127.0.0.1", 0), handler)
        self.state = _ServerState(behavior, by_purpose)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    @property
    def root_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


@contextmanager
def fake_server(
    behavior: str = "ok",
    by_purpose: dict[str, str] | None = None,
    handler: type[BaseHTTPRequestHandler] = _Handler,
) -> Iterator[_FakeServer]:
    server = _FakeServer(behavior, by_purpose, handler)
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


def make_provider(
    protocol: str,
    base_url: str,
    *,
    client: httpx.Client | None = None,
    policy: RetryPolicy | None = None,
    trust_env: bool = False,
) -> OpenAICompatibleProvider | AnthropicMessagesProvider:
    provider_spec = ProviderSpec(
        name="fake",
        base_url=base_url,
        api_key_env="FAKE_KEY",
        timeout_seconds=30,
        max_concurrency=2,
        trust_env=trust_env,
    )
    cls: Any = AnthropicMessagesProvider if protocol == "anthropic" else OpenAICompatibleProvider
    provider = cls(
        spec=model_spec(),
        provider=provider_spec,
        api_key="test-key-not-real",
        client=client,
        policy=policy,
    )
    if client is None:
        # 回环路径必须落在 HTTPTransport 上，矩阵行才是对 send_request 流式路径的验证。
        assert isinstance(provider._client._transport, httpx.HTTPTransport)
    return provider


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


def assert_no_http_worker_threads() -> None:
    assert not any(t.name == "codepilot-http" for t in threading.enumerate()), (
        "存在遗留的 codepilot-http 桥接线程"
    )


def point_proxy_env(
    monkeypatch: pytest.MonkeyPatch, proxy_url: str, *, no_proxy: str | None = None
) -> None:
    """用 monkeypatch 隔离并设置代理环境变量，不碰进程级真实配置。"""
    for key in _PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTP_PROXY", proxy_url)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    if no_proxy is not None:
        monkeypatch.setenv("NO_PROXY", no_proxy)


# --------------------------------------------------------------- 矩阵行 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row1_complete_response_parses_usage(protocol: str) -> None:
    with fake_server("ok") as server:
        provider = make_provider(protocol, server.base_url)
        try:
            with deadline_scope():
                result = provider.complete(request())
            assert result.content == "ok"
            assert result.input_tokens == 10
            assert result.output_tokens == 5
            assert result.cached_input_tokens == 0
            assert result.cost_known is True
            assert result.cost > 0
            if protocol == "openai":
                assert result.finish_reason == "stop"
            else:
                assert result.finish_reason == "end_turn"
            assert provider.attempts == 1
            assert provider.unacknowledged == 0
            assert server.state.requests == 1
        finally:
            provider.close()


def test_row1_native_usage_settles_in_dispatch_ledger(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """native usage 结算：完整走 guarded_send，ledger 记录 settled 与真实用量。"""
    ledger = tmp_path / "native-ledger.json"
    monkeypatch.setattr(dispatch_budget, "_guard", None)
    monkeypatch.setenv("CODEPILOT_DISPATCH_BUDGET_CNY", "20")
    monkeypatch.setenv("CODEPILOT_DISPATCH_LEDGER", str(ledger))
    with fake_server("ok") as server:
        provider = make_provider("anthropic", server.base_url)
        try:
            with deadline_scope():
                result = provider.complete(request())
            assert result.input_tokens == 10
            assert result.output_tokens == 5
        finally:
            provider.close()
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["frozen"] is False
    assert len(payload["requests"]) == 1
    record = payload["requests"][0]
    assert record["status"] == "settled"
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 5
    assert record["cost_cny"] > 0
    assert record["response_id"] == "phase28-fake-id"


# --------------------------------------------------------------- 矩阵行 2


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row2_delayed_headers_exceed_total_deadline(protocol: str) -> None:
    with fake_server("slow_headers") as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError) as caught, deadline_scope():
                provider.complete(request())
            elapsed = time.monotonic() - started
            assert caught.value.uncertain is True
            assert DEADLINE_SECONDS - 0.2 <= elapsed <= DEADLINE_SECONDS + 1.2
            assert server.state.requests == 1
            assert provider.unacknowledged == 1
            assert_no_http_worker_threads()
            assert server.state.finished.wait(4.0), "服务端 handler 未退出"
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 3


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row3_continuous_slow_body_hits_total_deadline(protocol: str) -> None:
    with fake_server("drip") as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError) as caught, deadline_scope():
                provider.complete(request())
            elapsed = time.monotonic() - started
            assert caught.value.uncertain is True
            # 分块从不让单次 read 超时：总 deadline 必须生效。
            assert DEADLINE_SECONDS - 0.2 <= elapsed <= DEADLINE_SECONDS + 1.2
            assert provider.attempts == 1
            assert provider.unacknowledged == 1
            assert server.state.requests == 1
            assert server.state.disconnected.wait(2.0), "客户端超时后没有断开连接"
            assert server.state.finished.wait(2.0), "服务端 handler 未退出，请求被遗弃"
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 4


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row4_staged_phases_cumulatively_exceed_deadline(protocol: str) -> None:
    with fake_server("staged") as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError) as caught, deadline_scope():
                provider.complete(request())
            elapsed = time.monotonic() - started
            assert caught.value.uncertain is True
            # 0.8s 头 + 每 0.5s 一个分块：单段不超时，累计必须被截断。
            assert DEADLINE_SECONDS - 0.2 <= elapsed <= DEADLINE_SECONDS + 1.2
            assert server.state.requests == 1
            assert provider.unacknowledged == 1
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 5


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row5_cancel_before_dispatch_sends_nothing(protocol: str) -> None:
    with fake_server("ok") as server:
        provider = make_provider(protocol, server.base_url)
        try:
            with pytest.raises(ResourceCancelledError), deadline_scope(cancel=lambda: True):
                provider.complete(request())
            assert server.state.requests == 0
            assert provider.attempts == 0
            assert provider.unacknowledged == 0
            assert_no_http_worker_threads()
        finally:
            provider.close()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row5_admit_rejection_sends_nothing(protocol: str) -> None:
    def admit(resource: str) -> None:
        raise ResourceCancelledError(f"admission rejected: {resource}")

    with fake_server("ok") as server:
        provider = make_provider(protocol, server.base_url)
        try:
            with pytest.raises(ResourceCancelledError), deadline_scope(admit=admit):
                provider.complete(request())
            assert server.state.requests == 0
            assert provider.attempts == 0
            assert provider.unacknowledged == 0
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 6


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row6_cancel_after_dispatch_is_uncertain_without_leak(protocol: str) -> None:
    cancel = threading.Event()
    with fake_server("drip") as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        timer = threading.Thread(target=lambda: (server.state.started.wait(2), cancel.set()))
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError) as caught, deadline_scope(cancel=cancel.is_set):
                provider.complete(request())
            elapsed = time.monotonic() - started
            timer.join(2)
            assert caught.value.uncertain is True
            # 取消不必等总时限：在 deadline 之前返回。
            assert elapsed < 1.5
            assert provider.attempts == 1
            # 已派发：结果未知（远端可能已计费），不能记成确定。
            assert provider.unacknowledged == 1
            assert server.state.requests == 1
            assert server.state.finished.wait(2.0), "取消后客户端没有断开，handler 未退出"
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 7


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row7_cancelling_one_request_does_not_affect_others(protocol: str) -> None:
    with fake_server("ok", by_purpose={"slow": "gzip_drip"}) as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        cancel = threading.Event()
        errors: list[BaseException] = []

        def slow() -> None:
            try:
                with deadline_scope(cancel=cancel.is_set):
                    provider.complete(request("slow"))
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=slow)
        worker.start()
        try:
            assert server.state.started.wait(2), "慢请求未到达服务端"
            cancel.set()
            assert provider.complete(request("fast")).content == "ok"
            worker.join(3)
            assert not worker.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], ProviderError)
            assert errors[0].uncertain is True
            # 取消之后同一 provider 的后续请求照常成功。
            assert provider.complete(request("fast")).content == "ok"
            assert server.state.requests == 3
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 8


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row8_no_hidden_retry_single_wire_request(protocol: str) -> None:
    """默认重试策略（4 次尝试）下，总时限耗尽后不得有任何第二次线上请求。"""
    with fake_server("drip") as server:
        provider = make_provider(protocol, server.base_url)
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError) as caught, deadline_scope():
                provider.complete(request())
            elapsed = time.monotonic() - started
            assert caught.value.uncertain is True
            # 截止后拦下重试的 DispatchStoppedError 不能掩盖"已派发、结果未知"。
            assert isinstance(caught.value.__cause__, DispatchStoppedError)
            assert provider.attempts == 1
            assert server.state.requests == 1
            assert elapsed <= DEADLINE_SECONDS + 1.0
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 9


def test_row9_no_thread_leak_after_repeated_timeouts() -> None:
    with fake_server("drip", by_purpose={"fast": "ok"}) as server:
        provider = make_provider("openai", server.base_url, policy=RetryPolicy(max_attempts=1))
        try:
            for _ in range(2):
                with pytest.raises(ProviderError), deadline_scope():
                    provider.complete(request())
                assert server.state.finished.wait(2.0)
            assert_no_http_worker_threads()
            # 泄漏的线程/被关掉的连接池没有影响后续请求。
            with deadline_scope():
                assert provider.complete(request("fast")).content == "ok"
            assert server.state.requests == 3
            assert provider.attempts == 3
            assert provider.unacknowledged == 2
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 10


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row10_gzip_decodes_once_with_decoded_length(protocol: str) -> None:
    with fake_server("gzip") as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        try:
            with deadline_scope():
                response = provider._post({"model": "fake-model-id"})
            # content 是解码后的明文：等于原始 JSON 而非 gzip 字节（恰好解码一次）。
            assert response.content == BODY
            assert "content-encoding" not in response.headers
            assert "transfer-encoding" not in response.headers
            # 重建响应的 content-length 语义必须是解码后的长度，而不是线上字节数。
            assert int(response.headers["content-length"]) == len(BODY)
            assert json.loads(response.text)["usage"]["input_tokens"] == 10
            assert_no_http_worker_threads()
        finally:
            provider.close()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row10_slow_gzip_stream_hits_total_deadline(protocol: str) -> None:
    with fake_server("gzip_drip") as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        started = time.monotonic()
        try:
            with pytest.raises(ProviderError) as caught, deadline_scope():
                provider.complete(request())
            elapsed = time.monotonic() - started
            assert caught.value.uncertain is True
            assert elapsed <= DEADLINE_SECONDS + 1.2
            assert server.state.requests == 1
            assert server.state.finished.wait(3.0), "服务端 handler 未退出"
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 11


def _mock_client() -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=BODY)),
        base_url="http://mock.test",
    )


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row11_mock_transport_synchronous_injection(protocol: str) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=BODY, headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://mock.test")
    provider = make_provider(protocol, "http://mock.test", client=client)
    try:
        with deadline_scope():
            result = provider.complete(request())
        # handler 内同步返回：一次调用、一次解析、无线程。
        assert result.content == "ok"
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert len(seen) == 1
        assert provider.attempts == 1
        assert provider.unacknowledged == 0
        assert_no_http_worker_threads()
    finally:
        provider.close()


def test_row11_mock_transport_cancel_after_dispatch_raises_requestcancelled() -> None:
    client = _mock_client()
    with deadline_scope(cancel=lambda: True), pytest.raises(RequestCancelled) as caught:
        send_request(
            client,
            "/v1/chat/completions",
            body={"model": "fake-model-id"},
            headers={},
            timeout=httpx.Timeout(2),
            deadline=time.monotonic() + 2,
            trust_env=False,
        )
    assert isinstance(caught.value, httpx.TransportError)
    assert_no_http_worker_threads()


def test_row11_mock_transport_past_deadline_raises_readtimeout() -> None:
    client = _mock_client()
    with pytest.raises(httpx.ReadTimeout):
        send_request(
            client,
            "/v1/chat/completions",
            body={"model": "fake-model-id"},
            headers={},
            timeout=httpx.Timeout(2),
            deadline=time.monotonic() - 1,
            trust_env=False,
        )
    assert_no_http_worker_threads()


# --------------------------------------------------------------- 矩阵行 12


class _BlockingTransport(httpx.BaseTransport):
    """非 MockTransport、非 HTTPTransport 的同步自定义 transport。"""

    def handle_request(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY)


def test_row12_custom_sync_transport_rejected_with_typeerror() -> None:
    client = httpx.Client(transport=_BlockingTransport(), base_url="http://custom.test")
    with deadline_scope(), pytest.raises(TypeError, match="HTTPTransport"):
        send_request(
            client,
            "/v1/chat/completions",
            body={"model": "fake-model-id"},
            headers={},
            timeout=httpx.Timeout(2),
            deadline=time.monotonic() + 2,
            trust_env=False,
        )
    assert_no_http_worker_threads()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row12_custom_sync_transport_rejected_through_provider(protocol: str) -> None:
    client = httpx.Client(transport=_BlockingTransport(), base_url="http://custom.test")
    provider = make_provider(protocol, "http://custom.test", client=client)
    try:
        with deadline_scope(), pytest.raises(TypeError, match="HTTPTransport"):
            provider.complete(request())
        # TypeError 不属于可重试/可翻译的传输错误，必须原样穿透。
        assert provider.attempts == 1
        assert_no_http_worker_threads()
    finally:
        provider.close()


# --------------------------------------------------------------- 矩阵行 13


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row13a_proxy_env_routes_request_through_loopback_proxy(
    protocol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fake_server("ok") as origin, fake_server(handler=_ProxyHandler) as proxy:
        point_proxy_env(monkeypatch, proxy.root_url)
        provider = make_provider(protocol, origin.base_url, trust_env=True)
        try:
            with deadline_scope():
                result = provider.complete(request())
            assert result.content == "ok"
            assert proxy.state.requests == 1, "请求没有走向回环假代理"
            assert origin.state.requests == 0
        finally:
            provider.close()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row13b_no_proxy_bypasses_proxy_for_loopback(
    protocol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fake_server("ok") as origin, fake_server(handler=_ProxyHandler) as proxy:
        point_proxy_env(monkeypatch, proxy.root_url, no_proxy="127.0.0.1")
        provider = make_provider(protocol, origin.base_url, trust_env=True)
        try:
            with deadline_scope():
                result = provider.complete(request())
            assert result.content == "ok"
            assert proxy.state.requests == 0, "NO_PROXY 没有放行回环直连"
            assert origin.state.requests == 1
        finally:
            provider.close()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row13c_trust_env_false_ignores_proxy_env(
    protocol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fake_server("ok") as origin, fake_server(handler=_ProxyHandler) as proxy:
        point_proxy_env(monkeypatch, proxy.root_url)
        provider = make_provider(protocol, origin.base_url, trust_env=False)
        try:
            with deadline_scope():
                result = provider.complete(request())
            assert result.content == "ok"
            assert proxy.state.requests == 0, "trust_env=False 仍然使用了环境代理"
            assert origin.state.requests == 1
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 14


def test_row14_connection_reuse_observation() -> None:
    """连续 5 次请求：每次都应是一条新 TCP 连接（请求私有 client），记录耗时。"""
    with fake_server("ok", handler=_KeepAliveHandler) as server:
        provider = make_provider("openai", server.base_url)
        timings: list[float] = []
        try:
            for _ in range(5):
                started = time.monotonic()
                with deadline_scope():
                    result = provider.complete(request())
                timings.append(time.monotonic() - started)
                assert result.content == "ok"
            assert server.state.requests == 5
            with server.state.lock:
                ports = list(server.state.peer_ports)
            assert len(ports) == 5
            # send_request 每请求新建私有 AsyncClient：不存在跨请求连接复用。
            assert len(set(ports)) == 5, f"出现连接复用（对端端口重复）: {ports}"
            assert all(t < 2.0 for t in timings), f"单次请求过慢: {timings}"
            # 供文档记录（pytest -s 可见）。
            print("PHASE28_ROW14_TIMINGS_MS", [round(t * 1000, 1) for t in timings])
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 15


def test_row15_bedrock_execution_path_bypasses_http_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codepilot.llm.bedrock_converse as bedrock_converse

    source = inspect.getsource(bedrock_converse)
    assert "http_lifecycle" not in source
    assert "send_request" not in source
    assert "httpx" not in source
    assert "asyncio" not in source

    def _forbidden(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise AssertionError("Bedrock 执行路径不应进入 http_lifecycle.send_request")

    # 运行时证据：把所有 HTTP adapter 的入口全部下毒，Bedrock 仍完整跑通。
    monkeypatch.setattr(http_lifecycle_module, "send_request", _forbidden)
    monkeypatch.setattr(openai_compatible_module, "send_request", _forbidden)
    monkeypatch.setattr(anthropic_messages_module, "send_request", _forbidden)

    converse_calls: list[dict[str, Any]] = []

    class _FakeRuntimeClient:
        def converse(self, **body: Any) -> dict[str, Any]:
            converse_calls.append(body)
            return {
                "output": {"message": {"content": [{"text": "ok"}]}},
                "usage": {"inputTokens": 10, "outputTokens": 5},
                "stopReason": "end_turn",
            }

    provider_spec = ProviderSpec(
        name="aws-fake",
        base_url="https://bedrock.fake.test",
        protocol="bedrock_converse",
        region="us-east-1",
    )
    provider = BedrockConverseProvider(
        spec=model_spec(), provider=provider_spec, client=_FakeRuntimeClient()
    )
    try:
        result = provider.complete(request())
        assert result.content == "ok"
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.finish_reason == "end_turn"
        assert len(converse_calls) == 1
        assert converse_calls[0]["modelId"] == "fake-model-id"
    finally:
        provider.close()
