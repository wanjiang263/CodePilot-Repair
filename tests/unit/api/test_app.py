"""F-18 的证明：无 key 时**拒绝启动**；错误 key 返回 401；正确 key 返回 200。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codepilot.api.app import create_app
from codepilot.config import Settings

KEY = "test-key-0123456789abcdef"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"api_key": KEY}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _client(**overrides: object) -> TestClient:
    return TestClient(create_app(_settings(**overrides)))


# --------------------------------------------------------------- 启动约束


def test_refuses_to_start_without_api_key() -> None:
    """不是"启动后拒绝请求"——监听上的端口就有被扫到的窗口期。"""
    with pytest.raises(RuntimeError, match="CODEPILOT_API_KEY"):
        create_app(Settings(api_key=None))


def test_refuses_to_start_with_blank_api_key() -> None:
    with pytest.raises(RuntimeError):
        create_app(_settings(api_key="   "))


def test_error_message_tells_you_what_to_do() -> None:
    with pytest.raises(RuntimeError) as info:
        create_app(Settings(api_key=None))
    assert ".env" in str(info.value)


# --------------------------------------------------------------- /health


def test_health_with_correct_key() -> None:
    response = _client().get("/health", headers={"X-API-Key": KEY})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_without_key_is_unauthorized() -> None:
    response = _client().get("/health")
    assert response.status_code == 401


def test_health_with_wrong_key_is_unauthorized() -> None:
    response = _client().get("/health", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_health_with_empty_key_header_is_unauthorized() -> None:
    response = _client().get("/health", headers={"X-API-Key": ""})
    assert response.status_code == 401


def test_health_response_leaks_nothing() -> None:
    """任何来自配置的字段都是给扫描器的免费信息。"""
    payload = _client().get("/health", headers={"X-API-Key": KEY}).json()
    assert set(payload) == {"status"}


def test_interactive_docs_are_disabled() -> None:
    """文档页本身是一个无需鉴权的静态页。"""
    client = _client()
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --------------------------------------------------------------- 默认绑定


def test_default_host_is_loopback() -> None:
    assert Settings().api_host == "127.0.0.1"


def test_default_port() -> None:
    assert Settings().api_port == 8000
