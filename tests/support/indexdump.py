"""索引等价性快照：数据库内容的全量、可比较的冻结视图。

R-16 的增量正确性、R-22 的集成回归都靠一条断言：**增量重建后的库必须与
"同一仓库状态下的一次全新全量索引"逐字段一致**。这条断言要成立，快照函数
只能有一份——两份会各自漂移，漂移的那份会让"等价"变成"两个都错但一样"。
所以它住在 ``tests/support``，被单测与集成测试共同引用。

``repositories.indexed_at`` 是唯一被排除的字段：它是时间戳，两次运行必然不同，
纳进来只会让等价性断言永远失败。其余 files / symbols / edges 逐字段入快照，
edges 不含数据库主键 ``id``（它是行代理键，全量与增量插入顺序不同会导致 id 不同，
但业务等价性看的是 ``(source, target, name, kind, confidence, line)``）。
"""

from __future__ import annotations

from codepilot.storage.repository import Stores

__all__ = ["dump_index"]


def dump_index(stores: Stores, repository_id: str) -> tuple:
    """返回 ``(files, symbols, edges)`` 三个已排序的元组集合，可直接用 ``==`` 比较。"""
    files = sorted(
        (f.id, f.path, f.module, f.size, f.sha256, f.line_count, f.is_test, f.parse_error)
        for f in stores.files.list_by_repo(repository_id)
    )
    symbols = sorted(
        (
            s.id,
            s.file_id,
            s.name,
            s.qualified_name,
            s.kind,
            s.start_line,
            s.end_line,
            s.signature,
            s.docstring,
            tuple(s.decorators),
            s.parent_id,
        )
        for s in stores.symbols.list_by_repo(repository_id)
    )
    edges = sorted(
        (
            e.source_id,
            e.target_id or "",
            e.target_name or "",
            e.kind,
            round(e.confidence, 6),
            e.line,
        )
        for e in stores.edges.list_by_repo(repository_id)
    )
    return files, symbols, edges
