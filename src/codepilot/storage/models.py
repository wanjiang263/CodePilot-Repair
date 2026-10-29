"""数据库表（02 §18.1）。

九张表：repositories / files / symbols / edges / tasks / runs / attempts /
events / evaluations。

几个刻意的选择：

- 主键统一用字符串 id（与 ``schemas`` 中的 id 一致），不用自增整数——
  symbol id 是 ``sha1(repository_id + qualified_name + kind)``，跨进程可复现，
  自增整数做不到。
- ``events.seq`` 与 ``attempts.iteration`` 都不设自增，由写入方决定：
  事件序号要能在 SSE 断线续传时精确比较，自增主键做不到（回滚会跳号）。
- JSON 列直接存 dict，SQLite 与 PostgreSQL 都支持。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from codepilot.clock import utcnow
from codepilot.storage.base import Base

__all__ = [
    "Attempt",
    "Evaluation",
    "Event",
    "FileNode",
    "GraphEdge",
    "RepositoryRow",
    "Run",
    "SymbolRow",
    "Task",
]


class RepositoryRow(Base):
    """一个被索引过的仓库版本。"""

    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    base_commit: Mapped[str] = mapped_column(String(64))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    files: Mapped[list[FileNode]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("url", "base_commit", name="uq_repo_commit"),)


class IndexSemantics(Base):
    """Rebuildable index semantic identity; absent on legacy indexes until rebuilt."""

    __tablename__ = "index_semantics"
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64))


class FileNode(Base):
    """索引中的一个文件。``path`` 统一用小写感的 POSIX 分隔符。"""

    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(Text)
    module: Mapped[str | None] = mapped_column(Text, default=None)
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)

    parse_error: Mapped[str | None] = mapped_column(Text, default=None)
    """非 None 表示该文件语法错误、未能解析。索引继续，不中断。"""

    repository: Mapped[RepositoryRow] = relationship(back_populates="files")
    symbols: Mapped[list[SymbolRow]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("repository_id", "path", name="uq_file_repo_path"),)


class SymbolRow(Base):
    """函数 / 方法 / 类 / 模块级符号。"""

    __tablename__ = "symbols"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(Text)
    qualified_name: Mapped[str] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)

    signature: Mapped[str | None] = mapped_column(Text, default=None)
    docstring: Mapped[str | None] = mapped_column(Text, default=None)
    decorators: Mapped[list[str]] = mapped_column(JSON, default=list)
    parent_id: Mapped[str | None] = mapped_column(String(64), default=None)

    file: Mapped[FileNode] = relationship(back_populates="symbols")

    __table_args__ = (Index("ix_symbol_repo_qname", "repository_id", "qualified_name"),)


class GraphEdge(Base):
    """符号之间的有向边。

    ``target_id`` 可以为空：调用点无法解析到仓库内唯一目标时（例如同名方法
    超过 5 个被抑制），只记录 ``target_name`` 并降低 confidence。
    "__table_args__" 中的唯一约束防止增量重建时产生重复边。
    """

    __tablename__ = "edges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    target_name: Mapped[str | None] = mapped_column(Text, default=None)

    kind: Mapped[str] = mapped_column(String(16))
    """CALL / INHERIT / IMPORT / TEST / REFERENCE。"""

    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    line: Mapped[int] = mapped_column(Integer, default=0)
    """边在**源文件**中的行号（02 §7.1）。同一对节点在同一文件中可能有多条
    调用边；持久化按 ``uq_edge_identity`` 去重，保留最强 confidence 中最早的一处。"""

    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "source_id",
            "target_id",
            "target_name",
            "kind",
            name="uq_edge_identity",
        ),
    )


class Task(Base):
    """一个待修复任务。与运行分离：同一个任务可以跑多次（不同配置、不同 seed）。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    repository_id: Mapped[str] = mapped_column(String(64), index=True)
    issue: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16))

    config_hash: Mapped[str] = mapped_column(String(64), default="")
    """本次运行配置的 hash。消融实验靠它区分配置组。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    runs: Mapped[list[Run]] = relationship(back_populates="task", cascade="all, delete-orphan")


class Run(Base):
    """一次运行。"""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)

    status: Mapped[str] = mapped_column(String(16), default="running")
    stop_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    code_version: Mapped[str] = mapped_column(Text, default="")
    """运行时的 git commit 与是否有未提交修改。实验追溯必需（02 §18.1）。"""

    task: Mapped[Task] = relationship(back_populates="runs")
    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    # events / evaluations 与 run 之间**必须**声明 relationship，即使平时只用
    # 外键查询。SQLAlchemy 的工作单元只按 relationship 推导插入顺序；少了它，
    # 同一个 session 里一起 add 的 run 和 event 会被判为互不依赖，event 可能
    # 先于 run 落库，在开启外键的 SQLite 上直接报 IntegrityError。
    events: Mapped[list[Event]] = relationship(back_populates="run", cascade="all, delete-orphan")
    evaluations: Mapped[list[Evaluation]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Attempt(Base):
    """一轮修复。

    ``(run_id, iteration)`` 唯一：同一轮不允许写两次，避免恢复时把
    半途的结果和完整结果混在一起。
    """

    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    iteration: Mapped[int] = mapped_column(Integer)

    patch_hash: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="")
    artifact_dir: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (UniqueConstraint("run_id", "iteration", name="uq_attempt_run_iter"),)

    run: Mapped[Run] = relationship(back_populates="attempts")


class Event(Base):
    """事件流。JSONL 与这张表内容一致，两个都写是为了既能流式读取又能查询。"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)

    node: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),)

    run: Mapped[Run] = relationship(back_populates="events")


class Evaluation(Base):
    """评测结果。与 run 一对一，但分开存：同一个 run 可能被多种 benchmark 口径评过。"""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    benchmark: Mapped[str] = mapped_column(String(64), index=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (UniqueConstraint("run_id", "benchmark", name="uq_eval_run_benchmark"),)

    run: Mapped[Run] = relationship(back_populates="evaluations")
