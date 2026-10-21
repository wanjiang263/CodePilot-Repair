"""``X-API-Key`` authentication for every non-static endpoint."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException, status

from codepilot.config import Settings

__all__ = ["api_key_dependency", "assert_api_key_configured", "verify_api_key"]

#: A deliberately low floor. It is not a password policy for humans; it prevents
#: the "set it to 'x' and expose a loopback port through a tunnel" failure.
MIN_API_KEY_LENGTH = 16


def assert_api_key_configured(settings: Settings) -> None:
    value = settings.api_key.get_secret_value() if settings.api_key is not None else ""
    if not value.strip():
        raise RuntimeError(
            "未配置 CODEPILOT_API_KEY，拒绝启动。\n"
            "请在 .env 中设置 CODEPILOT_API_KEY=<一个足够长的随机串>。\n"
            "本服务不提供无鉴权模式。"
        )
    if len(value.strip()) < MIN_API_KEY_LENGTH:
        raise RuntimeError(
            f"CODEPILOT_API_KEY 太短（至少 {MIN_API_KEY_LENGTH} 个字符），拒绝启动。"
        )


def verify_api_key(provided: str | None, settings: Settings) -> None:
    expected = settings.api_key
    if expected is None:  # pragma: no cover - create_app refuses this state first
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务未配置 API key",
        )
    if provided is None or not secrets.compare_digest(provided, expected.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少或错误的 X-API-Key",
            headers={"WWW-Authenticate": "X-API-Key"},
        )


def api_key_dependency(settings: Settings) -> Callable[..., None]:
    """Bind the expected key to one concrete ``Settings`` instance.

    Using ``Depends(get_settings)`` here would read the process singleton while
    ``create_app(my_settings)`` validated a different object.
    """

    def verify(
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        verify_api_key(x_api_key, settings)

    return verify
