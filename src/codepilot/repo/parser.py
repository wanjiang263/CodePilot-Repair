"""Python 解析（R-05）。

R-06…R-12 的每一个提取器都只看这一层的产出，所以这里的接口形状决定了后面
六个任务的手感。两个设计决定：

**IR 是惰性视图，不是转换后的对象树。** 最初的设计是把 tree-sitter 的节点
递归转成 frozen dataclass。实测否决了它：一个 0.69MB 的源码文件有 30 万个
节点，转换耗时 **1561ms**，而 tree-sitter 自己解析只要 124ms——转换比它要
包装的解析慢 12 倍，内存下界 23MB/文件。改成视图之后代价是 0：``SyntaxNode``
的每个成员都直接转发给底层的 C 节点，遍历一个 30 万节点的树是 58ms。

**语法错误的文件照样返回树。** tree-sitter 是容错解析器：它会给带错误的
文件建出大部分正确的树，只把出问题的那段标成 ``ERROR``/``MISSING`` 节点。
丢掉整棵树等于把"这一行有语法错误"变成"这个文件不存在"——而后者会让
issue 定位漏掉一个可能相关的文件，且完全看不出来。所以：``errors`` 非空、
``root`` 仍然可用，由索引层决定怎么用（R-05 要求记 ``parse_error`` 并
**继续索引其余文件**，这里把它做得更细：坏文件自己也能贡献一部分 symbol）。

tree-sitter 的导入放在函数里而不是模块顶层：``PythonParser`` 是个 Protocol，
存在的意义之一就是"grammar 装不上时不至于连 import 都失败"。装在顶层的话，
一个缺失的 wheel 会让 ``import codepilot.repo.parser`` 直接炸，调用方连
"给出可操作的报错"的机会都没有。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # 只在类型检查时导入：运行时仍然按需导入（见上面关于"缺 wheel 也要能
    # import 本模块"的说明）。tree-sitter 自带 py.typed，所以这里能拿到
    # 真类型，不必把节点标成 object 再用 type: ignore 一路糊过去。
    from tree_sitter import Node, Parser, Tree

__all__ = [
    "ParseError",
    "ParsedFile",
    "ParserUnavailableError",
    "PythonParser",
    "SyntaxNode",
    "TreeSitterParser",
    "children_of_kind",
    "default_parser",
    "descendants",
    "docstring_of",
    "first_child_of_kind",
]


class ParserUnavailableError(RuntimeError):
    """语法解析后端不可用（grammar 缺失或平台没有 wheel）。"""


@dataclass(frozen=True, slots=True)
class ParseError:
    """一个语法错误的位置。"""

    message: str
    line: int
    column: int
    is_missing: bool
    """``True`` 表示 tree-sitter 补了一个缺失的 token（如缺右括号），
    而不是遇到了无法理解的文本。两者的排查方向完全不同。"""

    def render(self) -> str:
        kind = "缺失" if self.is_missing else "错误"
        return f"{self.line}:{self.column + 1} {kind}: {self.message}"


@runtime_checkable
class SyntaxNode(Protocol):
    """语法树上的一个节点（只读视图）。

    刻意做成 Protocol 而不是具体类：提取器的单测可以喂一个用 dict 搭的
    假树，不需要真的装 grammar，也不需要构造源码。

    行号是 **1-based 且闭区间**（``start_line`` 与 ``end_line`` 都指向真实
    存在的行），因为 ``Symbol.start_line``/``end_line`` 要写进报告给用户看，
    而 tree-sitter 与 Python 的 ``ast`` 一个用 0 起、一个用 1 起，把差异
    在这一层消化掉比让每个提取器各自记得 +1 可靠。
    """

    # 全部声明成只读属性而不是可写变量。协议描述的是"语法树上的一个节点"，
    # 它本来就是只读的；声明成可写变量会让每个实现被迫提供 setter，也会让
    # 用 property 实现的节点被判定为"不满足协议"。
    @property
    def kind(self) -> str:
        """节点类型，沿用 tree-sitter-python 的命名（``function_definition`` 等）。"""
        ...

    @property
    def start_line(self) -> int: ...
    @property
    def end_line(self) -> int: ...
    @property
    def start_col(self) -> int: ...
    @property
    def end_col(self) -> int: ...

    @property
    def text(self) -> str:
        """节点覆盖的源码切片。**每次访问都会重新解码字节**——循环里反复读
        同一个大节点的 ``text`` 是平方级的开销，需要就自己先存一份。"""
        ...

    @property
    def children(self) -> Sequence[SyntaxNode]: ...

    def field(self, name: str) -> SyntaxNode | None:
        """按字段名取第一个子节点，没有则 ``None``。"""
        ...

    def field_all(self, name: str) -> Sequence[SyntaxNode]:
        """按字段名取全部子节点。

        ``import os, sys`` 这类语句的 ``name`` 字段会出现多次，只取第一个
        会静默丢掉一半的导入——而症状是"某些跨文件调用解析不出来"。
        """
        ...


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """一个文件的解析结果。"""

    root: SyntaxNode
    errors: tuple[ParseError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def parse_error(self) -> str | None:
        """R-05 要求索引层记录的字段。``None`` 表示这个文件解析干净。

        只报**第一个**错误：给用户看的是一行摘要，全部错误在 ``errors`` 里。
        一个文件里 200 个错误通常是同一个原因（比如少了一个右括号导致后面
        全部错位），把它们都列出来只会淹没真正的原因。
        """
        if not self.errors:
            return None
        first = self.errors[0]
        suffix = f"（共 {len(self.errors)} 处）" if len(self.errors) > 1 else ""
        return f"{first.render()}{suffix}"


@runtime_checkable
class PythonParser(Protocol):
    """把源码字节变成语法树。"""

    name: str
    """后端名，写进索引元数据，让两份索引能看出是不是同一个后端产出的。"""

    def parse(self, source: bytes, *, path: str = "") -> ParsedFile:
        """解析 ``source``。

        ``path`` 只用于报错信息，不参与解析——带上它，用户才能在几十个
        文件的一轮索引里知道是哪个文件出的问题。
        """
        ...


# ---------------------------------------------------------------- 遍历助手
#
# 放在模块级而不是 Protocol 上：Protocol 只保留原语（kind / 位置 / 文本 /
# children / field），假树实现者只需要实现这 8 个成员，不会因为我们要多一个
# 便利方法就被迫改实现。


def descendants(node: SyntaxNode) -> Iterator[SyntaxNode]:
    """先序遍历自身与全部后代。

    显式栈而不是递归：仓库里存在上千行的函数，递归会撞 Python 的递归深度
    限制，而那种失败发生在"某个很大的文件"上，最难复现。
    """
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def children_of_kind(node: SyntaxNode, kind: str) -> list[SyntaxNode]:
    """直接子节点中指定类型的那些。"""
    return [child for child in node.children if child.kind == kind]


def first_child_of_kind(node: SyntaxNode, kind: str) -> SyntaxNode | None:
    for child in node.children:
        if child.kind == kind:
            return child
    return None


def docstring_of(body: SyntaxNode | None) -> str | None:
    """取块体第一个语句的文档字符串。

    02 §7.3：``expression_statement`` 中的首个 string。只认**第一个**语句
    ——一个文件中间漂着的字符串是普通表达式，不是文档。
    """
    if body is None:
        return None
    for statement in body.children:
        if statement.kind != "expression_statement":
            # 第一个语句不是表达式语句 => 没有 docstring，直接停。
            return None
        string = first_child_of_kind(statement, "string")
        if string is None:
            return None
        return _strip_string_quotes(string.text)
    return None


def _strip_string_quotes(raw: str) -> str:
    r"""去掉字符串字面量的引号与前缀。

    三引号排在单引号**前面**判：反过来的话，一个三引号字符串会先匹配到单
    引号分支，得到 `""doc""` 这种带残留引号的结果——而它不会报错，只会让
    报告里每个函数的文档字符串都多几个引号。

    前缀最多两字符（`rb` / `br` / `Rb`...），且必须是 `r`/`u`/`b`/`f` 的
    组合，否则 `browser()` 这种普通标识符会被误当前缀。
    """
    text = raw.strip()
    for prefix_len in (0, 1, 2):
        head = text[:prefix_len].lower()
        if head and not set(head) <= set("rubf"):
            break
        rest = text[prefix_len:]
        for quote in ('"""', "'''", '"', "'"):
            if rest.startswith(quote) and rest.endswith(quote) and len(rest) >= 2 * len(quote):
                return rest[len(quote) : -len(quote)]
    return text


# ---------------------------------------------------------------- tree-sitter 后端


class _TreeSitterNode:
    """``SyntaxNode`` 在 tree-sitter 上的实现（零转换的转发视图）。"""

    __slots__ = ("_node",)

    def __init__(self, node: Node) -> None:
        self._node = node

    @property
    def kind(self) -> str:
        return self._node.type

    @property
    def start_line(self) -> int:
        return self._node.start_point.row + 1

    @property
    def end_line(self) -> int:
        row = self._node.end_point.row
        column = self._node.end_point.column
        # end_point 是"最后一个字符**之后**"的位置。column == 0 意味着节点在
        # 行首结束，那一行没有任何内容，不算。
        #
        # 实测哪些节点会走到这一支：`module` 是最常见的——`def f():\n    pass\n`
        # 的 module 的 end_point 是 (2,0)，直接 +1 会得到 end_line=3，而文件
        # 只有 2 行。多行字符串的 `string_content` 同理。函数与类本身不会
        # （它们的 end_point 停在最后一个字符上，column > 0）。
        last = row + 1 if column > 0 else row
        return max(last, self.start_line)

    @property
    def start_col(self) -> int:
        return self._node.start_point.column

    @property
    def end_col(self) -> int:
        return self._node.end_point.column

    @property
    def text(self) -> str:
        # stub 里 ``Node.text`` 是 ``bytes | None``（节点没有字节范围时为 None）。
        return (self._node.text or b"").decode("utf-8", "replace")

    @property
    def children(self) -> Sequence[SyntaxNode]:
        return [_TreeSitterNode(child) for child in self._node.children]

    def field(self, name: str) -> SyntaxNode | None:
        found = self._node.child_by_field_name(name)
        return None if found is None else _TreeSitterNode(found)

    def field_all(self, name: str) -> Sequence[SyntaxNode]:
        # children_by_field_name 返回 list，直接复用。
        return [_TreeSitterNode(n) for n in self._node.children_by_field_name(name)]

    def __repr__(self) -> str:
        return f"<{self.kind} {self.start_line}:{self.start_col}-{self.end_line}:{self.end_col}>"


class TreeSitterParser:
    """02 §7.3 指定的解析后端。

    **不是线程安全的**：tree-sitter 的 ``Parser`` 对象带可变状态，两个线程
    共用一个会解析出交错的结果——症状是随机的、无法复现的树。这里用
    ``threading.local`` 让每个线程拿到自己的实例，代价只是每个线程一份
    grammar（几 MB），换来的是"并行索引不会偶发算错"。
    """

    name = "tree-sitter"

    def __init__(self) -> None:
        self._local = threading.local()

    def _backend(self) -> Parser:
        parser: Parser | None = getattr(self._local, "parser", None)
        if parser is None:
            try:
                import tree_sitter_python
                from tree_sitter import Language, Parser
            except ImportError as exc:  # pragma: no cover - 依赖已声明，属环境损坏
                raise ParserUnavailableError(
                    "tree-sitter 语法解析后端不可用。请确认 tree-sitter 与 "
                    "tree-sitter-python 已安装（uv sync）。"
                    f"原始错误：{exc}"
                ) from exc
            parser = Parser(Language(tree_sitter_python.language()))
            self._local.parser = parser
        return parser

    def parse(self, source: bytes, *, path: str = "") -> ParsedFile:
        tree: Tree = self._backend().parse(source)
        root = _TreeSitterNode(tree.root_node)
        errors = _collect_errors(tree.root_node) if tree.root_node.has_error else ()
        if errors and path:
            # 报错里带上文件名。索引一轮可能涉及几百个文件，只说"第 12 行
            # 有语法错误"等于没说。
            errors = tuple(
                ParseError(f"{path}: {e.message}", e.line, e.column, e.is_missing) for e in errors
            )
        return ParsedFile(root=root, errors=errors)


def _collect_errors(root: Node) -> tuple[ParseError, ...]:
    """收集每个 ``ERROR`` / ``MISSING`` 节点。

    只在 ``has_error`` 为真时调用：正常文件跳过整棵树的遍历，而这一步是
    O(节点数) 的。
    """
    found: list[ParseError] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_missing:
            found.append(
                ParseError(
                    message=f"缺少 `{node.type}`",
                    line=node.start_point.row + 1,
                    column=node.start_point.column,
                    is_missing=True,
                )
            )
        elif node.type == "ERROR":
            # stub 里 ``Node.text`` 是 ``bytes | None``。真实的 ERROR 节点总有
            # 字节，但类型上要接住 None，否则 mypy 过不去而运行期又永远看不到。
            raw = node.text or b""
            snippet = raw.decode("utf-8", "replace").strip().splitlines()
            head = snippet[0][:60] if snippet else ""
            found.append(
                ParseError(
                    message=f"无法解析：{head!r}" if head else "无法解析",
                    line=node.start_point.row + 1,
                    column=node.start_point.column,
                    is_missing=False,
                )
            )
        stack.extend(node.children)
    # 按位置排序：tree-sitter 的遍历顺序与源码顺序无关，而"第一个错误"
    # 必须是源码里最靠前的那个——报第 40 行的错而真正的起因在第 12 行，
    # 用户会去改错地方。
    found.sort(key=lambda e: (e.line, e.column))
    return tuple(found)


def default_parser() -> PythonParser:
    """构造默认解析器（02 §7.3 指定的 tree-sitter 后端）。"""
    return TreeSitterParser()
