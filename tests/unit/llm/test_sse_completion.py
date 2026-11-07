"""Real loopback SSE framing, settlement, cancellation and deadline checks."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

import codepilot.llm.dispatch_budget as dispatch_budget
from codepilot.llm.config import ModelPrice, ModelSpec, ProviderSpec
from codepilot.llm.dispatch_budget import (
    DispatchBudget,
    DispatchBudgetStoppedError,
    configure_dispatch_budget,
)
from codepilot.llm.openai_compatible import OpenAICompatibleProvider
from codepilot.llm.retry import RetryPolicy
from codepilot.llm.sse import SSECompletion
from codepilot.llm.tracking import UsageTrackingProvider
from codepilot.llm.types import LLMRequest, Message
from codepilot.observability.dispatch import Deadline, DispatchScope, dispatch_scope
from codepilot.schemas.usage import Usage


def event(delta=None, *, finish=None, usage=None):
    return (
        "data: "
        + json.dumps(
            {
                "id": "loopback-stream",
                "model": "fixture",
                "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
                if usage is None
                else [],
                "usage": usage,
            },
            ensure_ascii=False,
        )
        + "\r\n\r\n"
    )


USAGE = {"prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 2}}
WIRE = (
    event({"content": "收"})
    + event({"content": "到"})
    + event(finish="stop")
    + event(usage=USAGE)
    + "data: [DONE]\n\n"
).encode()


@contextmanager
def server(mode="normal"):
    state = {"requests": 0, "disconnected": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            state["requests"] += 1
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            assert body["stream"] and body["stream_options"]["include_usage"]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            try:
                if mode in {"cancel", "timeout"} or (
                    mode == "isolation" and self.headers.get("x-codepilot-purpose") == "cancel"
                ):
                    for _ in range(100):
                        self.wfile.write(event({"content": "partial"}).encode())
                        self.wfile.flush()
                        time.sleep(0.025)
                else:
                    wire = (
                        WIRE
                        if mode in {"normal", "isolation"}
                        else WIRE.replace(b"data: [DONE]\n\n", b"")
                    )
                    # Split both UTF-8 characters and SSE delimiters on real TCP.
                    for byte in wire:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                state["disconnected"].set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def provider(url, timeout=3):
    spec = ModelSpec(
        alias="sse-fixture",
        provider="loopback",
        model="fixture",
        context_window=1000,
        price=ModelPrice(
            currency="CNY", input_per_mtok=1, output_per_mtok=2, cached_input_per_mtok=0.1
        ),
    )
    return OpenAICompatibleProvider(
        spec=spec,
        provider=ProviderSpec(
            name="loopback",
            base_url=url,
            api_key_env="FIXTURE",
            timeout_seconds=timeout,
            trust_env=False,
        ),
        api_key="fixture-only",
        policy=RetryPolicy(max_attempts=3),
    )


REQUEST = LLMRequest(messages=[Message.user("reply")], max_output_tokens=16)


def test_real_socket_chunks_and_usage_settle_once(tmp_path: Path, monkeypatch):
    ledger = DispatchBudget(1, tmp_path / "ledger.json")
    monkeypatch.setattr(dispatch_budget, "_guard", None)
    configure_dispatch_budget(ledger)
    try:
        with server() as (url, state), provider(url) as model:
            deltas = []
            result = model.complete_stream(REQUEST, on_delta=deltas.append)
            assert deltas == ["收", "到"] and result.content == "收到"
            assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == (
                10,
                3,
                2,
            )
            assert result.raw["stream"]["done"] and result.raw["stream"]["content_deltas"] == 2
            assert state["requests"] == model.attempts == 1
        assert len(ledger.records) == 1 and ledger.records[0]["status"] == "settled"
        assert not ledger.frozen and ledger.total < 0.001
    finally:
        monkeypatch.setattr(dispatch_budget, "_guard", None)


@pytest.mark.parametrize("mode", ["cancel", "timeout", "truncated"])
def test_interrupted_stream_is_unknown_no_retry_no_owned_thread(
    tmp_path: Path, mode: str, monkeypatch
):
    ledger = DispatchBudget(1, tmp_path / "ledger.json")
    monkeypatch.setattr(dispatch_budget, "_guard", None)
    configure_dispatch_budget(ledger)
    cancelled = threading.Event()
    try:
        with server(mode) as (url, state), provider(url, timeout=0.35) as model:
            scope = DispatchScope(Deadline(time.monotonic() + 3), cancel=cancelled.is_set)
            started = time.monotonic()
            with dispatch_scope(scope), pytest.raises(DispatchBudgetStoppedError) as caught:
                model.complete_stream(
                    REQUEST, on_delta=lambda _: cancelled.set() if mode == "cancel" else None
                )
            assert caught.value.dispatched and model.unacknowledged == 1
            assert state["requests"] == model.attempts == 1
            assert time.monotonic() - started < 2
            if mode in {"cancel", "timeout"}:
                assert state["disconnected"].wait(1)
        assert ledger.frozen and ledger.records[0]["status"] == "unknown_frozen"
        assert ledger.total > 0
        assert not any(t.name == "codepilot-http" for t in threading.enumerate())
    finally:
        monkeypatch.setattr(dispatch_budget, "_guard", None)


@pytest.mark.parametrize(
    "suffix", ["", "data: [DONE]\n\n", event(finish="stop") + "data: [DONE]\n\n"]
)
def test_no_success_without_finish_done_and_usage(suffix):
    parser = SSECompletion()
    for line in (event({"content": "partial"}) + suffix).splitlines():
        parser.line(line)
    with pytest.raises(httpx.RemoteProtocolError):
        parser.finish()


def test_tool_argument_fragments_and_reasoning_are_preserved():
    wire = (
        event(
            {
                "reasoning_content": "reason",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "function": {"name": "read_file", "arguments": '{"path":'},
                    }
                ],
            }
        )
        + event({"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]})
        + event(finish="tool_calls")
        + event(usage=USAGE)
        + "data: [DONE]\n\n"
    )
    parser = SSECompletion()
    for line in wire.splitlines():
        parser.line(line)
    result = parser.finish()
    message = result["choices"][0]["message"]
    assert message["reasoning_content"] == "reason"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}


def test_cancelled_stream_does_not_poison_shared_provider(monkeypatch):
    monkeypatch.setattr(dispatch_budget, "_guard", None)
    cancelled = threading.Event()
    with server("isolation") as (url, state), provider(url) as model:

        def run_cancelled():
            scope = DispatchScope(Deadline(time.monotonic() + 3), cancel=cancelled.is_set)
            with dispatch_scope(scope), pytest.raises(Exception) as caught:
                model.complete_stream(
                    REQUEST.model_copy(update={"purpose": "cancel"}),
                    on_delta=lambda _: cancelled.set(),
                )
            from codepilot.llm.errors import ProviderError

            assert isinstance(caught.value, ProviderError) and caught.value.uncertain

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(run_cancelled)
            assert model.complete_stream(REQUEST).content == "收到"
            pending.result(timeout=5)
        assert model.complete_stream(REQUEST).content == "收到"
        assert state["requests"] == model.attempts == 3 and model.unacknowledged == 1
    assert not any(t.name == "codepilot-http" for t in threading.enumerate())


def test_stream_config_uses_existing_complete_and_usage_wrapper(monkeypatch):
    monkeypatch.setattr(dispatch_budget, "_guard", None)
    with server() as (url, state), provider(url) as model:
        model.spec = model.spec.model_copy(
            update={
                "extra_body": {"stream": True, "stream_options": {"include_usage": True}},
            }
        )
        usage = Usage()
        result = UsageTrackingProvider(model, usage=usage).complete(REQUEST)
        assert result.content == "收到" and usage.llm_calls == 1
        assert (usage.input_tokens, usage.output_tokens, usage.cached_input_tokens) == (10, 3, 2)
        assert usage.cost_unknown_calls == 0 and usage.cost > 0
        assert state["requests"] == model.attempts == 1


def test_mock_stream_observer_failure_is_uncertain_and_not_retried(monkeypatch):
    from codepilot.llm.errors import ProviderError

    monkeypatch.setattr(dispatch_budget, "_guard", None)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=WIRE)

    def observer(_):
        raise RuntimeError("callback-private-detail")

    with (
        httpx.Client(
            base_url="https://fixture.test", transport=httpx.MockTransport(handle)
        ) as client,
        provider("https://fixture.test") as model,
    ):
        model._client.close()
        model._client = client
        model._owns_client = False
        with pytest.raises(ProviderError) as caught:
            model.complete_stream(REQUEST, on_delta=observer)
        assert caught.value.uncertain and model.unacknowledged == 1
        assert "callback-private-detail" not in str(caught.value)
        assert model.attempts == len(calls) == 1


def test_invalid_finish_reason_is_rejected_before_settlement():
    parser = SSECompletion()
    with pytest.raises(httpx.RemoteProtocolError):
        for line in event(finish=True).splitlines():
            parser.line(line)
