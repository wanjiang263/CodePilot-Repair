"""客户端配置。"""

from __future__ import annotations

from httpclient.errors import ConfigError

MIN_TIMEOUT = 0.001


def validate_timeout(value: float) -> float:
    """超时必须是正数。"""
    if value < MIN_TIMEOUT:
        raise ConfigError(f"timeout must be >= {MIN_TIMEOUT}, got {value}")
    return value


def validate_retries(value: int) -> int:
    """重试次数必须是非负整数。"""
    if value < 0:
        raise ValueError(f"retries must be >= 0, got {value}")
    return value


class Config:
    """一次客户端配置的取值。"""

    def __init__(self, *, base_url: str, timeout: float = 5.0, retries: int = 2) -> None:
        self.base_url = base_url
        self.timeout = validate_timeout(timeout)
        self.retries = validate_retries(retries)

    def as_dict(self) -> dict[str, object]:
        """把配置摊平成可序列化的字典。"""
        return {"base_url": self.base_url, "timeout": self.timeout, "retries": self.retries}
