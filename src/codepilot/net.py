"""httpx 客户端构造与代理环境变量归一化。

2026-10-18 的真实 GLM 实验暴露了一条会在**发出第一个请求之前**就崩溃的路径：
Windows 会话把 ``NO_PROXY`` 写成 ``...,[::1]``，而 httpx 0.28 在
``trust_env=True`` 时会把每个 NO_PROXY 条目交给 ``URLPattern``，于是
``[::1]`` 被解析成 ``host='['`` + ``port=':1]'``，抛
``httpx.InvalidURL: Invalid port: ':1]'``。整次运行因此记成
``harness_internal_error``，一个模型请求都没发出去。

这里只做一件很小但必要的事：在构造客户端的一瞬间规范化 NO_PROXY：

- 带方括号的 IPv6（``[::1]``、``[fe80::1]``）去掉方括号——httpx 能正确
  解析裸 IPv6；
- 丢弃无法安全解析的条目（``<local>``、URL、CIDR、带空白的垃圾等）；
- 去重、去空。

构造完成后立刻恢复原环境，避免污染其他代码。代理本身（``HTTP_PROXY`` /
``HTTPS_PROXY`` / ``ALL_PROXY``）不做任何改动，httpx 原有的 ``trust_env``
行为保持不变。
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from collections.abc import Iterator
from typing import Any

import httpx

__all__ = ["build_http_client", "normalize_no_proxy", "sanitized_proxy_environment"]

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

#: 裸主机名 / IPv4 / IPv6 / 通配符 / 域名后缀。冒号允许：host:port 与 IPv6 都要用。
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:*\-]+$")

#: 方括号包起来的 IPv6，可选端口；NO_PROXY 里的端口没有实际意义，丢弃。
_BRACKETED_IPV6 = re.compile(r"^\[([0-9A-Fa-f:.]+)\](?::\d+)?$")

#: 环境变量是进程级共享状态，构造客户端期间临时改写需要串行化。
_LOCK = threading.RLock()


def normalize_no_proxy(value: str) -> str:
    """把 NO_PROXY 规范成 httpx 一定能解析的条目列表。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            continue
        match = _BRACKETED_IPV6.match(token)
        if match is not None:
            token = match.group(1)
        elif token.startswith("[") and token.endswith("]"):
            token = token[1:-1].strip()
        if not token or not _SAFE_TOKEN.match(token):
            continue
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return ",".join(tokens)


@contextlib.contextmanager
def sanitized_proxy_environment() -> Iterator[None]:
    """在构造 httpx.Client 期间临时替换为规范化后的 NO_PROXY。"""
    with _LOCK:
        saved = {key: os.environ.get(key) for key in _PROXY_ENV_KEYS}
        try:
            for key in ("NO_PROXY", "no_proxy"):
                if key in os.environ:
                    os.environ[key] = normalize_no_proxy(os.environ[key])
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def build_http_client(**kwargs: Any) -> httpx.Client:
    """构造 ``httpx.Client``，并对 NO_PROXY 做一次安全归一化。

    ``kwargs`` 原样传给 httpx，因此调用点不需要关心代理环境变量的差异。
    """
    with sanitized_proxy_environment():
        return httpx.Client(**kwargs)
