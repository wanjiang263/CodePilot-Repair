"""R-21：mini_auth 索引 golden test（04 Phase 1 验收：逐字段比对，差异全部列出）。

比对对象是**内存索引结果**——与 ``Indexer.index`` 全量分支完全相同的管线
（scan → parse → ``AnalysisContext`` → 收集 symbols + 四类边），symbols 与 edges
都是**不去重**的完整集合。用**固定 repository_id**（``repo_mini_auth_golden``）
而不是 ``repository_id(source, commit)`` 的派生值：``Symbol.id =
sha1(repository_id + qualified_name + kind)``，派生值会依赖临时目录路径，
期望 JSON 里的 id 就没法写死。索引器不碰 git，所以直接对普通 fixture 目录跑。

期望文件 ``tests/fixtures/golden/mini_auth_index.json`` 的生成经过人工核对：
逐个 symbol / 逐条边对着 ``tests/fixtures/repos/mini_auth/`` 的源码核过
（``src/`` 前缀去除、``__init__.py`` → 包名、``<locals>`` 嵌套、多行装饰器、
INHERIT 边、TEST 三级强度、``self.backend.*`` 同名 low 候选）。生成/核对脚本见
``artifacts/r13-r18/gen_golden.py``（调试工具，非验收产物）。

失败时**不**只断言相等：把每一处差异（缺失、多余、逐字段不符）都列进断言消息，
这样红的时候能直接看到是哪条边、哪个字段错了，而不是"两个大列表不相等"。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from codepilot.repo.context import AnalysisContext
from codepilot.repo.index import _extract_all_edges
from codepilot.repo.models import GraphEdge, Symbol
from codepilot.repo.parser import TreeSitterParser
from codepilot.repo.scanner import RepositoryScanner

REPO_ID = "repo_mini_auth_golden"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "mini_auth"
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "mini_auth_index.json"

_SYMBOL_FIELDS = (
    "name",
    "qualified_name",
    "kind",
    "id",
    "file_id",
    "path",
    "start_line",
    "end_line",
    "signature",
    "docstring",
    "decorators",
    "parent_id",
)


def _index_fixture() -> tuple[list[Symbol], tuple[GraphEdge, ...]]:
    """跑与 ``Indexer.index`` 全量分支相同的内存管线，返回 (symbols, edges)。"""
    parser = TreeSitterParser()
    scan = RepositoryScanner().scan(FIXTURE)
    parsed = {
        f.path: parser.parse((FIXTURE / f.path).read_bytes(), path=f.path)
        for f in scan.files
        if f.path.endswith(".py")
    }
    context = AnalysisContext(REPO_ID, parsed)
    symbols = [symbol for source_file in context.files for symbol in source_file.symbols]
    return symbols, _extract_all_edges(context)


def _symbol_dict(symbol: Symbol) -> dict[str, object]:
    return {
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "kind": symbol.kind,
        "id": symbol.id,
        "file_id": symbol.file_id,
        "path": symbol.path,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
        "signature": symbol.signature,
        "docstring": symbol.docstring,
        "decorators": list(symbol.decorators),
        "parent_id": symbol.parent_id,
    }


def _edge_dict(edge: GraphEdge) -> dict[str, object]:
    return {
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "target_name": edge.target_name,
        "kind": edge.kind,
        "confidence": edge.confidence,
        "line": edge.line,
    }


def _edge_key(row: dict[str, object]) -> tuple:
    # target_id 可能为 None（未解析边）；排序时归一化为 "" 否则 None 与 str 不可比。
    return (
        row["source_id"],
        row["target_id"] or "",
        row["target_name"],
        row["kind"],
        row["confidence"],
        row["line"],
    )


def test_golden_index_matches_field_by_field() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert expected["repository_id"] == REPO_ID

    symbols, edges = _index_fixture()
    actual_symbols = [_symbol_dict(s) for s in symbols]
    actual_edges = [_edge_dict(e) for e in edges]

    problems: list[str] = []

    # ---- symbols：按 id 对齐，逐字段比对（id 由 repository_id+qname+kind 决定，唯一）----
    expected_by_id = {row["id"]: row for row in expected["symbols"]}
    actual_by_id = {row["id"]: row for row in actual_symbols}
    if len(expected_by_id) != len(expected["symbols"]):
        problems.append("期望 JSON 里出现了重复的 symbol id")
    if len(actual_by_id) != len(actual_symbols):
        problems.append("实际索引里出现了重复的 symbol id")

    for sid in sorted(expected_by_id.keys() - actual_by_id.keys()):
        row = expected_by_id[sid]
        problems.append(f"缺失 symbol: {row['qualified_name']} [{row['kind']}] ({row['path']})")
    for sid in sorted(actual_by_id.keys() - expected_by_id.keys()):
        row = actual_by_id[sid]
        problems.append(f"多余 symbol: {row['qualified_name']} [{row['kind']}] ({row['path']})")
    for sid in sorted(expected_by_id.keys() & actual_by_id.keys()):
        exp, act = expected_by_id[sid], actual_by_id[sid]
        for field in _SYMBOL_FIELDS:
            if exp[field] != act[field]:
                problems.append(
                    f"symbol {act['qualified_name']} 字段 {field} 不符: "
                    f"期望={exp[field]!r} 实际={act[field]!r}"
                )

    # ---- edges：不去重，用多重集合比对，重复条数差异也要报出来 ----
    expected_edges = Counter(_edge_key(row) for row in expected["edges"])
    actual_edge_counts = Counter(_edge_key(row) for row in actual_edges)
    for key in sorted(expected_edges.keys() - actual_edge_counts.keys()):
        problems.append(f"缺失 edge ×{expected_edges[key]}: {_fmt_edge(key)}")
    for key in sorted(actual_edge_counts.keys() - expected_edges.keys()):
        problems.append(f"多余 edge ×{actual_edge_counts[key]}: {_fmt_edge(key)}")
    for key in sorted(expected_edges.keys() & actual_edge_counts.keys()):
        if expected_edges[key] != actual_edge_counts[key]:
            problems.append(
                f"edge 条数不符（期望 {expected_edges[key]} / 实际 {actual_edge_counts[key]}）: "
                f"{_fmt_edge(key)}"
            )

    summary = (
        f"symbols 期望 {len(expected['symbols'])} / 实际 {len(actual_symbols)}；"
        f"edges 期望 {len(expected['edges'])} / 实际 {len(actual_edges)}"
    )
    assert not problems, (
        f"golden 索引不一致（{summary}），共 {len(problems)} 处差异:\n"
        + "\n".join(f"  - {p}" for p in problems)
    )


def _fmt_edge(key: tuple) -> str:
    source_id, target_id, target_name, kind, confidence, line = key
    target = target_id if target_id else f"<unresolved:{target_name}>"
    return f"{kind} {source_id[:8]}→{str(target)[:8]} name={target_name!r} [{confidence} @L{line}]"
