"""Phase28 registry→adapter→回环 HTTP 闭环集成：真 HTTPTransport 打 127.0.0.1。

与 ``tests/unit/llm/test_phase28_http_matrix.py`` 的分工：单测直接构造 provider 并大量
使用 MockTransport；本文件从 **LLMRegistry（生产入口）** 出发——Fake 凭据走
``api_key_env`` 环境变量（monkeypatch 注入）、provider 由 registry 按
``LLMConfig``（角色→别名→provider spec）解析构造，请求经 **真实 HTTPTransport**
发往本机回环假服务，覆盖：

1. 正常响应：usage 解析、``Usage`` 统计归账、dispatch ledger 结算（settled + 精确费用）；
2. 总 deadline：慢速响应体在 deadline 处被截断，ledger 冻结为 unknown_frozen、
   服务端确认客户端断开——全部走真 socket，不经 MockTransport。

零外部网络（只绑 127.0.0.1 随机端口）、零 Docker、零真实模型请求、零费用。
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

import codepilot.llm.dispatch_budget as dispatch_budget
from codepilot.llm.config import LLMConfig, ModelPrice, ModelSpec, ProviderSpec
from codepilot.llm.dispatch_budget import DispatchBudgetStoppedError
from codepilot.llm.registry import LLMMode, LLMRegistry
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.types import LLMRequest, Message
from codepilot.observability.dispatch import Deadline, DispatchScope, dispatch_scope

BUDGET_SECONDS = 2  # 目标总时限
# dispatch_timeout 返回 int(min(limit, remaining))：scope 恰好 +2s 会被截到 1s。
# 与单测同一惯例（SCOPE = 目标 + 0.8），min() 后传输超时正好等于目标 2s。
SCOPE_SECONDS = BUDGET_SECONDS + 0.8

USAGE = {"prompt_tokens": 10, "completion_tokens": 5}
BODY = json.dumps(
    {
        "id": "phase28-loop-id",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": USAGE,
    }
).encode()

API_KEY_ENV = "PHASE28_S6_FAKE_KEY"


class _ServerState:
    def __init__(self, behavior: str) -> None:
        self.behavior = behavior
        self.requests = 0
        self.started = threading.Event()
        self.disconnected = threading.Event()
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: Any) -> None:  # 假服务日志不混进 pytest 输出
        return

    def do_POST(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        with state.lock:
            state.requests += 1
        state.started.set()
        try:
            if state.behavior == "drip":
                # 持续慢速响应体：单次 read 永不超时，只有总 deadline 能截断它。
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(BODY) * 1000))
                self.end_headers()
                for _ in range(4000):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.02)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            state.disconnected.set()


class _FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, behavior: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)  # 随机端口，绝无端口冲突
        self.state = _ServerState(behavior)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


@contextmanager
def fake_server(behavior: str = "ok") -> Iterator[_FakeServer]:
    server = _FakeServer(behavior)
    server.thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server.thread.join(timeout=5)


def loopback_config(base_url: str) -> LLMConfig:
    return LLMConfig(
        providers={
            "loopback": ProviderSpec(
                name="loopback",
                base_url=base_url,
                api_key_env=API_KEY_ENV,
                protocol="openai_compatible",
                max_concurrency=2,
                timeout_seconds=30,
                trust_env=False,  # 忽略进程代理设置：只准打回环
            )
        },
        models={
            "loop-model": ModelSpec(
                alias="loop-model",
                provider="loopback",
                model="fake-model-id",
                context_window=8000,
                max_output_tokens=512,
                price=ModelPrice(currency="CNY", input_per_mtok=1.0, output_per_mtok=2.0),
            )
        },
        roles={"coder": "loop-model"},
        budget_currency="CNY",
    )


def request() -> LLMRequest:
    return LLMRequest(
        messages=[Message.system("你是助手"), Message.user("修复这个 bug")],
        purpose="generic",
        max_output_tokens=512,
    )


def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """与本轮单测同一套 Fake 预算环境：预算 20 CNY、账本落 tmp。"""
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(dispatch_budget, "_guard", None)
    monkeypatch.setenv("CODEPILOT_DISPATCH_BUDGET_CNY", "20")
    monkeypatch.setenv("CODEPILOT_DISPATCH_LEDGER", str(path))
    return path


def test_registry_live_request_over_loopback_settles_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_env(tmp_path, monkeypatch)
    monkeypatch.setenv(API_KEY_ENV, "fake-key-not-real")
    with fake_server("ok") as server:
        registry = LLMRegistry(loopback_config(server.base_url), mode=LLMMode.LIVE)
        try:
            with dispatch_scope(DispatchScope(deadline=Deadline(time.monotonic() + 10))):
                result = registry.complete("coder", request())
        finally:
            registry.close()
        assert server.state.requests == 1  # 无隐藏重试：服务端恰好收到一次线上请求

    assert result.content == "ok"
    assert result.input_tokens == 10 and result.output_tokens == 5
    assert result.cost_known is True
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["frozen"] is False
    assert len(payload["requests"]) == 1
    record = payload["requests"][0]
    assert record["status"] == "settled"
    assert record["input_tokens"] == 10 and record["output_tokens"] == 5
    # 10 in × 1.0 CNY/MTok + 5 out × 2.0 CNY/MTok = 0.00002 CNY（Fake 单价下的精确结算）。
    assert record["cost_cny"] == pytest.approx(0.00002)
    assert record["cost_cny"] > 0


def test_registry_request_honors_total_deadline_over_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = ledger_env(tmp_path, monkeypatch)
    monkeypatch.setenv(API_KEY_ENV, "fake-key-not-real")
    with fake_server("drip") as server:
        registry = LLMRegistry(
            loopback_config(server.base_url), mode=LLMMode.LIVE, policy=RetryPolicy(max_attempts=1)
        )
        started = time.monotonic()
        try:
            with (
                pytest.raises(DispatchBudgetStoppedError) as caught,
                dispatch_scope(DispatchScope(deadline=Deadline(time.monotonic() + SCOPE_SECONDS))),
            ):
                registry.complete("coder", request())
        finally:
            registry.close()
        elapsed = time.monotonic() - started
        # 总 deadline 生效：慢体在 2s 处被截断（宽松上界防 CI 抖动，不用大 timeout 掩盖）。
        assert BUDGET_SECONDS - 0.5 < elapsed < 8.0
        assert caught.value.dispatched is True
        assert server.state.disconnected.wait(5), "客户端超时后没有断开连接"
        assert server.state.requests == 1

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert payload["frozen"] is True
    record = payload["requests"][0]
    assert record["status"] == "unknown_frozen"
    assert record["cost_cny"] is None
