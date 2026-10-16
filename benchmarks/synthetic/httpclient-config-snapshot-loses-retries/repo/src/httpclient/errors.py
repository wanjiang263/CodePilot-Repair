"""HTTP 客户端的异常层级。"""

from __future__ import annotations


class HttpError(Exception):
    """本库所有错误的基类。"""


class ConfigError(HttpError):
    """配置不合法：缺超时、重试次数为负等。"""


class TransportError(HttpError):
    """底层传输失败。"""
