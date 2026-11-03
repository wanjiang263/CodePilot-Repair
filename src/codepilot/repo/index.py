"""Content-incremental parsing with conservative complete association closure.

The database stores files/symbols/edges but no complete unresolved import bindings.
Therefore every update rebuilds all associations (including aliases, cycles and
previously unresolved imports). Warm parse trees are reused by content; a cold
indexer reparses unchanged sources to reconstruct bindings. No stale edge is kept
merely to claim locality. All database changes share the caller's transaction.

Phase29 T10 adds one bounded exception to the full rebuild: when a run changes no
symbol id anywhere (``old_symbol_ids == new_symbol_ids``, content-independent ids),
edge extraction and edge row replacement are restricted to the changed files —
for every other source the recomputed rows are provably identical to the stored
ones, so leaving them untouched is equivalence, not staleness. See the fast-path
comment in ``Indexer.index`` and
tests/unit/repo/test_phase29_index_incremental.py.

Phase33 T05 replaces the previous "any symbol-set change falls back to the full
rebuild" rule with an affected-closure rebuild: changed and removed files,
every file whose import bindings resolve (directly or through a re-export
chain) to a dirty module, every test file linked to a changed or removed file
by the counterpart-stem rule, and every file containing an unresolved attribute
call whose method-name candidate set changed. Extraction and edge-row
replacement are restricted to that closure;
``IndexStats.edge_closure_paths`` reports it (empty means the whole graph
was replaced). Equivalence against a full rebuild is still pinned by dump_index
comparisons in tests/unit/repo/.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from codepilot.clock import utcnow
from codepilot.observability.logging import get_logger
from codepilot.repo.calls import extract_calls
from codepilot.repo.context import AnalysisContext
from codepilot.repo.edges import extract_edges

# _absolute_module is package-private but deliberately shared here so the
# closure walk and the resolver can never disagree about what a raw import
# string means for a given file.
from codepilot.repo.imports import ImportResolver, _absolute_module
from codepilot.repo.models import (
    Confidence,
    GraphEdge,
    Symbol,
    SymbolKind,
    confidence_to_score,
    score_to_confidence,
)
from codepilot.repo.parser import ParsedFile, PythonParser, default_parser, descendants
from codepilot.repo.scanner import RepositoryScanner, ScannedFile, is_test_path
from codepilot.repo.symbols import file_id, module_name
from codepilot.repo.test_links import extract_test_links
from codepilot.repo.workspace import Workspace
from codepilot.storage.models import FileNode, SymbolRow
from codepilot.storage.models import GraphEdge as GraphEdgeRow
from codepilot.storage.repository import Stores

log = get_logger(__name__)

__all__ = ["IndexScanIncompleteError", "IndexStats", "Indexer", "RepositoryIndex"]

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
    """本次实际解析路径；冷启动或缓存逐出会包含未变化文件。"""
    changed_paths: tuple[str, ...]
    removed_paths: tuple[str, ...]
    reused_paths: tuple[str, ...]
    parse_errors: tuple[str, ...]
    duration_seconds: float
    association_mode: str = "complete_closure_v2"
    parse_reused_paths: tuple[str, ...] = ()
    fast_mode: bool = False
    """本次运行是否走了 Phase29 T10 的增量快边路径（完整重建时为 False）。"""
    edge_closure_paths: tuple[str, ...] = ()
    """本次边替换实际覆盖的闭包文件路径（Phase33 T05）。

    空元组表示全图替换（冷启动、语义指纹不匹配或完整重建）；非空表示
    ``replace_for_sources`` 只删插了这些文件（加上被删文件的旧符号）的边。
    fast_mode 与闭包模式都会填这个字段，测试用它断言"只重建受影响边"。
    """


class IndexScanIncompleteError(OSError):
    """扫描读到不可读路径，索引在写库前中止：既有已提交索引原样保留。

    继承 ``OSError``：这个失败本来就是以普通 ``OSError`` 抛出的，所有按
    ``except OSError`` 分类/透传的调用方（CLI、eval harness）语义不变。
    类型化只为给调用方一个可靠判别依据——retrieval runner 据此把它归类为
    既有的 ``code="unreadable"`` 环境失败，而不是落进通用执行异常。
    """

    def __init__(self, message: str, *, unreadable: tuple[str, ...]) -> None:
        super().__init__(message)
        self.unreadable = unreadable


class Indexer:
    """执行一次完整或增量索引。解析器与扫描器可注入（测试计数用）。"""

    def __init__(
        self,
        parser: PythonParser | None = None,
        scanner: RepositoryScanner | None = None,
        progress: Callable[..., Any] | None = None,
        cache_max_entries: int = 128,
        cache_max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._parsed: OrderedDict[tuple[str, str, str], tuple[ParsedFile, int]] = OrderedDict()
        self.progress = progress
        self.parser = parser or default_parser()
        self.scanner = scanner or RepositoryScanner()
        # 解析树缓存的容量上界（条数与源字节）。默认值与历史行为完全一致；
        # 仅 Phase28 剖析脚本用更大的上界观察缓存命中率，不改变默认语义。
        self._cache_max_entries = cache_max_entries
        self._cache_max_bytes = cache_max_bytes

    def _progress(self, phase: str, **counts: int) -> None:
        if self.progress is not None:
            try:
                self.progress("preparation_progress", phase=phase, counts=counts)
            except Exception:
                log.exception("index.progress_unavailable")

    def index(self, stores: Stores, workspace: Workspace) -> IndexStats:
        """索引 ``workspace``，幂等：重复运行不产生重复记录。

        事务边界在调用方（``session_scope``）：本方法中途抛异常时，调用方的
        回滚会把半个索引整个丢掉，不会留下"文件已写、边没写"的中间态。
        """
        started = time.monotonic()
        repository_id = workspace.repository_id
        root = Path(workspace.path)

        scan = self.scanner.scan(root)
        self._progress("scanned", files=len(scan.files))
        if scan.unreadable:
            raise IndexScanIncompleteError(
                "index scan incomplete; retain previous committed index: " + repr(scan.unreadable),
                unreadable=scan.unreadable,
            )
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
        removed_paths = sorted(set(stored_files) - scanned_paths)

        import importlib.metadata

        fingerprint_input = (
            type(self.parser).__module__
            + type(self.parser).__qualname__
            + type(self.scanner).__module__
            + type(self.scanner).__qualname__
        ).encode()
        for filename in (
            "index.py",
            "parser.py",
            "context.py",
            "symbols.py",
            "imports.py",
            "calls.py",
            "edges.py",
            "test_links.py",
            "scanner.py",
        ):
            fingerprint_input += Path(__file__).with_name(filename).read_bytes()
        for package in ("tree-sitter", "tree-sitter-python"):
            try:
                fingerprint_input += importlib.metadata.version(package).encode()
            except importlib.metadata.PackageNotFoundError:
                fingerprint_input += b"unavailable"
        fingerprint = hashlib.sha256(fingerprint_input).hexdigest()

        # Initialised before the closure: the verified-snapshot return path never
        # reaches the incremental guard below.
        fast_mode = False
        closure_mode = False
        edge_closure_paths: tuple[str, ...] = ()

        def finish(
            reparsed: list[str],
            parse_reused: list[str],
            parse_errors: list[str],
            mode: str,
        ) -> IndexStats:
            return IndexStats(
                association_mode=mode,
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
                parsed_paths=tuple(sorted(reparsed)),
                parse_reused_paths=tuple(sorted(parse_reused)),
                changed_paths=tuple(f.path for f in changed),
                removed_paths=tuple(removed_paths),
                reused_paths=tuple(f.path for f in reused),
                parse_errors=tuple(sorted(parse_errors)),
                duration_seconds=time.monotonic() - started,
                fast_mode=fast_mode,
                edge_closure_paths=edge_closure_paths,
            )

        if (
            not changed
            and not removed_paths
            and stores.repositories.index_semantics(repository_id) == fingerprint
        ):
            self._progress("index_verified_reuse", files=len(scan.files))
            return finish(
                [],
                [],
                [
                    f"{row.path}: {row.parse_error}"
                    for row in stored_files.values()
                    if row.parse_error
                ],
                "verified_snapshot_v2",
            )

        parsed_files: dict[str, ParsedFile] = {}
        parse_errors: list[str] = []
        reparsed: list[str] = []
        parse_reused: list[str] = []
        # Complete associations are the safe closure when unresolved bindings
        # cannot be recovered from old edges. Cache is instance-local, bounded.
        cached_at_start = dict(self._parsed)
        for scanned_file in scan.files:
            if not scanned_file.path.endswith(".py"):
                continue
            key = (str(root.resolve()), scanned_file.path, scanned_file.sha256)
            cached = cached_at_start.get(key)
            if cached is not None:
                parsed = cached[0]
                if key in self._parsed:
                    self._parsed.move_to_end(key)
                parse_reused.append(scanned_file.path)
            else:
                raw = (root / scanned_file.path).read_bytes()
                if hashlib.sha256(raw).hexdigest() != scanned_file.sha256:
                    raise OSError("workspace changed during index scan")
                parsed = self.parser.parse(raw, path=scanned_file.path)
                reparsed.append(scanned_file.path)
                if len(raw) <= 1024 * 1024:
                    self._parsed[key] = (parsed, len(raw))
                    while (
                        len(self._parsed) > self._cache_max_entries
                        or sum(v[1] for v in self._parsed.values()) > self._cache_max_bytes
                    ):
                        self._parsed.popitem(last=False)
            parsed_files[scanned_file.path] = parsed
            if len(parsed_files) == 1 or len(parsed_files) % 100 == 0:
                self._progress(
                    "parsing",
                    parsed=len(reparsed),
                    parse_reused=len(parse_reused),
                    changed=len(changed),
                )
            if parsed.parse_error:
                parse_errors.append(f"{scanned_file.path}: {parsed.parse_error}")

        self._progress(
            "linking",
            sources=len(parsed_files),
            parsed=len(reparsed),
            parse_reused=len(parse_reused),
            changed=len(changed),
            reused=len(reused),
            removed=len(removed_paths),
        )
        context = AnalysisContext(repository_id, parsed_files)

        new_symbol_ids: set[str] = set()
        file_rows: list[dict[str, object]] = []
        symbol_rows: list[dict[str, object]] = []
        for scanned_file in scan.files:
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

        # Keep the rows, not just the ids: the affected-closure walk needs the
        # stored method names/kinds to detect candidate-set changes (Phase33).
        old_symbol_rows = stores.symbols.list_by_repo(repository_id)
        old_symbol_ids = {row.id for row in old_symbol_rows}
        removed_symbol_ids = old_symbol_ids - new_symbol_ids
        changed_path_set = {f.path for f in changed}

        # Incremental fast path (Phase29 T10): symbol ids are content-independent
        # (sha1 of repository + qualified_name + kind). When no symbol appeared or
        # disappeared anywhere, resolutions for unchanged sources are provably
        # identical to the stored rows — same parse, same qname->id map,
        # deterministic extractors — so edge *extraction* and edge *row
        # replacement* can be restricted to the changed files instead of
        # rebuilding the whole graph. Any symbol-set change (added, removed or
        # renamed symbol, removed file, new parse error) falls into the
        # affected-closure rebuild (Phase33 T05) or, when the semantics
        # fingerprint does not match, the conservative full rebuild. The
        # equivalence claim is pinned by
        # tests/support/indexdump.dump_index comparisons
        # (tests/unit/repo/test_phase29_index_incremental.py) and by the
        # semantic digests in docs/notes/phase29-index-profile.json.
        #
        # The stored rows only describe the current extractor semantics when the
        # persisted fingerprint matches: a legacy or stale-semantics index must
        # take the full rebuild (see the deleted-IMPORT case in
        # test_phase26_cold_index_rebuilds_old_semantics).
        semantics_match = stores.repositories.index_semantics(repository_id) == fingerprint
        fast_mode = (
            incremental
            and not removed_paths
            and old_symbol_ids == new_symbol_ids
            and semantics_match
        )
        if fast_mode:
            all_edges = _extract_all_edges(context, only_paths=frozenset(changed_path_set))
            if _import_bindings_changed(stores, all_edges, changed_path_set, context):
                # Phase30: a body-only edit can still retarget imports (re-export
                # swap, alias retarget). The changed file's own IMPORT target set
                # then differs from the stored one, and every downstream file that
                # resolved THROUGH the old target (two re-export hops away) now
                # has stale edges that the changed-file restriction cannot see.
                # Drop to the affected-closure rebuild below, which re-extracts
                # the whole importer/re-export chain instead of the full graph.
                fast_mode = False
        if fast_mode:
            context_paths = {source_file.path for source_file in context.files}
            edge_closure_paths = tuple(sorted(p for p in changed_path_set if p in context_paths))
            edge_recompute_sources = {
                symbol.id
                for source_file in context.files
                if source_file.path in changed_path_set
                for symbol in source_file.symbols
            } | removed_symbol_ids
        elif incremental and semantics_match:
            # Phase33 T05 affected-closure rebuild: replace edge rows only for
            # files that can provably differ from the stored ones (see
            # _affected_closure). Extraction runs on FULL-context import
            # bindings so re-export chains resolve exactly like a full rebuild;
            # only the emitted edge set is restricted to the closure.
            closure_mode = True
            imports_full = ImportResolver(context)
            old_methods, new_methods = _closure_source_maps(context, old_symbol_rows)
            affected = _affected_closure(
                context,
                imports_full,
                changed_path_set=changed_path_set,
                removed_paths=removed_paths,
                old_methods=old_methods,
                new_methods=new_methods,
            )
            affected_ids = {
                symbol.id
                for source_file in context.files
                if source_file.path in affected
                for symbol in source_file.symbols
            }
            calls = extract_calls(context, imports_full, only_paths=affected)
            all_edges = (
                *(edge for edge in imports_full.edges() if edge.source_id in affected_ids),
                *calls,
                *extract_edges(context, imports_full, only_paths=affected),
                *extract_test_links(context, imports_full, calls, only_paths=affected),
            )
            edge_closure_paths = tuple(sorted(affected))
            edge_recompute_sources = affected_ids | removed_symbol_ids
        else:
            all_edges = _extract_all_edges(context)
            edge_recompute_sources = old_symbol_ids | new_symbol_ids
        edge_rows = _edge_rows(
            repository_id,
            tuple(edge for edge in all_edges if edge.kind != "TEST"),
            edge_recompute_sources,
        )
        # Fast/closure path: the extractors above already restricted output to
        # the closure files, so every TEST source is inside
        # edge_recompute_sources and these rows are a pure insert.
        computed_test_edges = tuple(edge for edge in all_edges if edge.kind == "TEST")

        removed_file_ids = [file_id(repository_id, path) for path in removed_paths]
        affected_file_ids = [
            *[file_id(repository_id, f.path) for f in scan.files],
            *removed_file_ids,
        ]

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
        del symbol_rows, file_rows
        stores.edges.replace_for_sources(repository_id, sorted(edge_recompute_sources), edge_rows)
        del edge_rows, all_edges
        if removed_paths:
            stores.files.delete_by_paths(repository_id, removed_paths)
        dangling_deleted, dangling_unresolved = _resolve_dangling(stores, repository_id)
        _replace_test_edges(
            stores,
            repository_id,
            computed_test_edges,
            full_replace=not (fast_mode or closure_mode),
        )

        stores.repositories.set_index_semantics(repository_id, fingerprint)
        stats = finish(reparsed, parse_reused, parse_errors, "complete_closure_v2")
        log.info(
            "repo.index.done",
            repository_id=repository_id,
            incremental=incremental,
            fast_mode=fast_mode,
            closure_mode=closure_mode,
            edge_closure=len(edge_closure_paths),
            changed=len(changed),
            removed=len(removed_paths),
            reused=len(reused),
            dangling_edges_deleted=dangling_deleted,
            dangling_edges_unresolved=dangling_unresolved,
            duration_seconds=round(stats.duration_seconds, 3),
        )
        return stats


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


def _replace_test_edges(
    stores: Stores,
    repository_id: str,
    computed: tuple[GraphEdge, ...],
    *,
    full_replace: bool = True,
) -> None:
    """Write TEST edges. ``full_replace`` rebuilds the whole TEST partition;
    the incremental fast path instead replaces only the sources present in
    ``computed`` (already restricted to the changed files) and leaves untouched
    test files' rows — identical by the same proof as the non-TEST edges.

    Do not materialize the old graph, a second merge map and all row dictionaries
    at once. EdgeStore consumes this iterator in bounded transaction-local batches.
    """
    rows: Iterator[dict[str, object]] = (
        {
            "id": _edge_id(repository_id, edge.source_id, edge.target_id, edge.target_name, "TEST"),
            "repository_id": repository_id,
            "source_id": edge.source_id,
            "target_id": edge.target_id,
            "target_name": edge.target_name,
            "kind": "TEST",
            "confidence": confidence_to_score(edge.confidence),
            "line": edge.line,
        }
        for edge in computed
    )
    if full_replace:
        stores.edges.delete_by_kind(repository_id, "TEST")
        stores.edges.replace_for_sources(repository_id, [], rows)
    else:
        # Fast path: the main edge replace above already deleted every stored
        # row (any kind) of the changed sources, so these TEST rows are a pure
        # insert. Deleting by source_id here again would also wipe the fresh
        # non-TEST rows of the same sources.
        stores.edges.replace_for_sources(repository_id, [], rows)


def _extract_all_edges(
    context: AnalysisContext, only_paths: frozenset[str] | None = None
) -> tuple[GraphEdge, ...]:
    """Run all four extractors. ``only_paths`` (incremental fast path) restricts
    extraction to those files; see the fast-path proof in ``Indexer.index``."""
    imports = ImportResolver(context, only_paths=only_paths)
    calls = extract_calls(context, imports, only_paths=only_paths)
    structural = extract_edges(context, imports, only_paths=only_paths)
    tests = extract_test_links(context, imports, calls, only_paths=only_paths)
    return (*imports.edges(), *calls, *structural, *tests)


def _import_bindings_changed(
    stores: Stores,
    all_edges: tuple[GraphEdge, ...],
    changed_path_set: set[str],
    context: AnalysisContext,
) -> bool:
    """True when any changed file's resolved IMPORT targets moved (Phase30).

    The Phase29 fast path is sound only while a body-only edit cannot change
    what anything resolves to. That breaks for re-exports: swapping
    ``from pkg.a import value`` to ``from pkg.b import value`` keeps every
    symbol id, but downstream files whose imports resolve THROUGH the changed
    module now point at a different target. Comparing the freshly extracted
    IMPORT edges of the changed files against the stored rows for the same
    sources detects the retarget; any difference falls back to the full
    rebuild. The query is bounded by the changed files' symbols (out_edges is
    indexed by source_id), not by graph size.
    """
    changed_source_ids = {
        symbol.id
        for source_file in context.files
        if source_file.path in changed_path_set
        for symbol in source_file.symbols
    }
    if not changed_source_ids:
        return False
    new_bindings = {
        (edge.source_id, edge.target_id or "", edge.target_name)
        for edge in all_edges
        if edge.kind == "IMPORT" and edge.source_id in changed_source_ids
    }
    stored_bindings: set[tuple[str, str, str]] = set()
    for source_id in changed_source_ids:
        for row in stores.edges.out_edges(source_id):
            if row.kind == "IMPORT":
                stored_bindings.add((row.source_id, row.target_id or "", row.target_name or ""))
    return new_bindings != stored_bindings


def _closure_source_maps(
    context: AnalysisContext, old_rows: list[SymbolRow]
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    """新旧方法名 → id 集（属性调用候选集比较用，Phase33 T05）。

    镜像 calls.py 的 ``_methods_by_name`` 构建：只收 method/test_method。某个方法名的
    id 集变化时，任何文件里未解析属性调用（``obj.thing()``）的保守回退候选集都可能
    变化，闭包必须把含此类调用的文件拉进受影响集（见 ``_affected_closure``）。
    """
    old_methods: dict[str, set[str]] = {}
    for row in old_rows:
        if row.kind in ("method", "test_method"):
            old_methods.setdefault(row.name, set()).add(row.id)
    new_methods: dict[str, set[str]] = {}
    for symbol in context.symbols:
        if symbol.kind in {"method", "test_method"}:
            new_methods.setdefault(symbol.name, set()).add(symbol.id)
    return (
        {name: frozenset(ids) for name, ids in old_methods.items()},
        {name: frozenset(ids) for name, ids in new_methods.items()},
    )


def _affected_closure(
    context: AnalysisContext,
    imports: ImportResolver,
    *,
    changed_path_set: set[str],
    removed_paths: list[str],
    old_methods: Mapping[str, frozenset[str]],
    new_methods: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    """一次增量变化后，边行可能与存量不同的文件路径集合。

    四个通道与提取器的全部"绕过本文件内容"的边来源一一对应：

    1. 变更/删除文件自身。
    2. import 绑定传播（imports.py + resolution.py）：绑定了脏模块的文件。
       ``imports`` 必须是全上下文解析（不能 only_paths 受限），re-export 链才
       解析得动。fixpoint 把链折进脏集：hub 变更 → mid 因 ``from pkg.hub
       import x`` 入闭包并把 pkg.mid 折脏 → consumer 因绑定 pkg.mid 入闭包。
       包前缀规则（脏项以 mention + "." 开头）覆盖 ``import pkg`` 后走
       ``pkg.c.x`` 的 dotted 引用（resolution.py 裸模块导入特判）。mentions
       同时收文本 absolute（``_absolute_module``）与解析后的
       ``target_module``：模块被删除后 ``target_module`` 为 None，只有文本
       形式能命中脏集。
    3. counterpart stem（test_links.py 通道 (c)）：test_x.py 与 x.py 的 TEST
       边不经 import；x.py 变更/删除时对应测试文件必须入闭包。
    4. 属性调用候选（calls.py 的保守回退）：全局同名方法表变化（delta 非空）
       会改变任何文件里 ``obj.thing()`` 的候选集；扫描未入集文件的语法树，
       attribute 名命中 delta 即入闭包（过近似，安全侧）。

    通道 3/4 拉入的文件不再传播脏集：它们自身内容未变，下游对它们的解析
    也不变。返回集外的文件保留存量边行——全量重建会逐字节重现它们。
    """
    file_by_path = {source_file.path: source_file for source_file in context.files}
    affected = {path for path in changed_path_set if path in file_by_path}
    dirty = {
        module_name(path) for path in (*changed_path_set, *removed_paths) if path.endswith(".py")
    }

    mentions: dict[str, set[str]] = {}
    for binding in imports.bindings:
        source = context.by_id.get(binding.source_id)
        if source is None:
            continue
        bucket = mentions.setdefault(source.path, set())
        if binding.target_module:
            bucket.add(binding.target_module)
        absolute = _absolute_module(file_by_path[source.path], binding.module)
        if absolute is not None:
            bucket.add(absolute)

    grew = True
    while grew:
        grew = False
        for source_file in context.files:
            if source_file.path in affected:
                continue
            for module in mentions.get(source_file.path, ()):
                if module in dirty or any(d.startswith(module + ".") for d in dirty):
                    affected.add(source_file.path)
                    if source_file.path.endswith(".py"):
                        dirty.add(module_name(source_file.path))
                    grew = True
                    break

    changed_stems = {PurePosixPath(path).stem for path in (*changed_path_set, *removed_paths)}
    for source_file in context.files:
        if source_file.path in affected or not is_test_path(source_file.path):
            continue
        stem = PurePosixPath(source_file.path).stem
        if stem.startswith("test_"):
            counterpart = stem.removeprefix("test_")
        elif stem.endswith("_test"):
            counterpart = stem.removesuffix("_test")
        else:
            continue
        if counterpart in changed_stems:
            affected.add(source_file.path)

    delta = {
        name
        for name in set(old_methods) | set(new_methods)
        if old_methods.get(name, frozenset()) != new_methods.get(name, frozenset())
    }
    if delta:
        for source_file in context.files:
            if source_file.path in affected:
                continue
            for node in descendants(source_file.parsed.root):
                if node.kind != "call":
                    continue
                function = node.field("function")
                if function is None or function.kind != "attribute":
                    continue
                attribute = function.field("attribute")
                if attribute is not None and attribute.text in delta:
                    affected.add(source_file.path)
                    break

    return frozenset(affected)


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
