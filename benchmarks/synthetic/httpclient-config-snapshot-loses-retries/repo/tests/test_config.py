"""``httpclient.config`` 的现有测试。"""

from __future__ import annotations

from httpclient.config import Config, validate_retries


def test_defaults() -> None:
    config = Config(base_url="https://example.invalid/")
    assert config.timeout == 5.0
    assert config.retries == 2


def test_as_dict_carries_base_url() -> None:
    config = Config(base_url="https://example.invalid/", timeout=1.5)
    assert config.as_dict()["base_url"] == "https://example.invalid/"


def test_zero_retries_is_allowed() -> None:
    assert validate_retries(0) == 0


def test_positive_retries_pass_through() -> None:
    assert validate_retries(3) == 3
