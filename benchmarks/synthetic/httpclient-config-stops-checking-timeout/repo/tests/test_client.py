"""``httpclient.client`` 的现有测试。

这些测试刻意写成**不依赖 sender 的参数个数**，也不依赖 ``new_client`` 是否转发
配置关键字。本库允许"把重试次数一路传到传输层"这类跨文件演进（见 cross-module
任务），把签名写死的可见测试会在正确修复之后变红，于是修复被自己的测试挡住。
"""

from __future__ import annotations

import pytest

from httpclient.client import Client, new_client
from httpclient.config import Config
from httpclient.transport import build_sender


def failing_sender(*args: object, **kwargs: object) -> str:
    raise ConnectionError("nope")


def test_request_returns_response() -> None:
    client = new_client(base_url="https://example.invalid/api/", responses=["ok"])
    assert client.request("users") == "ok"


def test_request_uses_the_configured_timeout() -> None:
    sender = build_sender(responses=["ok"])
    client = Client(Config(base_url="https://example.invalid/", timeout=2.5), sender=sender)
    client.request("users")
    assert sender.calls[0][1] == 2.5


def test_retries_exhaust_and_raise() -> None:
    client = Client(Config(base_url="https://example.invalid/", retries=1), sender=failing_sender)
    with pytest.raises(ConnectionError):
        client.request_with_retries("a")


def test_build_sender_returns_a_callable() -> None:
    assert callable(build_sender(responses=["a", "b"]))
