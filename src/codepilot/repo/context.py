"""只读的静态分析上下文：共享解析结果、符号和词法作用域。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
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
    """内存中的名称查找；不读 Git、不写数据库、不构造 NetworkX 图。"""

    def __init__(self, repository_id: str, parsed_files: Mapping[str, ParsedFile]) -> None:
        self.repository_id = repository_id
        self.files = tuple(
            SourceFile(
                path.replace("\\", "/"),
                parsed,
                extract_symbols(parsed, path=path, repository_id=repository_id),
            )
            for path, parsed in sorted(parsed_files.items())
        )
        self.symbols = tuple(symbol for file in self.files for symbol in file.symbols)
        self.by_id: dict[str, Symbol] = {}
        self.modules: dict[str, list[SourceFile]] = {}
        self.children: dict[str, list[Symbol]] = {}
        for file in self.files:
            self.modules.setdefault(file.module, []).append(file)
        for symbol in self.symbols:
            if symbol.id in self.by_id:
                raise ValueError(f"符号 ID 冲突（重复模块路径）: {symbol.qualified_name}")
            self.by_id[symbol.id] = symbol
            if symbol.parent_id is not None:
                self.children.setdefault(symbol.parent_id, []).append(symbol)

    def parents(self, symbol: Symbol) -> Iterator[Symbol]:
        current: Symbol | None = symbol
        while current is not None:
            yield current
            current = self.by_id.get(current.parent_id or "")

    def named(self, parent: Symbol, name: str) -> tuple[Symbol, ...]:
        return tuple(s for s in self.children.get(parent.id, ()) if s.name == name)

    def module_symbols(self, module: str) -> tuple[Symbol, ...]:
        return tuple(file.symbols[0] for file in self.modules.get(module, ()))

    def internal_module(self, module: str) -> bool:
        return module in self.modules or any(name.startswith(module + ".") for name in self.modules)

    def file_for(self, symbol: Symbol) -> SourceFile:
        return next(file for file in self.files if file.path == symbol.path)

    def module_for(self, symbol: Symbol) -> str:
        return module_name(symbol.path)
