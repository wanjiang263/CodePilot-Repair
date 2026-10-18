"""NO_PROXY 归一化的回归测试（2026-10-18 真实 GLM 事故）。

事故：Windows 会话把 ``NO_PROXY`` 写成 ``...,[::1]``，httpx 0.28 在
``trust_env=True`` 时把每个条目交给 ``URLPattern``，``[::1]`` 被解析成
``host='['`` + ``port=':1]'``，抛 ``httpx.InvalidURL``，整次真实运行在发出
第一个请求前崩溃。这里锁住修复后的行为：归一化、可构造 client、环境还原。
"""

from __future__ import annotations

import os

import httpx

from codepilot.net import build_http_client, normalize_no_proxy, sanitized_proxy_environment


def test_normalize_strips_bracketed_ipv6() -> None:
    assert normalize_no_proxy("localhost,127.0.0.1,::1,[::1]") == "localhost,127.0.0.1,::1"


def test_normalize_drops_unparseable_entries() -> None:
    assert normalize_no_proxy("<local>,*.example.com,.foo,10.0.0.0/8, [fe80::1]:8080 ") == (
        "*.example.com,.foo,fe80::1"
    )


def test_client_construction_tolerates_bracketed_ipv6(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1,[::1]")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7899")
    with build_http_client(base_url="https://example.invalid") as client:
        assert isinstance(client, httpx.Client)


def test_environment_is_restored(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "a.example,[::1]")
    original = os.environ["NO_PROXY"]
    with sanitized_proxy_environment():
        assert os.environ["NO_PROXY"] == "a.example,::1"
    assert os.environ["NO_PROXY"] == original
