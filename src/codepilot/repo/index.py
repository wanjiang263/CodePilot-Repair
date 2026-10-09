"""Repository Index：扫描 → 解析 → 提取 → 持久化（R-15）与增量重建（R-16）。

这一层把 R-04…R-12 的内存静态分析接到 F-12/F-13 的存储层上。关键语义：

**变化判定**（02 §7.7）：以 ``(repository_id, path, sha256)`` 为准。文件内容不变
就只更新扫描计数，**不重新解析**——R-16 的验收断言依赖这一点，解析器调用
次数是可观测的。

**边的重建范围**：提取只覆盖本次解析的文件（变化集）。持久化时：

1. 变化集与已删除文件的**旧 symbol 的出边**全部删除，用新提取的边替换；
2. 指向已不存在 symbol 的边（无论源在哪个文件）按种类收敛：
   IMPORT / CALL 改写为"未解析"形态（``target_id=None``、confidence=low），
   INHERIT / REFERENCE / TEST 删除。这不是折中，而是**与全量重索引收敛**：
   全新解析一个引用了已删除名字的源文件时，IMPORT/CALL 提取器总是产出一条
   未解析边（名字仍在源码里），而 INHERIT/REFERENCE/TEST 只在能解析到仓库内
   目标时才产出边。已知边界：一条 import 曾解析到**多个**同名目标、其中一部分
   被删时，剩余边的 confidence 不会重算（全量重索引会因目标数变化而调整档位）
   ——同名重复定义部分删除是罕见情形，漂移随该源文件下一次变化自愈；
3. 未变化文件的出边**不重建**——它们的源文件没被重新解析，而目标 symbol id
   是 ``sha1(repository_id + qualified_name + kind)``，目标没改名/换 kind 时
   依然有效。这是"只重新解析变化的文件"（04 Phase 1 验收）的必然边界：
   未变化文件指向**新出现** symbol 的边要等它自己变化时才会补上。

**TEST 边是例外**：它按"源在变化集 **或** 目标在变化集"合并重建——既有边与
新提取边按键取最强 confidence。否则一个生产文件改名函数后，全量索引会给
未变化测试文件补上的 low（文件名约定）关联在增量结果里持续缺失，而 TEST 边
是回归测试选择（related_tests）的输入，缺失是静默的漏检。已知边界：未变化
测试文件对新 symbol 的 **medium** 关联（import + 测试名包含 symbol 名）在该
测试文件自身变化前不会出现——medium 需要该文件的 import 绑定，而增量不重
新解析它；low（文件名约定）会立即补上。

**边的去重**：``uq_edge_identity`` 不含行号，而同一 ``(source, target, name,
kind)`` 在源文件里可以出现多次（两处调用同一个函数）。持久化保留最强
confidence 中最早的一处；内存中的完整边集合不去重（golden test 比对的就是
内存结果）。

**解析失败的文件不中断索引**（R-05）：记 ``parse_error``，仍贡献可识别的
symbol——这是 R-05 已经定下的语义，这里原样承接。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from codepilot.clock import utcnow
from codepilot.observability.logging import get_logger
from codepilot.repo.calls import extract_calls
from codepilot.repo.context import AnalysisContext
from codepilot.repo.edges import extract_edges
from codepilot.repo.imports import ImportResolver
from codepilot.repo.models import (
    Confidence,
    GraphEdge,
    Symbol,
    SymbolKind,
    confidence_to_score,
    score_to_confidence,
)
from codepilot.repo.parser import ParsedFile, PythonParser, default_parser
from codepilot.repo.scanner import RepositoryScanner, ScannedFile
from codepilot.repo.symbols import file_id, module_name
from codepilot.repo.test_links import extract_test_links
from codepilot.repo.workspace import Workspace
from codepilot.storage.models import FileNode, SymbolRow
from codepilot.storage.models import GraphEdge as GraphEdgeRow
from codepilot.storage.repository import Stores

log = get_logger(__name__)

__all__ = ["IndexStats", "Indexer", "RepositoryIndex"]

_TEST_KINDS = ("test_function", "test_method")
_CONFIDENCE_RANK: dict[Confidence, int] = {"high": 2, "medium": 1, "low": 0}


@dataclass(frozen=True, slots=True)
class IndexStats:
    """一次索引运行的可观测结果（``codepilot index`` 与测试都用它）。"""

    repository_id: str
    base_commit: str
    incremental: bool
    files: int
    symbols_by_kind: dict[str, int]
    edges_by_kind_confidence: dict[tuple[str, str], int]
    tests: int
    scan: dict[str, int]
    parsed_paths: tuple[str, ...]
    """本次真正调用过解析器的 ``.py`` 文件。R-16 验收断言它就是变化集。"""
    changed_paths: tuple[str, ...]
    removed_paths: tuple[str, ...]
    reused_paths: tuple[str, ...]
    parse_errors: tuple[str, ...]
    duration_seconds: float


class Indexer:
    """执行一次完整或增量索引。解析器与扫描器可注入（测试计数用）。"""

    def __init__(
        self,
        parser: PythonParser | None = None,
        scanner: RepositoryScanner | None = None,
    ) -> None:
        self.parser = parser or default_parser()
        self.scanner = scanner or RepositoryScanner()

    def index(self, stores: Stores, workspace: Workspace) -> IndexStats:
        """索引 ``workspace``，幂等：重复运行不产生重复记录。

        事务边界在调用方（``session_scope``）：本方法中途抛异常时，调用方的
        回滚会把半个索引整个丢掉，不会留下"文件已写、边没写"的中间态。
        """
        started = time.monotonic()
        repository_id = workspace.repository_id
        root = Path(workspace.path)

        scan = self.scanner.scan(root)
        stored_files = {f.path: f for f in stores.files.list_by_repo(repository_id)}
        incremental = bool(stored_files)

        changed: list[ScannedFile] = []
        reused: list[ScannedFile] = []
        for scanned_file in scan.files:
            previous = stored_files.get(scanned_file.path)
            if previous is not None and previous.sha256 == scanned_file.sha256:
                reused.append(scanned_file)
            else:
                changed.append(scanned_file)
        scanned_paths = {f.path for f in scan.files}
        removed_paths = sorted(path for path in stored_files if path not in scanned_paths)

        parsed_files: dict[str, ParsedFile] = {}
        parse_errors: list[str] = []
        for scanned_file in changed:
            if not scanned_file.path.endswith(".py"):
                continue
            raw = (root / scanned_file.path).read_bytes()
            parsed = self.parser.parse(raw, path=scanned_file.path)
            parsed_files[scanned_file.path] = parsed
            if parsed.parse_error:
                parse_errors.append(f"{scanned_file.path}: {parsed.parse_error}")

        known = self._load_reused_symbols(stores, repository_id, [f.path for f in reused])
        context = AnalysisContext(repository_id, parsed_files, known_symbols=known)

        new_symbol_ids: set[str] = set()
        file_rows: list[dict[str, object]] = []
        symbol_rows: list[dict[str, object]] = []
        for scanned_file in changed:
            module = module_name(scanned_file.path) if scanned_file.path.endswith(".py") else None
            maybe_parsed = parsed_files.get(scanned_file.path)
            file_rows.append(
                {
                    "id": file_id(repository_id, scanned_file.path),
                    "repository_id": repository_id,
                    "path": scanned_file.path,
                    "module": module,
                    "size": scanned_file.size,
                    "sha256": scanned_file.sha256,
                    "line_count": scanned_file.line_count,
                    "is_test": scanned_file.is_test,
                    "parse_error": maybe_parsed.parse_error if maybe_parsed else None,
                }
            )
        for source_file in context.files:
            for symbol in source_file.symbols:
                new_symbol_ids.add(symbol.id)
                symbol_rows.append(_symbol_row(repository_id, symbol))

        all_edges = _extract_all_edges(context)
        edge_rows = _edge_rows(
            repository_id,
            tuple(edge for edge in all_edges if edge.kind != "TEST"),
            new_symbol_ids,
        )
        computed_test_edges = tuple(edge for edge in all_edges if edge.kind == "TEST")

        changed_file_ids = [file_id(repository_id, f.path) for f in changed]
        removed_file_ids = [file_id(repository_id, path) for path in removed_paths]
        affected_file_ids = [*changed_file_ids, *removed_file_ids]
        old_symbol_ids = set(stores.symbols.list_ids_for_files(affected_file_ids))

        # repositories 必须先写：files/symbols/edges 都有指向它的外键，
        # 而本方法可能运行在启用外键的 SQLite 上（F-11 默认开启）。
        stores.repositories.upsert(
            repository_id=repository_id,
            url=workspace.source.normalized,
            base_commit=workspace.base_commit,
            indexed_at=utcnow(),
        )
        stores.files.upsert_many(file_rows)
        stores.symbols.replace_for_files(affected_file_ids, symbol_rows)
        stores.edges.replace_for_sources(
            repository_id, sorted(old_symbol_ids | new_symbol_ids), edge_rows
        )
        if removed_paths:
            stores.files.delete_by_paths(repository_id, removed_paths)
        dangling_deleted, dangling_unresolved = _resolve_dangling(stores, repository_id)
        _merge_test_edges(
            stores, repository_id, computed_test_edges, old_symbol_ids | new_symbol_ids
        )

        stats = IndexStats(
            repository_id=repository_id,
            base_commit=workspace.base_commit,
            incremental=incremental,
            files=len(stores.files.list_by_repo(repository_id)),
            symbols_by_kind=stores.symbols.count_by_kind(repository_id),
            edges_by_kind_confidence={
                (kind, score_to_confidence(score)): count
                for (kind, score), count in stores.edges.count_by_kind_confidence(
                    repository_id
                ).items()
            },
            tests=sum(
                count
                for kind, count in stores.symbols.count_by_kind(repository_id).items()
                if kind in _TEST_KINDS
            ),
            scan=scan.counts(),
            parsed_paths=tuple(sorted(parsed_files)),
            changed_paths=tuple(f.path for f in changed),
            removed_paths=tuple(removed_paths),
            reused_paths=tuple(f.path for f in reused),
            parse_errors=tuple(sorted(parse_errors)),
            duration_seconds=time.monotonic() - started,
        )
        log.info(
            "repo.index.done",
            repository_id=repository_id,
            incremental=incremental,
            changed=len(changed),
            removed=len(removed_paths),
            reused=len(reused),
            dangling_edges_deleted=dangling_deleted,
            dangling_edges_unresolved=dangling_unresolved,
            duration_seconds=round(stats.duration_seconds, 3),
        )
        return stats

    def _load_reused_symbols(
        self, stores: Stores, repository_id: str, reused_paths: list[str]
    ) -> list[Symbol]:
        """未变化文件的 symbol 从持久化结果读回，作为无解析树的 known 符号。

        增量上下文用它们做跨文件名称解析（变化文件 import 未变化模块），
        提取器不会遍历它们（没有树）。
        """
        if not reused_paths:
            return []
        file_rows = stores.files.list_by_repo(repository_id)
        path_by_file_id = {row.id: row.path for row in file_rows}
        reused_file_ids = {file_id(repository_id, path) for path in reused_paths}
        known: list[Symbol] = []
        for row in stores.symbols.list_by_repo(repository_id):
            if row.file_id not in reused_file_ids:
                continue
            known.append(_symbol_object(row, path_by_file_id[row.file_id]))
        known.sort(key=lambda s: (s.path, s.start_line, s.qualified_name))
        return known


class RepositoryIndex:
    """一个 ``repository_id`` 的持久化索引查询接口（接口冻结清单 R 簇）。

    只读。每次方法调用都是一次新查询——调用方决定 session 的生命周期，
    本对象不缓存行，避免"查出来的和库里不是同一份"。
    """

    def __init__(self, stores: Stores, repository_id: str) -> None:
        self._stores = stores
        self.repository_id = repository_id

    def exists(self) -> bool:
        return self._stores.repositories.get(self.repository_id) is not None

    def files(self) -> list[FileNode]:
        return self._stores.files.list_by_repo(self.repository_id)

    def symbols(self, *, kind: str | None = None) -> list[SymbolRow]:
        rows = self._stores.symbols.list_by_repo(self.repository_id)
        rows.sort(key=lambda r: (r.qualified_name, r.id))
        return [row for row in rows if kind is None or row.kind == kind]

    def edges(self, *, kind: str | None = None) -> list[GraphEdgeRow]:
        rows = self._stores.edges.list_by_repo(self.repository_id)
        rows.sort(key=lambda r: (r.kind, r.source_id, r.target_id or "", r.target_name or ""))
        return [row for row in rows if kind is None or row.kind == kind]

    def tests(self) -> list[SymbolRow]:
        return self.symbols(kind="test_function") + self.symbols(kind="test_method")

    def symbol_objects(self) -> list[Symbol]:
        """``SymbolRow`` → 内存 ``Symbol``（补上 ``path``，图查询与历史归属用）。"""
        path_by_file_id = {row.id: row.path for row in self.files()}
        result = [
            _symbol_object(row, path_by_file_id[row.file_id])
            for row in self._stores.symbols.list_by_repo(self.repository_id)
        ]
        result.sort(key=lambda s: (s.qualified_name, s.id))
        return result


def _symbol_object(row: SymbolRow, path: str) -> Symbol:
    return Symbol(
        name=row.name,
        qualified_name=row.qualified_name,
        # 表里 kind 存成 str，读回时它必然是 SymbolKind 之一（写入侧已约束）。
        # 用 cast 显式表达这层信任，而非 blanket type: ignore。
        kind=cast("SymbolKind", row.kind),
        id=row.id,
        file_id=row.file_id,
        path=path,
        start_line=row.start_line,
        end_line=row.end_line,
        signature=row.signature,
        docstring=row.docstring,
        decorators=tuple(row.decorators),
        parent_id=row.parent_id,
    )


def _symbol_row(repository_id: str, symbol: Symbol) -> dict[str, object]:
    return {
        "id": symbol.id,
        "file_id": symbol.file_id,
        "repository_id": repository_id,
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "kind": symbol.kind,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "signature": symbol.signature,
        "docstring": symbol.docstring,
        "decorators": list(symbol.decorators),
        "parent_id": symbol.parent_id,
    }


def _resolve_dangling(stores: Stores, repository_id: str) -> tuple[int, int]:
    """处理指向已不存在 symbol 的边，返回 ``(删除数, 改写为未解析数)``。

    收敛规则（与全量重索引产出一致，见模块 docstring）：

    - IMPORT / CALL：按 ``(source, target_name, kind)`` 分组，保留行号最早的
      一条，改写为 ``target_id=None``、confidence=low 的未解析边——这正是
      重新解析该源文件会产出的东西；同组其余行删除。
    - INHERIT / REFERENCE / TEST：删除。这些种类只在目标可解析时才会被
      提取器产出。
    """
    dangling = stores.edges.list_dangling_targets(repository_id)
    if not dangling:
        return 0, 0
    delete_ids: list[str] = []
    rewrite_groups: dict[tuple[str, str, str], list[GraphEdgeRow]] = {}
    for row in dangling:
        if row.kind in ("IMPORT", "CALL"):
            key = (row.source_id, row.target_name or "", row.kind)
            rewrite_groups.setdefault(key, []).append(row)
        else:
            delete_ids.append(row.id)

    synthesized: list[dict[str, object]] = []
    for (source_id, target_name, kind), rows in sorted(rewrite_groups.items()):
        delete_ids.extend(row.id for row in rows)
        new_id = _edge_id(repository_id, source_id, None, target_name, kind)
        if stores.edges.get(new_id) is not None:
            # 已存在一条等价的未解析边（例如同名调用本就未解析）：不重复插入。
            continue
        first_line = min(row.line for row in rows)
        synthesized.append(
            {
                "id": new_id,
                "repository_id": repository_id,
                "source_id": source_id,
                "target_id": None,
                "target_name": target_name,
                "kind": kind,
                "confidence": confidence_to_score("low"),
                "line": first_line,
            }
        )
    deleted = stores.edges.delete_by_ids(repository_id, delete_ids)
    stores.edges.replace_for_sources(repository_id, [], synthesized)
    return deleted, len(synthesized)


def _merge_test_edges(
    stores: Stores,
    repository_id: str,
    computed: tuple[GraphEdge, ...],
    changed_source_ids: set[str],
) -> None:
    """合并重建 TEST 边集合（规则见模块 docstring 的"TEST 边是例外"）。

    ``computed`` 是本次提取的全部 TEST 边：变化源的是全强度（含调用证据的
    high），未变化源的只有 low（medium 需要未解析文件的 import 绑定，见
    docstring 边界）。与既有边按键合并，取最强 confidence、并列取最早行号，
    然后整体替换 TEST 集合——保证幂等，也让变化源被降级的边真正降下去。
    """
    merged: dict[tuple[str, str, str], tuple[float, int]] = {}
    for row in stores.edges.list_by_repo(repository_id):
        if row.kind != "TEST" or row.source_id in changed_source_ids:
            continue
        assert row.target_id is not None  # TEST 边由提取器产出时必有目标
        merged[(row.source_id, row.target_id, row.target_name or "")] = (
            row.confidence,
            row.line,
        )
    for edge in computed:
        if edge.source_id not in changed_source_ids and edge.target_id not in changed_source_ids:
            continue
        assert edge.target_id is not None
        key = (edge.source_id, edge.target_id, edge.target_name)
        score = confidence_to_score(edge.confidence)
        previous = merged.get(key)
        if previous is None or (score, -edge.line) > (previous[0], -previous[1]):
            merged[key] = (score, edge.line)

    stores.edges.delete_by_kind(repository_id, "TEST")
    rows = [
        {
            "id": _edge_id(repository_id, source_id, target_id, target_name, "TEST"),
            "repository_id": repository_id,
            "source_id": source_id,
            "target_id": target_id,
            "target_name": target_name,
            "kind": "TEST",
            "confidence": score,
            "line": line,
        }
        for (source_id, target_id, target_name), (score, line) in sorted(merged.items())
    ]
    stores.edges.replace_for_sources(repository_id, [], rows)


def _extract_all_edges(context: AnalysisContext) -> tuple[GraphEdge, ...]:
    """对本次解析的文件跑全部四类边提取。"""
    imports = ImportResolver(context)
    calls = extract_calls(context, imports)
    structural = extract_edges(context, imports)
    tests = extract_test_links(context, imports, calls)
    return (*imports.edges(), *calls, *structural, *tests)


def _edge_rows(
    repository_id: str, edges: tuple[GraphEdge, ...], changed_source_ids: set[str]
) -> list[dict[str, object]]:
    """内存边 → 持久化行：过滤到变化集源、按唯一约束去重、生成稳定 id。"""
    best: dict[tuple[str, str | None, str, str], GraphEdge] = {}
    for edge in edges:
        if edge.source_id not in changed_source_ids:
            continue
        key = (edge.source_id, edge.target_id, edge.target_name, edge.kind)
        previous = best.get(key)
        if previous is None or _better(edge, previous):
            best[key] = edge
    rows: list[dict[str, object]] = []
    ordered = sorted(
        best.items(),
        key=lambda item: (item[0][0], item[0][1] or "", item[0][2], item[0][3]),
    )
    for (source_id, target_id, target_name, kind), edge in ordered:
        rows.append(
            {
                "id": _edge_id(repository_id, source_id, target_id, target_name, kind),
                "repository_id": repository_id,
                "source_id": source_id,
                "target_id": target_id,
                "target_name": target_name,
                "kind": kind,
                "confidence": confidence_to_score(edge.confidence),
                "line": edge.line,
            }
        )
    return rows


def _better(candidate: GraphEdge, previous: GraphEdge) -> bool:
    """同一约束键保留最强 confidence；并列时保留行号更早的一处。"""
    rank = (_CONFIDENCE_RANK[candidate.confidence], -candidate.line)
    return rank > (_CONFIDENCE_RANK[previous.confidence], -previous.line)


def _edge_id(
    repository_id: str, source_id: str, target_id: str | None, target_name: str, kind: str
) -> str:
    payload = repository_id + source_id + (target_id or "") + target_name + kind
    return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()
