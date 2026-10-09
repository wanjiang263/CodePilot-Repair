"""全局测试配置。

职责（04 通用完成标准 + 03 §22）：
- Docker 不可用时，自动跳过所有 ``@pytest.mark.docker`` 测试，而不是让整个 suite 失败；
- 提供共享 fixture 的注册点。

真实 LLM 测试（``@pytest.mark.llm``）不在本文件里自动跳过：它们必须被显式地
``-m "not llm"`` 排除，或由开发者主动运行（见 ``scripts/`` 下的验收脚本）。
默认 ``addopts`` 刻意不写 ``-m``，保证 ``uv run pytest`` 在没有 Docker 的机器上
也能全绿——Docker 测试靠下面的 hook 跳过，llm 测试靠 marker 显式选择。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from codepilot.storage.db import (
    create_engine_from_url,
    create_session_factory,
    init_db,
)
from codepilot.storage.repository import Stores

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@lru_cache(maxsize=1)
def docker_available() -> bool:
    """检测 Docker 是否真的可用：既要有 CLI，daemon 也要能响应。

    只检查 ``shutil.which("docker")`` 会在"装了 CLI 但 daemon 没起"时误判，
    导致 docker 测试失败而不是跳过。
    """
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Docker 不可用时跳过 docker 测试，并在报告里给出原因。"""
    if docker_available():
        return
    skip_docker = pytest.mark.skip(reason="Docker 不可用（CLI 缺失或 daemon 未运行），跳过本测试")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip_docker)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Iterator[Path]:
    """一个干净的临时目录，用于需要写文件的测试（workspace、artifact 等）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    yield workspace


@pytest.fixture
def db_engine() -> Iterator[Engine]:
    """每个测试一个独立的内存数据库。

    用 ``sqlite://`` + ``StaticPool``（见 ``create_engine_from_url``）：
    默认的 SQLite 内存库是"每连接一个空库"，不加 StaticPool 的话建好的表
    在下一次取连接时就没了。
    """
    engine = create_engine_from_url("sqlite://")
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(db_engine)


@pytest.fixture
def stores(session_factory: sessionmaker[Session]) -> Iterator[Stores]:
    """数据访问层。

    结束时不 commit，只 rollback。理由有二：

    1. 每个测试用的是独立的内存库，没有需要提交到别处的东西；
    2. 有测试**故意**制造 flush 失败（例如验证唯一约束生效）。那种情况下
       session 已经处于 deactive 状态，再 commit 会在 teardown 里抛
       ``PendingRollbackError``，把一次预期的失败变成一个看起来毫不相关的错误。
    """
    session = session_factory()
    try:
        yield Stores(session)
    finally:
        session.rollback()
        session.close()
