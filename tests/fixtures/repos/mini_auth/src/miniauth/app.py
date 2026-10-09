"""装配层：演示多行装饰器与递归调用。"""

from __future__ import annotations

from miniauth.backends import AuthBackend, MemoryBackend
from miniauth.models import User
from miniauth.plugins import audited, retry
from miniauth.session import Session


@audited
@retry(
    times=5,
)
def build_session(backend: AuthBackend, name: str) -> Session | None:
    """按名字装出一个会话。

    两个装饰器，其中一个是**跨行**写的（``@retry(`` 与 ``)`` 不在同一行）
    ——R-07 要求装饰器列表能正确解析出 ``audited`` 与 ``retry`` 两项。
    """
    user = backend.load(name)
    if user is None:
        return None
    return Session(backend, user)


def count_down(n: int) -> int:
    """递归函数：R-09 要求 ``count_down`` 内部对自己的调用建出边。"""
    if n <= 0:
        return 0
    return 1 + count_down(n - 1)


def bootstrap() -> MemoryBackend:
    """建一个装了 demo 用户的后端。"""
    backend = MemoryBackend()
    backend.add(User(name="demo", roles=["admin"]), "hunter2")
    return backend
