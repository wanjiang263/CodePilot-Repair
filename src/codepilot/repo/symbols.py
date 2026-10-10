"""从已有解析树提取符号及属性（R-06 / R-07）。

提供 repository_id 时返回完整属性，并保留原有名称提取接口。
嵌套函数仍属于 ``function``，通过 qualified name 中的 ``<locals>`` 表达作用域。
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import PurePosixPath, PureWindowsPath

from codepilot.repo.models import Symbol, SymbolKind
from codepilot.repo.parser import ParsedFile, SyntaxNode
from codepilot.repo.scanner import is_test_path

__all__ = ["Symbol", "SymbolKind", "extract_symbols", "file_id", "module_name", "symbol_id"]


def symbol_id(repository_id: str, qualified_name: str, kind: SymbolKind) -> str:
    """02 §7.1 的稳定标识；不依赖行号或解析顺序。"""
    return hashlib.sha1(
        (repository_id + qualified_name + kind).encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def file_id(repository_id: str, path: str) -> str:
    """``FileNode`` 的稳定标识。索引层与符号提取必须用同一个公式，
    否则 ``Symbol.file_id`` 关联不上 ``files`` 表。"""
    return hashlib.sha1((repository_id + path).encode(), usedforsecurity=False).hexdigest()


def module_name(path: str) -> str:
    """按仓库相对路径推导模块名（02 §7.2）。

    仅移除仓库根部的 ``src/``；``__init__.py`` 使用所在包名。
    根部的 ``__init__.py`` 没有可推导的包路径，保留 ``__init__`` 作为模块名。
    接受两种路径分隔符，不读取仓库或执行其代码。
    """
    relative = PurePosixPath(path.replace("\\", "/"))
    if (
        relative.is_absolute()
        or PureWindowsPath(path).drive
        or ".." in relative.parts
        or relative.suffix != ".py"
    ):
        raise ValueError(f"需要仓库内的相对 Python 文件路径: {path!r}")

    parts = list(relative.with_suffix("").parts)
    if len(parts) > 1 and parts[0] == "src":
        parts.pop(0)
    if len(parts) > 1 and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def extract_symbols(
    parsed: ParsedFile, *, path: str, repository_id: str | None = None
) -> tuple[Symbol, ...]:
    """按源码先序返回模块及其全部类、函数、方法定义。

    测试符号要求文件满足现有测试路径约定，名称以 ``test_`` 开头，且直接
    属于模块或类作用域；函数内的嵌套函数始终是普通 ``function``。
    条件语句和装饰器包装不创建作用域，也不筛掉任何分支。
    ``parsed.errors`` 不影响遍历：语法错误文件仍能贡献可识别的定义。
    提供 ``repository_id`` 时补全 R-07 属性；省略时兼容 R-06 的名称提取契约。
    """
    module = module_name(path)
    is_test = is_test_path(path.replace("\\", "/"))
    path = path.replace("\\", "/")
    module_symbol = _symbol(
        module.rsplit(".", 1)[-1],
        module,
        "module",
        parsed.root,
        repository_id=repository_id,
        path=path,
        parent_id=None,
    )
    symbols = [module_symbol]
    occurrences: dict[str, int] = {}
    stack: list[tuple[SyntaxNode, Symbol, tuple[str, ...], int | None]] = [
        (parsed.root, module_symbol, (), None)
    ]

    while stack:
        node, parent, decorators, decorator_line = stack.pop()
        scope, scope_kind = parent.qualified_name, parent.kind
        if node.kind == "decorated_definition":
            definition = node.field("definition")
            if definition is not None:
                decorations = (
                    tuple(
                        child.text.removeprefix("@").strip()
                        for child in node.children
                        if child.kind == "decorator"
                    )
                    if repository_id is not None
                    else ()
                )
                stack.append(
                    (
                        definition,
                        parent,
                        decorations,
                        node.start_line if repository_id is not None else None,
                    )
                )
                continue
        if node.kind not in {"class_definition", "function_definition"}:
            stack.extend((child, parent, (), None) for child in reversed(node.children))
            continue

        name_node = node.field("name")
        if name_node is None or not name_node.text:
            # 容错树可能没有定义名；不为它制造空名称，但继续寻找可用定义。
            stack.extend((child, parent, (), None) for child in reversed(node.children))
            continue

        name = name_node.text
        kind = _definition_kind(node, name, scope_kind, is_test=is_test)
        prefix = scope if scope_kind in {"module", "class"} else f"{scope}.<locals>"
        base_name = f"{prefix}.{name}"
        count = occurrences.get(base_name, 0) + 1
        occurrences[base_name] = count
        qualified_name = base_name if count == 1 else f"{base_name}#{count}"
        current = _symbol(
            name,
            qualified_name,
            kind,
            node,
            repository_id=repository_id,
            path=path,
            parent_id=parent.id or None,
            decorators=decorators,
            start_line=decorator_line,
        )
        symbols.append(current)

        body = node.field("body")
        if body is not None:
            # 子定义继承已消歧的父名称，重复类/函数的子符号因此也不会碰撞。
            stack.append((body, current, (), None))

    return tuple(symbols)


def _symbol(
    name: str,
    qualified_name: str,
    kind: SymbolKind,
    node: SyntaxNode,
    *,
    repository_id: str | None,
    path: str,
    parent_id: str | None,
    decorators: tuple[str, ...] = (),
    start_line: int | None = None,
) -> Symbol:
    if repository_id is None:
        return Symbol(name, qualified_name, kind)
    body = node if kind == "module" else node.field("body")
    return Symbol(
        name,
        qualified_name,
        kind,
        id=symbol_id(repository_id, qualified_name, kind),
        file_id=file_id(repository_id, path),
        path=path,
        start_line=node.start_line if start_line is None else start_line,
        end_line=node.end_line,
        signature=None if kind == "module" else _signature(node, body),
        docstring=_docstring(body),
        decorators=decorators,
        parent_id=parent_id,
    )


def _signature(node: SyntaxNode, body: SyntaxNode | None) -> str:
    if body is None:
        return node.text.strip().removesuffix(":")
    # Tree-sitter 列号按 UTF-8 字节计数；不能直接作为 Unicode 字符串下标。
    lines = node.text.encode("utf-8").splitlines(keepends=True)
    row = body.start_line - node.start_line
    column = body.start_col - (node.start_col if row == 0 else 0)
    offset = sum(len(line) for line in lines[:row]) + column
    header = b"".join(lines)[:offset].decode("utf-8").rstrip()
    # 多行体前可能有行尾注释；冒号必须定位在语法 token，而非注释文本中。
    colon = next((child for child in reversed(node.children) if child.kind == ":"), None)
    if colon is not None:
        colon_row = colon.start_line - node.start_line
        colon_col = colon.start_col - (node.start_col if colon_row == 0 else 0)
        end = sum(len(line) for line in lines[:colon_row]) + colon_col
        header = b"".join(lines)[:end].decode("utf-8").rstrip()
    return header.removesuffix(":").rstrip()


def _docstring(body: SyntaxNode | None) -> str | None:
    if body is None:
        return None
    for statement in body.children:
        if statement.kind == "comment":
            continue
        if statement.kind != "expression_statement":
            return None
        literal = next(
            (n for n in statement.children if n.kind in {"string", "concatenated_string"}), None
        )
        if literal is None:
            return None
        try:
            value = ast.literal_eval(literal.text)
        except (SyntaxError, ValueError):
            return None
        return inspect.cleandoc(value) if isinstance(value, str) else None
    return None


def _definition_kind(
    node: SyntaxNode, name: str, scope_kind: SymbolKind, *, is_test: bool
) -> SymbolKind:
    if node.kind == "class_definition":
        return "class"
    is_test_name = is_test and name.startswith("test_")
    if scope_kind == "class":
        return "test_method" if is_test_name else "method"
    if scope_kind == "module" and is_test_name:
        return "test_function"
    return "function"
