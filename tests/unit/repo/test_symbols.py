"""R-06：符号类型、独立的 qualified-name 规则及重复定义回归。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from codepilot.repo.parser import ParsedFile, SyntaxNode, TreeSitterParser
from codepilot.repo.symbols import Symbol, extract_symbols, module_name


@pytest.fixture
def parser() -> TreeSitterParser:
    return TreeSitterParser()


def _fixture_symbols(
    fixtures_dir: Path, parser: TreeSitterParser, repo: str, path: str
) -> tuple[Symbol, ...]:
    source = (fixtures_dir / "repos" / repo / path).read_bytes()
    return extract_symbols(parser.parse(source, path=path), path=path)


def _symbols(parser: TreeSitterParser, source: str, path: str = "pkg/mod.py") -> tuple[Symbol, ...]:
    return extract_symbols(parser.parse(source.encode(), path=path), path=path)


@pytest.mark.parametrize("source", ["", "# only a comment\n", "VALUE = 1\n"])
def test_module_symbol_exists_without_definitions(parser: TreeSitterParser, source: str) -> None:
    assert _symbols(parser, source) == (Symbol("mod", "pkg.mod", "module"),)


def test_plain_layout_uses_module_path(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_calc", "calc/core.py")
    assert symbols == (
        Symbol("core", "calc.core", "module"),
        Symbol("add", "calc.core.add", "function"),
        Symbol("multiply", "calc.core.multiply", "function"),
        Symbol("divide", "calc.core.divide", "function"),
    )


def test_src_layout_removes_prefix(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_auth", "src/miniauth/tokens.py")
    assert symbols[0] == Symbol("tokens", "miniauth.tokens", "module")
    assert Symbol("issue_token", "miniauth.tokens.issue_token", "function") in symbols


def test_deep_path_becomes_dotted_module(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_pkg", "pkg/deep/nested.py")
    assert symbols == (
        Symbol("nested", "pkg.deep.nested", "module"),
        Symbol("leaf", "pkg.deep.nested.leaf", "function"),
    )


@pytest.mark.parametrize(
    ("repo", "path", "package"),
    [
        ("mini_pkg", "pkg/__init__.py", "pkg"),
        ("mini_pkg", "pkg/deep/__init__.py", "pkg.deep"),
        ("mini_auth", "src/miniauth/__init__.py", "miniauth"),
        ("mini_auth", "src/miniauth/backends/__init__.py", "miniauth.backends"),
    ],
)
def test_init_file_uses_package_name(
    fixtures_dir: Path, parser: TreeSitterParser, repo: str, path: str, package: str
) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, repo, path)
    assert symbols == (Symbol(package.rsplit(".", 1)[-1], package, "module"),)


def test_definitions_in_init_use_package_prefix(parser: TreeSitterParser) -> None:
    symbols = _symbols(parser, "def factory(): pass\n", "src/pkg/deep/__init__.py")
    assert symbols[1] == Symbol("factory", "pkg.deep.factory", "function")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/src/mod.py", "src.mod"),
        ("lib/src/mod.py", "lib.src.mod"),
        ("src.py", "src"),
        ("__init__.py", "__init__"),
        ("src/__init__.py", "__init__"),
        (r"src\pkg\mod.py", "pkg.mod"),
        (r"src\pkg\__init__.py", "pkg"),
    ],
)
def test_module_path_boundaries(path: str, expected: str) -> None:
    assert module_name(path) == expected


@pytest.mark.parametrize(
    "path", ["/pkg/mod.py", "C:/pkg/mod.py", "C:mod.py", "../mod.py", "mod.txt", ""]
)
def test_invalid_repository_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="相对 Python 文件路径"):
        module_name(path)


def test_class_method_uses_class_scope(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_auth", "src/miniauth/models.py")
    assert Symbol("User", "miniauth.models.User", "class") in symbols
    assert Symbol("has_role", "miniauth.models.User.has_role", "method") in symbols
    assert Symbol("display", "miniauth.models.User.display", "method") in symbols


def test_nested_function_uses_locals(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_auth", "src/miniauth/plugins.py")
    assert Symbol("decorate", "miniauth.plugins.retry.<locals>.decorate", "function") in symbols


def test_nested_function_in_method_uses_locals(
    fixtures_dir: Path, parser: TreeSitterParser
) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_auth", "src/miniauth/models.py")
    assert Symbol("render", "miniauth.models.User.display.<locals>.render", "function") in symbols


def test_each_function_scope_adds_locals(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_auth", "src/miniauth/plugins.py")
    assert (
        Symbol("wrapper", "miniauth.plugins.retry.<locals>.decorate.<locals>.wrapper", "function")
        in symbols
    )
    assert Symbol("wrapper", "miniauth.plugins.audited.<locals>.wrapper", "function") in symbols
    assert len([symbol for symbol in symbols if symbol.name == "wrapper"]) == 2


def test_nested_class_and_its_method_keep_lexical_scopes(parser: TreeSitterParser) -> None:
    symbols = _symbols(
        parser,
        "class Outer:\n"
        "    class Inner:\n"
        "        def method(self): pass\n"
        "def factory():\n"
        "    class Local:\n"
        "        def method(self):\n"
        "            def inner(): pass\n",
    )
    assert symbols[2] == Symbol("Inner", "pkg.mod.Outer.Inner", "class")
    assert symbols[3] == Symbol("method", "pkg.mod.Outer.Inner.method", "method")
    assert symbols[5] == Symbol("Local", "pkg.mod.factory.<locals>.Local", "class")
    assert symbols[6] == Symbol("method", "pkg.mod.factory.<locals>.Local.method", "method")
    assert symbols[7] == Symbol(
        "inner", "pkg.mod.factory.<locals>.Local.method.<locals>.inner", "function"
    )


def test_top_level_test_functions(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_calc", "test_calc.py")
    assert symbols == (
        Symbol("test_calc", "test_calc", "module"),
        Symbol("test_divide_returns_float", "test_calc.test_divide_returns_float", "test_function"),
        Symbol(
            "test_divide_by_zero_raises_value_error",
            "test_calc.test_divide_by_zero_raises_value_error",
            "test_function",
        ),
    )


def test_test_directory_functions(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_auth", "tests/test_tokens.py")
    assert symbols[1] == Symbol(
        "test_issue_returns_three_parts",
        "tests.test_tokens.test_issue_returns_three_parts",
        "test_function",
    )
    assert [symbol.kind for symbol in symbols[1:]] == ["test_function"] * 3


def test_test_methods(fixtures_dir: Path, parser: TreeSitterParser) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_auth", "tests/test_session.py")
    assert symbols[1] == Symbol("TestSession", "tests.test_session.TestSession", "class")
    assert symbols[2] == Symbol(
        "test_login_with_correct_password",
        "tests.test_session.TestSession.test_login_with_correct_password",
        "test_method",
    )
    assert [symbol.kind for symbol in symbols] == [
        "module",
        "class",
        "test_method",
        "test_method",
        "class",
        "test_method",
        "test_method",
    ]


def test_test_prefix_in_production_file_stays_ordinary(parser: TreeSitterParser) -> None:
    symbols = _symbols(
        parser, "def test_helper(): pass\nclass Helper:\n    def test_method(self): pass\n"
    )
    assert symbols[1].kind == "function"
    assert symbols[3].kind == "method"


def test_helpers_and_nested_functions_in_test_file_stay_ordinary(parser: TreeSitterParser) -> None:
    symbols = _symbols(
        parser,
        "def helper(): pass\n"
        "def test_outer():\n"
        "    def test_inner(): pass\n"
        "class TestGroup:\n"
        "    def helper(self): pass\n"
        "    def test_method(self):\n"
        "        def test_inner(): pass\n",
        "tests/test_helpers.py",
    )
    assert symbols[1].kind == "function"
    assert symbols[2].kind == "test_function"
    assert symbols[3] == Symbol(
        "test_inner", "tests.test_helpers.test_outer.<locals>.test_inner", "function"
    )
    assert symbols[5].kind == "method"
    assert symbols[6].kind == "test_method"
    assert symbols[7] == Symbol(
        "test_inner", "tests.test_helpers.TestGroup.test_method.<locals>.test_inner", "function"
    )


@pytest.mark.parametrize(
    "path", ["test_mod.py", "mod_test.py", "tests/helper.py", r"tests\helper.py"]
)
def test_test_kinds_reuse_scanner_path_conventions(parser: TreeSitterParser, path: str) -> None:
    assert _symbols(parser, "def test_example(): pass\n", path)[1].kind == "test_function"


def test_duplicate_conditional_functions_are_all_retained(
    fixtures_dir: Path, parser: TreeSitterParser
) -> None:
    symbols = _fixture_symbols(fixtures_dir, parser, "mini_pkg", "pkg/duplicates.py")
    assert symbols == (
        Symbol("duplicates", "pkg.duplicates", "module"),
        Symbol("platform_path", "pkg.duplicates.platform_path", "function"),
        Symbol("platform_path", "pkg.duplicates.platform_path#2", "function"),
        Symbol("platform_path", "pkg.duplicates.platform_path#3", "function"),
        Symbol("unique", "pkg.duplicates.unique", "function"),
    )


def test_duplicate_classes_disambiguate_their_children(parser: TreeSitterParser) -> None:
    symbols = _symbols(
        parser,
        "class Group:\n    def run(self): pass\nclass Group:\n    def run(self): pass\n",
    )
    assert symbols == (
        Symbol("mod", "pkg.mod", "module"),
        Symbol("Group", "pkg.mod.Group", "class"),
        Symbol("run", "pkg.mod.Group.run", "method"),
        Symbol("Group", "pkg.mod.Group#2", "class"),
        Symbol("run", "pkg.mod.Group#2.run", "method"),
    )


def test_duplicate_outer_functions_disambiguate_nested_functions(parser: TreeSitterParser) -> None:
    symbols = _symbols(
        parser,
        "def outer():\n    def inner(): pass\ndef outer():\n    def inner(): pass\n",
    )
    assert [symbol.qualified_name for symbol in symbols] == [
        "pkg.mod",
        "pkg.mod.outer",
        "pkg.mod.outer.<locals>.inner",
        "pkg.mod.outer#2",
        "pkg.mod.outer#2.<locals>.inner",
    ]


def test_duplicate_methods_and_nested_functions_are_retained(parser: TreeSitterParser) -> None:
    symbols = _symbols(
        parser,
        "class Group:\n"
        "    if flag:\n"
        "        def run(self): pass\n"
        "    else:\n"
        "        def run(self): pass\n"
        "def outer():\n"
        "    if flag:\n"
        "        def inner(): pass\n"
        "    else:\n"
        "        def inner(): pass\n",
    )
    assert symbols[2] == Symbol("run", "pkg.mod.Group.run", "method")
    assert symbols[3] == Symbol("run", "pkg.mod.Group.run#2", "method")
    assert symbols[5] == Symbol("inner", "pkg.mod.outer.<locals>.inner", "function")
    assert symbols[6] == Symbol("inner", "pkg.mod.outer.<locals>.inner#2", "function")
    assert len(symbols) == 7


def test_duplicate_name_across_kinds_shares_disambiguation(parser: TreeSitterParser) -> None:
    symbols = _symbols(parser, "def same(): pass\nclass same: pass\ndef same(): pass\n")
    assert symbols[1:] == (
        Symbol("same", "pkg.mod.same", "function"),
        Symbol("same", "pkg.mod.same#2", "class"),
        Symbol("same", "pkg.mod.same#3", "function"),
    )


def test_duplicate_test_definitions_keep_test_kinds(parser: TreeSitterParser) -> None:
    symbols = _symbols(
        parser,
        "def test_same(): pass\ndef test_same(): pass\n"
        "class TestGroup:\n"
        "    def test_same(self): pass\n"
        "    def test_same(self): pass\n",
        "tests/test_mod.py",
    )
    assert symbols[2] == Symbol("test_same", "tests.test_mod.test_same#2", "test_function")
    assert symbols[5] == Symbol("test_same", "tests.test_mod.TestGroup.test_same#2", "test_method")
    assert len(symbols) == 6


def test_decorated_async_definitions_are_extracted_once(parser: TreeSitterParser) -> None:
    symbols = _symbols(
        parser,
        "@decorate(1)\n@other\nasync def worker():\n"
        "    @decorate\n    async def inner(): pass\n"
        "@decorate\nclass Group:\n"
        "    @decorate\n    async def run(self): pass\n",
    )
    assert symbols == (
        Symbol("mod", "pkg.mod", "module"),
        Symbol("worker", "pkg.mod.worker", "function"),
        Symbol("inner", "pkg.mod.worker.<locals>.inner", "function"),
        Symbol("Group", "pkg.mod.Group", "class"),
        Symbol("run", "pkg.mod.Group.run", "method"),
    )


def test_syntax_error_does_not_discard_recognizable_symbols(
    fixtures_dir: Path, parser: TreeSitterParser
) -> None:
    path = "calc/broken.py"
    parsed = parser.parse((fixtures_dir / "repos" / "mini_calc" / path).read_bytes(), path=path)
    assert parsed.parse_error is not None
    symbols = extract_symbols(parsed, path=path)
    assert symbols[0] == Symbol("broken", "calc.broken", "module")
    assert Symbol("before_the_error", "calc.broken.before_the_error", "function") in symbols
    assert Symbol("after_the_error", "calc.broken.after_the_error", "function") in symbols


def test_unicode_definition_names(parser: TreeSitterParser) -> None:
    assert _symbols(parser, "def 计算(): pass\n")[1] == Symbol("计算", "pkg.mod.计算", "function")


def test_extraction_is_deterministic_and_counters_are_per_file(parser: TreeSitterParser) -> None:
    parsed = parser.parse(b"def same(): pass\ndef same(): pass\n")
    first = extract_symbols(parsed, path="one.py")
    assert extract_symbols(parsed, path="one.py") == first
    second = extract_symbols(parsed, path="two.py")
    assert [symbol.qualified_name for symbol in second] == ["two", "two.same", "two.same#2"]


@pytest.mark.parametrize("missing_name", [None, ""])
def test_partial_definition_keeps_recognizable_children(
    parser: TreeSitterParser, missing_name: str | None
) -> None:
    """协议边界可能返回不完整定义；其中可识别的子定义仍须保留。"""
    good = parser.parse(b"def retained(): pass\n").root.children[0]
    name = None if missing_name is None else Mock(spec=SyntaxNode, text=missing_name)
    incomplete = Mock(spec=SyntaxNode, kind="function_definition", children=[good])
    incomplete.field.return_value = name
    root = Mock(spec=SyntaxNode, kind="module", children=[incomplete])

    assert extract_symbols(ParsedFile(root=root), path="partial.py") == (
        Symbol("partial", "partial", "module"),
        Symbol("retained", "partial.retained", "function"),
    )


def test_definition_without_body_still_has_a_symbol(parser: TreeSitterParser) -> None:
    name = parser.parse(b"def incomplete(): pass\n").root.children[0].field("name")
    incomplete = Mock(spec=SyntaxNode, kind="function_definition", children=[])
    incomplete.field.side_effect = lambda field: name if field == "name" else None
    root = Mock(spec=SyntaxNode, kind="module", children=[incomplete])

    assert extract_symbols(ParsedFile(root=root), path="partial.py") == (
        Symbol("partial", "partial", "module"),
        Symbol("incomplete", "partial.incomplete", "function"),
    )
