"""数据访问层（02 §18.1）。

设计约定：

- 每个 store 在构造时接收一个 ``Session``，自己**不创建** session，也不提交。
  事务边界由调用方（``session_scope``）决定，否则一个失败的回滚会顺手把
  别的模块已经写好的数据一起丢掉。
- 索引类写入用"先删后插"而不是逐行 upsert：增量索引的语义本来就是
  "删除并重建与这些文件相关的边"（02 §7.7），而且批量插入比逐行 merge
  快一到两个数量级——一个中型仓库有几万个 symbol。
- 查询返回 ORM 行对象。需要领域对象时由上层转换，这一层不做业务判断。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, insert, select
from sqlalchemy.orm import Session

from codepilot.clock import utcnow
from codepilot.storage.models import (
    Attempt,
    Evaluation,
    Event,
    FileNode,
    GraphEdge,
    IndexSemantics,
    RepositoryRow,
    Run,
    SymbolRow,
    Task,
)

__all__ = [
    "AttemptStore",
    "EdgeStore",
    "EvaluationStore",
    "EventStore",
    "FileStore",
    "RepositoryStore",
    "RunStore",
    "Stores",
    "SymbolStore",
    "TaskStore",
]


class RepositoryStore:
    """``repositories`` 表。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(
        self,
        *,
        repository_id: str,
        url: str,
        base_commit: str,
        indexed_at: datetime | None = None,
    ) -> RepositoryRow:
        row = self._s.get(RepositoryRow, repository_id)
        if row is None:
            row = RepositoryRow(
                id=repository_id, url=url, base_commit=base_commit, indexed_at=indexed_at
            )
            self._s.add(row)
        else:
            row.url = url
            row.base_commit = base_commit
            if indexed_at is not None:
                row.indexed_at = indexed_at
        self._s.flush()
        return row

    def get(self, repository_id: str) -> RepositoryRow | None:
        return self._s.get(RepositoryRow, repository_id)

    def index_semantics(self, repository_id: str) -> str | None:
        row = self._s.get(IndexSemantics, repository_id)
        return row.fingerprint if row is not None else None

    def set_index_semantics(self, repository_id: str, fingerprint: str) -> None:
        self._s.merge(IndexSemantics(repository_id=repository_id, fingerprint=fingerprint))
        self._s.flush()

    def find_by_url_commit(self, url: str, base_commit: str) -> RepositoryRow | None:
        stmt = select(RepositoryRow).where(
            RepositoryRow.url == url, RepositoryRow.base_commit == base_commit
        )
        return self._s.execute(stmt).scalar_one_or_none()

    def mark_indexed(self, repository_id: str, when: datetime | None = None) -> None:
        row = self._s.get(RepositoryRow, repository_id)
        if row is None:
            raise KeyError(f"repository not found: {repository_id}")
        row.indexed_at = when or utcnow()


class FileStore:
    """``files`` 表。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert_many(self, rows: Iterable[dict[str, object]]) -> None:
        """按主键 upsert。

        文件数量远小于 symbol 数量，逐行处理是可以接受的，而且能保留
        ``parse_error`` 这类"本次没解析但上次有值"的字段语义。
        """
        for data in rows:
            file_id = str(data["id"])
            row = self._s.get(FileNode, file_id)
            if row is None:
                self._s.add(FileNode(**data))
            else:
                for key, value in data.items():
                    setattr(row, key, value)
        self._s.flush()

    def get(self, file_id: str) -> FileNode | None:
        return self._s.get(FileNode, file_id)

    def get_by_path(self, repository_id: str, path: str) -> FileNode | None:
        stmt = select(FileNode).where(
            FileNode.repository_id == repository_id, FileNode.path == path
        )
        return self._s.execute(stmt).scalar_one_or_none()

    def list_by_repo(self, repository_id: str) -> list[FileNode]:
        stmt = (
            select(FileNode).where(FileNode.repository_id == repository_id).order_by(FileNode.path)
        )
        return list(self._s.execute(stmt).scalars())

    def delete_by_paths(self, repository_id: str, paths: Sequence[str]) -> list[str]:
        """删除文件并返回被删的 file id（调用方据此清理 symbol 与边）。"""
        if not paths:
            return []
        stmt = select(FileNode.id).where(
            FileNode.repository_id == repository_id, FileNode.path.in_(paths)
        )
        ids = list(self._s.execute(stmt).scalars())
        if ids:
            self._s.execute(delete(FileNode).where(FileNode.id.in_(ids)))
        return ids


class SymbolStore:
    """``symbols`` 表。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def replace_for_files(self, file_ids: Sequence[str], rows: Iterable[dict[str, object]]) -> None:
        """删除这些文件的全部 symbol，再插入新的。

        增量索引只重建受影响文件的 symbol；未被重新解析的文件其 symbol 保持原样。
        """
        if file_ids:
            self._s.execute(delete(SymbolRow).where(SymbolRow.file_id.in_(file_ids)))
        materialized = list(rows)
        if materialized:
            self._s.execute(insert(SymbolRow), materialized)
        self._s.flush()

    def get(self, symbol_id: str) -> SymbolRow | None:
        return self._s.get(SymbolRow, symbol_id)

    def get_by_qualified_name(self, repository_id: str, qualified_name: str) -> list[SymbolRow]:
        """返回列表而不是单值：同名重复定义是合法的（02 §7.2 规则）。"""
        stmt = select(SymbolRow).where(
            SymbolRow.repository_id == repository_id,
            SymbolRow.qualified_name == qualified_name,
        )
        return list(self._s.execute(stmt).scalars())

    def find_by_name(self, repository_id: str, name: str, limit: int = 50) -> list[SymbolRow]:
        """按裸名模糊匹配，供 ``find_symbol`` 工具使用。"""
        stmt = (
            select(SymbolRow)
            .where(
                SymbolRow.repository_id == repository_id,
                SymbolRow.name.ilike(f"%{name}%"),
            )
            .order_by(func.length(SymbolRow.qualified_name))
            .limit(limit)
        )
        return list(self._s.execute(stmt).scalars())

    def list_by_repo(self, repository_id: str) -> list[SymbolRow]:
        stmt = select(SymbolRow).where(SymbolRow.repository_id == repository_id)
        return list(self._s.execute(stmt).scalars())

    def list_by_file(self, file_id: str) -> list[SymbolRow]:
        stmt = select(SymbolRow).where(SymbolRow.file_id == file_id).order_by(SymbolRow.start_line)
        return list(self._s.execute(stmt).scalars())

    def list_ids_for_files(self, file_ids: Sequence[str]) -> list[str]:
        """一批文件的全部 symbol id。增量索引在删除前取出，用于清理相关的边。"""
        if not file_ids:
            return []
        stmt = select(SymbolRow.id).where(SymbolRow.file_id.in_(file_ids))
        return list(self._s.execute(stmt).scalars())

    def count_by_kind(self, repository_id: str) -> dict[str, int]:
        stmt = (
            select(SymbolRow.kind, func.count())
            .where(SymbolRow.repository_id == repository_id)
            .group_by(SymbolRow.kind)
        )
        # 不能用 dict(result)：SQLAlchemy 的 Result 迭代出的是 Row 对象，
        # 它满足序列协议但不满足 mapping 协议，dict() 会直接抛 TypeError。
        return {kind: count for kind, count in self._s.execute(stmt)}  # noqa: C416


class EdgeStore:
    """``edges`` 表。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def replace_for_sources(
        self, repository_id: str, source_ids: Sequence[str], rows: Iterable[dict[str, object]]
    ) -> None:
        if source_ids:
            self._s.execute(
                delete(GraphEdge).where(
                    GraphEdge.repository_id == repository_id,
                    GraphEdge.source_id.in_(source_ids),
                )
            )
        from itertools import batched

        for batch in batched(rows, 1000):
            self._s.execute(insert(GraphEdge), list(batch))
        self._s.flush()

    def list_by_repo(self, repository_id: str) -> list[GraphEdge]:
        stmt = select(GraphEdge).where(GraphEdge.repository_id == repository_id)
        return list(self._s.execute(stmt).scalars())

    def out_edges(self, source_id: str) -> list[GraphEdge]:
        """调用者 / 子类 / import 的出发边。"""
        stmt = select(GraphEdge).where(GraphEdge.source_id == source_id)
        return list(self._s.execute(stmt).scalars())

    def in_edges(self, target_id: str) -> list[GraphEdge]:
        """谁调用了它。``target_id`` 为空（未解析到目标）的边不会出现在这里。"""
        stmt = select(GraphEdge).where(GraphEdge.target_id == target_id)
        return list(self._s.execute(stmt).scalars())

    def in_edges_by_name(self, repository_id: str, target_name: str) -> list[GraphEdge]:
        """按名字找调用方。用于目标未解析但名字已知的情形。"""
        stmt = select(GraphEdge).where(
            GraphEdge.repository_id == repository_id,
            GraphEdge.target_name == target_name,
        )
        return list(self._s.execute(stmt).scalars())

    def count_by_kind_confidence(self, repository_id: str) -> dict[tuple[str, float], int]:
        """按 (kind, confidence) 分组计数。``codepilot index`` 的输出与
        Phase 1 golden test 都依赖它。"""
        stmt = (
            select(GraphEdge.kind, GraphEdge.confidence, func.count())
            .where(GraphEdge.repository_id == repository_id)
            .group_by(GraphEdge.kind, GraphEdge.confidence)
        )
        return {(kind, conf): count for kind, conf, count in self._s.execute(stmt)}

    def get(self, edge_id: str) -> GraphEdge | None:
        return self._s.get(GraphEdge, edge_id)

    def delete_by_ids(self, repository_id: str, edge_ids: Sequence[str]) -> int:
        if not edge_ids:
            return 0
        # DML 的 execute 运行时返回 CursorResult（带 rowcount），但桩把 Session.execute
        # 声明为 Result；cast 补上这个已知的类型缺口，不改变运行时行为。
        result = cast(
            "CursorResult[Any]",
            self._s.execute(
                delete(GraphEdge).where(
                    GraphEdge.repository_id == repository_id,
                    GraphEdge.id.in_(edge_ids),
                )
            ),
        )
        self._s.flush()
        return result.rowcount or 0

    def delete_by_kind(self, repository_id: str, kind: str) -> int:
        """删除某仓库下指定种类的全部边。索引层用它整体替换 TEST 边集合。"""
        result = cast(
            "CursorResult[Any]",
            self._s.execute(
                delete(GraphEdge).where(
                    GraphEdge.repository_id == repository_id,
                    GraphEdge.kind == kind,
                )
            ),
        )
        self._s.flush()
        return result.rowcount or 0

    def list_dangling_targets(self, repository_id: str) -> list[GraphEdge]:
        """``target_id`` 指向已不存在 symbol 的边（增量重建后由索引层处理）。"""
        valid = select(SymbolRow.id).where(SymbolRow.repository_id == repository_id)
        stmt = select(GraphEdge).where(
            GraphEdge.repository_id == repository_id,
            GraphEdge.target_id.is_not(None),
            GraphEdge.target_id.notin_(valid),
        )
        return list(self._s.execute(stmt).scalars())


class TaskStore:
    """``tasks`` 表。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(
        self,
        *,
        task_id: str,
        repository_id: str,
        issue: str,
        mode: str,
        config_hash: str = "",
    ) -> Task:
        row = self._s.get(Task, task_id)
        if row is None:
            row = Task(
                id=task_id,
                repository_id=repository_id,
                issue=issue,
                mode=mode,
                config_hash=config_hash,
                created_at=utcnow(),
            )
            self._s.add(row)
        else:
            row.issue = issue
            row.config_hash = config_hash
        self._s.flush()
        return row

    def get(self, task_id: str) -> Task | None:
        return self._s.get(Task, task_id)

    def list_by_repo(self, repository_id: str) -> list[Task]:
        stmt = select(Task).where(Task.repository_id == repository_id)
        return list(self._s.execute(stmt).scalars())


class RunStore:
    """``runs`` 表。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def create(
        self,
        *,
        run_id: str,
        task_id: str,
        code_version: str = "",
        started_at: datetime | None = None,
    ) -> Run:
        row = Run(
            id=run_id,
            task_id=task_id,
            status="running",
            code_version=code_version,
            started_at=started_at or utcnow(),
        )
        self._s.add(row)
        self._s.flush()
        return row

    def get(self, run_id: str) -> Run | None:
        return self._s.get(Run, run_id)

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        stop_reason: str | None,
        usage: dict[str, object],
        finished_at: datetime | None = None,
    ) -> None:
        row = self._s.get(Run, run_id)
        if row is None:
            raise KeyError(f"run not found: {run_id}")
        row.status = status
        row.stop_reason = stop_reason
        row.usage = dict(usage)
        row.finished_at = finished_at or utcnow()

    def list_by_task(self, task_id: str) -> list[Run]:
        stmt = select(Run).where(Run.task_id == task_id).order_by(Run.started_at)
        return list(self._s.execute(stmt).scalars())


class AttemptStore:
    """``attempts`` 表。``(run_id, iteration)`` 唯一。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(
        self,
        *,
        run_id: str,
        iteration: int,
        patch_hash: str = "",
        status: str = "",
        artifact_dir: str = "",
    ) -> None:
        stmt = select(Attempt).where(Attempt.run_id == run_id, Attempt.iteration == iteration)
        row = self._s.execute(stmt).scalar_one_or_none()
        if row is None:
            self._s.add(
                Attempt(
                    run_id=run_id,
                    iteration=iteration,
                    patch_hash=patch_hash,
                    status=status,
                    artifact_dir=artifact_dir,
                )
            )
        else:
            row.patch_hash = patch_hash
            row.status = status
            row.artifact_dir = artifact_dir
        self._s.flush()

    def list_by_run(self, run_id: str) -> list[Attempt]:
        stmt = select(Attempt).where(Attempt.run_id == run_id).order_by(Attempt.iteration)
        return list(self._s.execute(stmt).scalars())


class EventStore:
    """``events`` 表。``seq`` 由本类分配，保证每 run 内连续且单调。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def next_seq(self, run_id: str) -> int:
        stmt = select(func.max(Event.seq)).where(Event.run_id == run_id)
        current = self._s.execute(stmt).scalar()
        return (current or 0) + 1

    def append(
        self,
        *,
        run_id: str,
        node: str,
        type: str,
        payload: dict[str, object] | None = None,
        seq: int | None = None,
        timestamp: datetime | None = None,
    ) -> Event:
        row = Event(
            run_id=run_id,
            seq=seq if seq is not None else self.next_seq(run_id),
            node=node,
            type=type,
            payload=dict(payload or {}),
            timestamp=timestamp or utcnow(),
        )
        self._s.add(row)
        self._s.flush()
        return row

    def list_after(self, run_id: str, after_seq: int = 0) -> list[Event]:
        """SSE 断线续传用（02 §19：``?after_seq=``）。"""
        stmt = (
            select(Event).where(Event.run_id == run_id, Event.seq > after_seq).order_by(Event.seq)
        )
        return list(self._s.execute(stmt).scalars())


class EvaluationStore:
    """``evaluations`` 表。``(run_id, benchmark)`` 唯一。"""

    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(
        self,
        *,
        run_id: str,
        benchmark: str,
        resolved: bool,
        metrics: dict[str, object] | None = None,
    ) -> None:
        stmt = select(Evaluation).where(
            Evaluation.run_id == run_id, Evaluation.benchmark == benchmark
        )
        row = self._s.execute(stmt).scalar_one_or_none()
        if row is None:
            self._s.add(
                Evaluation(
                    run_id=run_id,
                    benchmark=benchmark,
                    resolved=resolved,
                    metrics=dict(metrics or {}),
                )
            )
        else:
            row.resolved = resolved
            row.metrics = dict(metrics or {})
        self._s.flush()

    def list_by_run(self, run_id: str) -> list[Evaluation]:
        stmt = select(Evaluation).where(Evaluation.run_id == run_id)
        return list(self._s.execute(stmt).scalars())

    def list_by_benchmark(self, benchmark: str) -> list[Evaluation]:
        stmt = select(Evaluation).where(Evaluation.benchmark == benchmark)
        return list(self._s.execute(stmt).scalars())


class Stores:
    """把各 store 挂在同一个 session 上，供调用方一次性构造。

    ``with session_scope(factory) as session: stores = Stores(session)``

    不用 ``@dataclass``：九个 store 都是构造时创建的，写成 dataclass 字段会
    让它们变成必填参数，必须由调用方逐个传入，反而更啰嗦。
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repositories = RepositoryStore(session)
        self.files = FileStore(session)
        self.symbols = SymbolStore(session)
        self.edges = EdgeStore(session)
        self.tasks = TaskStore(session)
        self.runs = RunStore(session)
        self.attempts = AttemptStore(session)
        self.events = EventStore(session)
        self.evaluations = EvaluationStore(session)
