"""R-05 的证明：PythonParser Protocol 与 tree-sitter 后端。

这里钉的主要是**位置信息**——行号是唯一一个错了不会报错、只会让报告里的
每一处引用都偏移一行的东西。所以 `end_line` 的边界（`end_point` 是"最后一个
字符之后"，节点在行首结束时不能算那一行）单独有用例。

另一组是"坏文件不中断索引"：R-05 的验收要求它，而它同时也是本任务最容易
做成"整个文件被丢掉"的地方——tree-sitter 是容错解析器，丢掉整棵树等于把
"这一行有语法错误"降级成"这个文件不存在"。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from codepilot.repo.parser import (
    ParsedFile,
    PythonParser,
    SyntaxNode,
    TreeSitterParser,
    children_of_kind,
    default_parser,
    descendants,
    docstring_of,
    first_child_of_kind,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "repos"

SOURCE = b'''"""Mod doc."""

import os, sys
import os.path as osp
from .rel import thing as t

CONST = 1


@deco
@deco2(1, k=2)
def outer(a, b=2, *args, **kw) -> int:
    """Doc string."""
    def inner():
        return t()
    return inner


class Base:
    """Base doc."""

    def method(self):
        return self.helper()


class Child(Base, Mixin, metaclass=M):
    attr: int = 1
'''


@pytest.fixture
def parser() -> TreeSitterParser:
    return TreeSitterParser()


@pytest.fixture
def tree(parser: TreeSitterParser) -> ParsedFile:
    return parser.parse(SOURCE)


def kinds_of(node: SyntaxNode) -> list[str]:
    return [n.kind for n in descendants(node)]


# ------------------------------------------------------------------ 基本


def test_default_parser_satisfies_the_protocol() -> None:
    assert isinstance(default_parser(), PythonParser)


def test_root_is_a_module(tree: ParsedFile) -> None:
    assert tree.root.kind == "module"
    assert tree.ok
    assert tree.parse_error is None


def test_empty_source_parses_to_an_empty_module(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"")
    assert tree.ok
    assert tree.root.children == []
    # 空模块的 end_point 是 (0,0)：归一化会算出 0，靠 max() 兜回 start_line。
    assert tree.root.start_line == 1
    assert tree.root.end_line == 1


def test_source_without_trailing_newline(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"def f():\n    return 1")
    function = tree.root.children[0]
    assert function.start_line == 1
    assert function.end_line == 2


def test_expected_node_kinds_are_present(tree: ParsedFile) -> None:
    kinds = set(kinds_of(tree.root))
    for expected in (
        "function_definition",
        "class_definition",
        "decorated_definition",
        "call",
        "import_statement",
        "import_from_statement",
    ):
        assert expected in kinds, f"缺少节点类型 {expected}"


# ------------------------------------------------------------------ 字段


def test_function_field_access(tree: ParsedFile) -> None:
    decorated = first_child_of_kind(tree.root, "decorated_definition")
    assert decorated is not None
    function = decorated.field("definition")
    assert function is not None and function.kind == "function_definition"
    name = function.field("name")
    assert name is not None and name.text == "outer"
    assert function.field("parameters") is not None
    assert function.field("body") is not None


def test_decorators_are_children_not_named_fields(tree: ParsedFile) -> None:
    """``decorated_definition`` 的装饰器**没有具名字段**。

    实测 ``field_name_for_child`` 对它返回 ``None``，``children_by_field_name
    ("decorator")`` 返回 0 个。按字段取会静默得到空列表——症状是"所有
    函数的装饰器都是空的"，而且不报错。所以这里按 kind 取。
    """
    decorated = first_child_of_kind(tree.root, "decorated_definition")
    assert decorated is not None
    assert list(decorated.field_all("decorator")) == []
    decorators = children_of_kind(decorated, "decorator")
    assert [d.text for d in decorators] == ["@deco", "@deco2(1, k=2)"]


def test_repeated_named_fields_are_all_returned(tree: ParsedFile) -> None:
    """``import os, sys`` 有两项，``field`` 只给第一个。

    只取第一个会静默丢掉一半导入——症状是"某些跨文件调用解析不出来"，
    而排查方向会指向调用解析而不是 import 提取。
    """
    statement = first_child_of_kind(tree.root, "import_statement")
    assert statement is not None
    assert [n.text for n in statement.field_all("name")] == ["os", "sys"]
    assert statement.field("name") is not None
    assert statement.field("name").text == "os"  # type: ignore[union-attr]


def test_missing_field_returns_none(tree: ParsedFile) -> None:
    klass = first_child_of_kind(tree.root, "class_definition")
    assert klass is not None
    assert klass.field("superclasses") is None  # Base 没有基类
    assert list(klass.field_all("nonexistent")) == []


def test_class_superclasses_and_methods(tree: ParsedFile) -> None:
    classes = children_of_kind(tree.root, "class_definition")
    child = next(c for c in classes if c.field("name").text == "Child")  # type: ignore[union-attr]
    assert child.field("superclasses") is not None

    base = next(c for c in classes if c.field("name").text == "Base")  # type: ignore[union-attr]
    body = base.field("body")
    assert body is not None
    methods = children_of_kind(body, "function_definition")
    assert [m.field("name").text for m in methods] == ["method"]  # type: ignore[union-attr]


def test_relative_import_module_name(tree: ParsedFile) -> None:
    statement = first_child_of_kind(tree.root, "import_from_statement")
    assert statement is not None
    module = statement.field("module_name")
    assert module is not None
    assert module.kind == "relative_import"
    assert module.text == ".rel"


# ------------------------------------------------------------------ 行号


def test_line_numbers_are_one_based_and_inclusive(tree: ParsedFile) -> None:
    """``def outer`` 从第 12 行开始，到第 16 行结束（``return inner``）。

    改这一条之前先确认 SOURCE 没变——行号断言与源码是绑定的。
    """
    decorated = first_child_of_kind(tree.root, "decorated_definition")
    assert decorated is not None
    # 装饰器行（第 10 行）算在 decorated_definition 里，不算在内层函数里。
    assert decorated.start_line == 10
    function = decorated.field("definition")
    assert function is not None
    assert function.start_line == 12
    assert function.end_line == 16


def test_module_ending_at_a_line_start_does_not_count_that_line(
    parser: TreeSitterParser,
) -> None:
    """``end_point`` 是"最后一个字符**之后**"的位置。

    真正会走到归一化那一支的是 ``module``：``def f():\\n    pass\\n`` 的
    module 的 ``end_point`` 是 ``(2, 0)``——第 3 行的行首——而文件只有 2 行。
    不做归一化，根节点的 ``end_line`` 就是 3。

    这个偏差会一路流进索引：模块 symbol 的行范围与 scanner 数出来的
    ``line_count`` 从此对不上，而两者都不会报错。
    """
    tree = parser.parse(b"def f():\n    pass\n")
    assert tree.root.end_line == 2, "根节点的结束行不能越过文件的实际行数"
    assert tree.root.end_line == len(b"def f():\n    pass\n".decode().splitlines())


def test_multiline_string_content_ending_at_a_line_start(parser: TreeSitterParser) -> None:
    tree = parser.parse(b'x = """a\nb\n"""\n')
    contents = [n for n in descendants(tree.root) if n.kind == "string_content"]
    assert contents
    assert contents[0].end_line == 2


def test_single_line_definition() -> None:
    """单行定义 ``def f(): pass`` 的起止行相同。"""
    tree = TreeSitterParser().parse(b"def f(): pass\n")
    function = tree.root.children[0]
    assert function.start_line == function.end_line == 1
    assert function.start_col == 0
    assert function.end_col == 13


def test_multiline_nested_function_line_ranges(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"def outer():\n    def inner():\n        return 1\n    return inner()\n")
    outer = tree.root.children[0]
    inner = children_of_kind(outer.field("body"), "function_definition")[0]  # type: ignore[arg-type]
    assert (outer.start_line, outer.end_line) == (1, 4)
    assert (inner.start_line, inner.end_line) == (2, 3)


# ------------------------------------------------------------------ docstring


def test_module_docstring(tree: ParsedFile) -> None:
    assert docstring_of(tree.root) == "Mod doc."


def test_function_docstring(tree: ParsedFile) -> None:
    decorated = first_child_of_kind(tree.root, "decorated_definition")
    function = decorated.field("definition")  # type: ignore[union-attr]
    assert docstring_of(function.field("body")) == "Doc string."  # type: ignore[union-attr]


def test_class_docstring(tree: ParsedFile) -> None:
    base = children_of_kind(tree.root, "class_definition")[0]
    assert docstring_of(base.field("body")) == "Base doc."


def test_missing_docstring_is_none(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"def f():\n    return 1\n")
    assert docstring_of(tree.root.children[0].field("body")) is None


def test_first_statement_not_a_string_means_no_docstring(parser: TreeSitterParser) -> None:
    """文件中间漂着的字符串是普通表达式，不是文档。"""
    tree = parser.parse(b'x = 1\n"not a docstring"\n')
    assert docstring_of(tree.root) is None


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        (b'"""double triple"""', "double triple"),
        (b"'''single triple'''", "single triple"),
        (b'"double"', "double"),
        (b"'single'", "single"),
        (b'r"raw"', "raw"),
        (b'R"upper raw"', "upper raw"),
        (b'b"bytes"', "bytes"),
        (b'rb"both"', "both"),
        (b'"""multi\nline"""', "multi\nline"),
        # 三引号里嵌单引号：如果先判单引号，这里会得到 `""a" b""` 这种带残留
        # 引号的结果，而且不报错——报告里每个函数的文档都会多几个引号。
        (b'"""a" b"""', 'a" b'),
        (b"'''it's'''", "it's"),
    ],
)
def test_docstring_quote_and_prefix_stripping(literal: bytes, expected: str) -> None:
    tree = TreeSitterParser().parse(literal + b"\n")
    assert docstring_of(tree.root) == expected


# ------------------------------------------------------------------ 语法错误


def test_clean_file_has_no_errors(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"def f():\n    return 1\n", path="ok.py")
    assert tree.errors == ()
    assert tree.parse_error is None


def test_syntax_error_is_reported_with_a_location(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"def f(a: int -> int:\n    return a\n", path="bad.py")
    assert not tree.ok
    assert tree.errors
    first = tree.errors[0]
    assert first.line == 1
    assert "bad.py" in first.message, "报错里必须带文件名"
    assert tree.parse_error is not None
    assert tree.parse_error.startswith("1:")


def test_missing_token_is_distinguished_from_unparsable_text(parser: TreeSitterParser) -> None:
    """``MISSING`` 与 ``ERROR`` 要分开：前者是"少了个 token"（语法树里被补
    了一个空节点），后者是"这段文本看不懂"，排查方向完全不同。

    实测哪些输入产生哪一种（不是猜的）：``def f(: pass`` → ``MISSING: )``；
    ``x = (1 + 2`` / ``x = [1, 2`` / ``x = "abc`` → ``ERROR``。同样是"少了个
    右括号"，左边是 MISSING、右边是 ERROR——所以两种都得处理。
    """
    tree = parser.parse(b"def f(: pass\n")
    assert any(e.is_missing for e in tree.errors)
    assert any("缺少" in e.render() for e in tree.errors)

    other = parser.parse(b"x = (1 + 2\n")
    assert other.errors
    assert not any(e.is_missing for e in other.errors)


def test_errors_are_sorted_by_source_position(parser: TreeSitterParser) -> None:
    """报"第一个错误"必须是源码里最靠前的那个。

    tree-sitter 节点的遍历顺序与源码顺序无关，不排序的话会报第 9 行的错，
    而真正的起因在第 2 行——用户会去改错地方。
    """
    source = b"def a(: pass\ndef b(: pass\ndef c(: pass\n"
    tree = parser.parse(source)
    lines = [e.line for e in tree.errors]
    assert lines == sorted(lines)


def test_error_count_is_summarised(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"def a(: pass\ndef b(: pass\n")
    assert len(tree.errors) > 1
    assert f"共 {len(tree.errors)} 处" in (tree.parse_error or "")


# ------------------------------------------------------------------ 容错：坏文件不中断


def test_broken_file_still_yields_a_usable_tree(parser: TreeSitterParser) -> None:
    """R-05 的核心验收：坏文件**自己**也要贡献可用的树。

    丢掉整棵树等于把"这一行有语法错误"降级成"这个文件不存在"，而后者会让
    issue 定位静默漏掉一个可能相关的文件。
    """
    broken = (FIXTURES / "mini_calc" / "calc" / "broken.py").read_bytes()
    tree = parser.parse(broken, path="calc/broken.py")
    assert not tree.ok

    names = {
        node.field("name").text  # type: ignore[union-attr]
        for node in descendants(tree.root)
        if node.kind == "function_definition" and node.field("name") is not None
    }
    assert "before_the_error" in names
    assert "after_the_error" in names


def test_good_files_in_the_same_repo_parse_cleanly(parser: TreeSitterParser) -> None:
    good = (FIXTURES / "mini_calc" / "calc" / "core.py").read_bytes()
    tree = parser.parse(good, path="calc/core.py")
    assert tree.ok
    assert tree.parse_error is None


def test_path_is_included_in_messages_only_on_error(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"def f(: pass\n", path="pkg/mod.py")
    assert all("pkg/mod.py" in e.message for e in tree.errors)


# ------------------------------------------------------------------ 遍历助手


def test_descendants_is_iterative(parser: TreeSitterParser) -> None:
    """显式栈而不是递归：仓库里有上千行的函数，递归会撞深度限制，
    而那种失败只在"某个很大的文件"上出现，最难复现。"""
    depth = 3000
    source = b"x = " + b"[" * depth + b"]" * depth + b"\n"
    tree = parser.parse(source)
    assert sum(1 for _ in descendants(tree.root)) > depth


def test_children_of_kind_is_direct_children_only(tree: ParsedFile) -> None:
    assert children_of_kind(tree.root, "call") == []
    assert children_of_kind(tree.root, "class_definition")


def test_first_child_of_kind_returns_none_when_absent(tree: ParsedFile) -> None:
    assert first_child_of_kind(tree.root, "while_statement") is None


# ------------------------------------------------------------------ 编码与并发


def test_non_utf8_bytes_do_not_crash(parser: TreeSitterParser) -> None:
    tree = parser.parse(b"# caf\xe9\nx = 1\n")
    assert tree.root.kind == "module"


def test_unicode_identifiers_are_decoded(parser: TreeSitterParser) -> None:
    tree = parser.parse("def 计算(数目: int) -> int:\n    return 数目\n".encode())
    function = tree.root.children[0]
    assert function.field("name").text == "计算"  # type: ignore[union-attr]


def test_concurrent_parsing_is_consistent(parser: TreeSitterParser) -> None:
    """tree-sitter 的 Parser 对象带可变状态，两个线程共用会解析出交错的结果。

    这种 bug 的症状是随机的、无法复现的树，所以值得用一条测试钉住。
    """
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def work() -> None:
        try:
            for _ in range(20):
                tree = parser.parse(SOURCE)
                text = "|".join(
                    n.field("name").text  # type: ignore[union-attr]
                    for n in descendants(tree.root)
                    if n.kind == "function_definition" and n.field("name") is not None
                )
                with lock:
                    results.append(text)
        except BaseException as exc:
            # 线程里的异常要带回主线程，否则它只会打印到 stderr、测试照样绿。
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发解析抛异常: {errors}"
    assert len(set(results)) == 1, "并发解析的结果不一致"
    assert results[0].startswith("outer")


# ------------------------------------------------------------------ Protocol 可替换


class _FakeNode:
    """只用 dict 搭出来的假树：证明 Protocol 真的能被替换。"""

    def __init__(
        self,
        kind: str,
        text: str = "",
        line: int = 1,
        children: Sequence[SyntaxNode] = (),
        fields: dict[str, Sequence[SyntaxNode]] | None = None,
    ) -> None:
        self.kind = kind
        self.text = text
        self.start_line = line
        self.end_line = line
        self.start_col = 0
        self.end_col = len(text)
        self.children = list(children)
        self._fields = fields or {}

    def field(self, name: str) -> SyntaxNode | None:
        found = self._fields.get(name)
        return found[0] if found else None

    def field_all(self, name: str) -> Sequence[SyntaxNode]:
        return self._fields.get(name, [])


def test_the_protocol_is_satisfied_by_a_hand_built_tree() -> None:
    """R-06…R-12 的单测不必真的装 grammar、也不必构造源码。"""
    name = _FakeNode("identifier", "f")
    body = _FakeNode("block", "return 1")
    function = _FakeNode(
        "function_definition",
        "def f():\n    return 1",
        children=[name, body],
        fields={"name": [name], "body": [body]},
    )
    root = _FakeNode("module", children=[function])

    assert isinstance(root, SyntaxNode)
    assert first_child_of_kind(root, "function_definition") is function
    assert docstring_of(body) is None
    assert [n.kind for n in descendants(root)] == [
        "module",
        "function_definition",
        "identifier",
        "block",
    ]
