"""只读的静态分析上下文：共享解析结果、符号和词法作用域。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from codepilot.repo.models import Symbol
from codepilot.repo.parser import ParsedFile, SyntaxNode
from codepilot.repo.symbols import extract_symbols, module_name


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    parsed: ParsedFile
    symbols: tuple[Symbol, ...]

    @property
    def module(self) -> str:
        return self.symbols[0].qualified_name

    def scoped_nodes(self) -> Iterator[tuple[SyntaxNode, Symbol]]:
        """定义体属于新符号，默认值、装饰器和基类表达式属于外层。"""
        definitions = iter(self.symbols[1:])
        stack = [(self.parsed.root, self.symbols[0])]
        while stack:
            node, owner = stack.pop()
            if node.kind in {"class_definition", "function_definition"}:
                name = node.field("name")
                if name is not None and name.text:
                    current = next(definitions)
                    yield node, current
                    body = node.field("body")
                    for child in reversed(node.children):
                        stack.append(
                            (
                                child,
                                current if child.kind == "block" and body is not None else owner,
                            )
                        )
                    continue
            yield node, owner
            stack.extend((child, owner) for child in reversed(node.children))


class AnalysisContext:
    """内存中的名称查找；不读 Git、不写数据库、不构造 NetworkX 图。

    ``known_symbols`` 是**没有解析树**的仓库内符号（R-16 增量索引从持久化结果
    读回的未变化文件）。它们参与名称解析（``by_id`` / ``named`` /
    ``module_symbols`` / ``parents``），但提取器只遍历 ``files``——也就是本次
    真正解析过的文件。调用方必须保证 known 集合在每个文件内自洽（父链闭合），
    本类不验证这一点。
    """

    def __init__(
        self,
        repository_id: str,
        parsed_files: Mapping[str, ParsedFile],
        *,
        known_symbols: Iterable[Symbol] = (),
    ) -> None:
        self.repository_id = repository_id
        self.files = tuple(
            SourceFile(
                path.replace("\\", "/"),
                parsed,
                extract_symbols(parsed, path=path, repository_id=repository_id),
            )
            for path, parsed in sorted(parsed_files.items())
        )
        parsed_symbols = tuple(symbol for file in self.files for symbol in file.symbols)
        parsed_module_ids = {file.symbols[0].id for file in self.files}
        self.symbols = (*parsed_symbols, *tuple(known_symbols))
        self.by_id: dict[str, Symbol] = {}
        self._module_index: dict[str, list[Symbol]] = {}
        self.children: dict[str, list[Symbol]] = {}
        for file in self.files:
            self._module_index.setdefault(file.module, []).append(file.symbols[0])
        for symbol in self.symbols:
            if symbol.id in self.by_id:
                raise ValueError(f"符号 ID 冲突（重复模块路径）: {symbol.qualified_name}")
            self.by_id[symbol.id] = symbol
            if symbol.parent_id is not None:
                self.children.setdefault(symbol.parent_id, []).append(symbol)
            if symbol.kind == "module" and symbol.id not in parsed_module_ids:
                self._module_index.setdefault(symbol.qualified_name, []).append(symbol)

    def parents(self, symbol: Symbol) -> Iterator[Symbol]:
        current: Symbol | None = symbol
        while current is not None:
            yield current
            current = self.by_id.get(current.parent_id or "")

    def named(self, parent: Symbol, name: str) -> tuple[Symbol, ...]:
        return tuple(s for s in self.children.get(parent.id, ()) if s.name == name)

    def module_symbols(self, module: str) -> tuple[Symbol, ...]:
        return tuple(self._module_index.get(module, ()))

    def internal_module(self, module: str) -> bool:
        return module in self._module_index or any(
            name.startswith(module + ".") for name in self._module_index
        )

    def file_for(self, symbol: Symbol) -> SourceFile:
        """取 symbol 所在文件的解析视图。known 符号没有树，调用它是编程错误。"""
        for file in self.files:
            if file.path == symbol.path:
                return file
        raise LookupError(
            f"符号没有可用的解析树（增量上下文中的未变化文件）: {symbol.qualified_name}"
        )

    def module_for(self, symbol: Symbol) -> str:
        return module_name(symbol.path)
