"""structlog 配置。

一条规则：**日志不是事件流**。需要被回放、被断言、被写进 Run Report 的东西
走 ``EventSink``；这里的日志只服务于"人现在正盯着终端"的场景。

这个分工避免了一个常见退化：为了排查方便，把工具调用参数、代码片段、
prompt 全文都打进日志，然后日志变成又一个必须脱敏的数据出口。
"""

from __future__ import annotations

import logging
import sys

import structlog

__all__ = ["configure_logging", "get_logger"]

_configured = False


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """配置根 logger 与 structlog。

    ``json_output`` 供容器环境使用；本地默认走带颜色的可读格式。
    重复调用是安全的（幂等）。
    """
    global _configured

    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=numeric, force=True)

    renderer = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """取一个 logger。未配置时按默认参数自动配置一次。"""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]
