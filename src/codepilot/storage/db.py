"""数据库连接（02 §18.1）。

默认 SQLite（单机、零依赖），``DATABASE_URL`` 可切到 PostgreSQL。
两者都走 SQLAlchemy，所以上层代码不需要知道用的是哪个。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from codepilot.storage.base import Base

__all__ = [
    "create_engine_from_url",
    "create_session_factory",
    "init_db",
    "session_scope",
]


def create_engine_from_url(url: str, *, echo: bool = False) -> Engine:
    """按 URL 创建 Engine。

    SQLite 需要两处特殊处理：

    - ``check_same_thread=False``：API worker 线程与主线程会共用连接；
    - 内存库需要 ``StaticPool``，否则每个新连接都得到一个**全新的空库**，
      建好的表在下一个 session 里就消失了（测试里最常见的坑）。
    """
    if url.startswith("sqlite"):
        if ":memory:" in url or url.endswith("sqlite://"):
            engine = create_engine(
                url,
                echo=echo,
                future=True,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            engine = create_engine(
                url,
                echo=echo,
                future=True,
                connect_args={"check_same_thread": False},
            )
        _enable_sqlite_foreign_keys(engine)
        return engine
    return create_engine(url, echo=echo, future=True, pool_pre_ping=True)


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite 默认不强制外键，需要逐连接打开。"""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db(engine: Engine) -> None:
    """建表。

    V1 不使用迁移工具：表结构尚未定型，且历史数据随时可以从 artifacts 重建。

    这里对 ``models`` 做函数内导入，是为了触发"把表注册到 ``Base.metadata``"
    这个副作用。放在模块顶层会重新引入循环导入。
    """
    from codepilot.storage import models  # noqa: F401  (注册表定义的副作用)

    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """事务作用域：正常提交，异常回滚。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
