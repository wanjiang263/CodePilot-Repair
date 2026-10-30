"""Phase29 HTTP 兼容矩阵扩展：错误路径与可观测重试（本机回环，零真实服务商）。

Phase28 矩阵（``test_phase28_http_matrix.py``，39 例）覆盖正常响应、慢头/慢体/
阶段超时、取消语义、隔离、gzip、MockTransport、transport 拦截、代理、连接复用
与 Bedrock 边界；本轮在此之上补齐 **错误响应路径**（docs/notes/phase29-http-matrix.md
行 16–19）：

16. HTTP 500 + JSON 错误体 → ``ProviderError``（确定性失败，uncertain=False），
    线上恰好 1 次请求、无隐藏重试、无线程遗留。
17. 200 + 非法 JSON 体 → ``ProviderError``（网关截断/HTML 错误页症状必须归因为
    provider 失败，而不是不可归因的内部错误）。
18. HTTP 429 + Retry-After → ``RateLimitError``；重试策略下的每次尝试都计入
    ``provider.attempts``，与线上请求计数一致（重试可观测，不隐藏）。
19. 连接拒绝（无监听端口的回环地址）→ ``ProviderError``，无线程遗留。

一切 HTTP 走 127.0.0.1 随机端口（bind 0）；密钥全部是假值；零外部网络、
零真实模型请求。
"""

from __future__ import annotations

import json
import socket
import time
from http.server import BaseHTTPRequestHandler
from typing import Any

import pytest
from test_phase28_http_matrix import (
    DEADLINE_SECONDS,
    _ServerState,
    assert_no_http_worker_threads,
    deadline_scope,
    fake_server,
    make_provider,
    request,
)

from codepilot.llm.errors import ProviderError, RateLimitError
from codepilot.llm.retry import RetryPolicy

PROTOCOLS = ["openai", "anthropic"]

ERROR_BODY = json.dumps(
    {"error": {"type": "server_error", "message": "phase29 synthetic failure"}}
).encode()


class _ErrorPathHandler(BaseHTTPRequestHandler):
    """错误路径行为：500 / 非法 JSON / 429+Retry-After（复用 _FakeServer 状态）。"""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        state: _ServerState = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        with state.lock:
            state.requests += 1
        state.started.set()
        behavior = state.behavior
        if behavior == "http_500":
            payload, status = ERROR_BODY, 500
        elif behavior == "malformed_json":
            payload, status = b"<html>gateway error page</html>", 200
        elif behavior == "http_429":
            payload, status = ERROR_BODY, 429
        else:  # pragma: no cover - 测试自身配置错误
            raise AssertionError(f"unknown behavior {behavior}")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if behavior == "http_429":
            self.send_header("Retry-After", "0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        state.finished.set()


def error_server(behavior: str):
    return fake_server(behavior, handler=_ErrorPathHandler)


# --------------------------------------------------------------- 矩阵行 16


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row16_http_500_is_deterministic_provider_error(protocol: str) -> None:
    with error_server("http_500") as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        try:
            with pytest.raises(ProviderError) as caught, deadline_scope():
                provider.complete(request())
            # 已收到完整响应：失败是确定的，不得标成 uncertain。
            assert caught.value.uncertain is False
            assert caught.value.body == {
                "error": {"type": "server_error", "message": "phase29 synthetic failure"}
            }
            assert provider.attempts == 1
            assert server.state.requests == 1
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 17


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row17_malformed_json_body_is_provider_error(protocol: str) -> None:
    with error_server("malformed_json") as server:
        provider = make_provider(protocol, server.base_url, policy=RetryPolicy(max_attempts=1))
        try:
            with pytest.raises(ProviderError) as caught, deadline_scope():
                provider.complete(request())
            assert caught.value.uncertain is False
            assert "JSON" in str(caught.value)
            assert provider.attempts == 1
            assert server.state.requests == 1
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 18


def test_row18_http_429_retries_are_visible_and_bounded() -> None:
    """重试策略允许的每次尝试都必须出现在 attempts 与线上计数中，不隐藏。"""
    with error_server("http_429") as server:
        provider = make_provider("openai", server.base_url, policy=RetryPolicy(max_attempts=3))
        try:
            with pytest.raises(RateLimitError) as caught, deadline_scope(seconds=30.0):
                provider.complete(request())
            assert caught.value.retry_after == 0.0
            # 3 次尝试全部真实发生且可观测：attempts == 线上请求 == 3。
            assert provider.attempts == 3
            assert server.state.requests == 3
            assert_no_http_worker_threads()
        finally:
            provider.close()


# --------------------------------------------------------------- 矩阵行 19


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_row19_connection_refused_is_classified_provider_error(protocol: str) -> None:
    """无监听端口的回环地址：连接拒绝按传输失败归类（uncertain，保守），
    在总期限内被绝对 deadline 截断并归因为 provider 失败，无线程遗留。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()  # 端口已释放，无人监听
    provider = make_provider(
        protocol, f"http://127.0.0.1:{dead_port}/v1", policy=RetryPolicy(max_attempts=1)
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError) as caught, deadline_scope():
            provider.complete(request())
        # 连接层失败无法排除"半途发送"的可能：lifecycle 保守地标 uncertain。
        assert caught.value.uncertain is True
        assert time.monotonic() - started <= DEADLINE_SECONDS + 1.0
        assert_no_http_worker_threads()
    finally:
        provider.close()
