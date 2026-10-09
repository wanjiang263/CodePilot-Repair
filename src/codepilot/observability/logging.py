"""structlog 配置。

一条规则：**日志不是事件流**。需要被回放、被断言、被写进 Run Report 的东西
走 ``EventSink``；这里的日志只服务于"人现在正盯着终端"的场景。

这个分工避免了一个常见退化：为了排查方便，把工具调用参数、代码片段、
prompt 全文都打进日志，然后日志变成又一个必须脱敏的数据出口。
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO, cast

import structlog

__all__ = ["configure_logging", "get_logger"]

_configured = False


class _LiveStderr:
    """把每次写入转发到"此刻的 ``sys.stderr``"，而不是配置时被固化的那个流对象。

    ``typer.testing.CliRunner`` 每次 invoke 会把 ``sys.stderr`` 换成它的捕获缓冲、
    invoke 结束就关闭它；pytest 的 ``capsys`` 也会临时换流。若把这些瞬时流对象按值
    存进 structlog 的全局 logger factory，之后**整个进程**的日志都会写向一个已关闭
    的缓冲，抛 ``ValueError: I/O operation on closed file``——而且是否触发取决于测试
    执行顺序（哪个测试第一次写下第一行日志），是典型的跨测试状态污染。代理在每次
    write/flush 时重新解析 ``sys.stderr``，永远跟随真实目标，与 logger 是否被缓存无关。

    不实现 ``__slots__``：structlog 用 ``weakref.WeakKeyDictionary`` 按文件对象存写锁，
    实例必须可弱引用。
    """

    def write(self, message: str) -> int:
        return sys.stderr.write(message)

    def flush(self) -> None:
        sys.stderr.flush()


_stderr = _LiveStderr()


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
        # 传活代理而非 sys.stderr 本身：见 _LiveStderr。cast 只是向 structlog 的
        # TextIO 注解声明它按 duck typing 提供 write/flush。
        logger_factory=structlog.PrintLoggerFactory(file=cast("TextIO", _stderr)),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """取一个 logger。未配置时按默认参数自动配置一次。"""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]
