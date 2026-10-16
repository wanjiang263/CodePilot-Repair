"""``httpclient.errors`` 的现有测试。"""

from __future__ import annotations

from httpclient.errors import ConfigError, HttpError, TransportError


def test_hierarchy() -> None:
    assert issubclass(ConfigError, HttpError)
    assert issubclass(TransportError, HttpError)


def test_messages_survive() -> None:
    assert "bad" in str(ConfigError("bad input"))
