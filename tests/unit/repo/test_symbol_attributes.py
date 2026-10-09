from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codepilot.repo.parser import TreeSitterParser
from codepilot.repo.symbols import extract_symbols


def symbols(source: str, path: str = "src/pkg/mod.py", repository_id: str = "repo"):
    return extract_symbols(
        TreeSitterParser().parse(source.encode()), path=path, repository_id=repository_id
    )


def test_fixture_multiline_decorators_and_signature(fixtures_dir: Path) -> None:
    source = (fixtures_dir / "repos/mini_auth/src/miniauth/app.py").read_text(encoding="utf-8")
    result = symbols(source, "src/miniauth/app.py")
    build = result[1]
    assert build.decorators == ("audited", "retry(\n    times=5,\n)")
    assert build.signature == "def build_session(backend: AuthBackend, name: str) -> Session | None"
    assert build.start_line == 11
    assert build.end_line == 24
    assert source.splitlines()[build.end_line - 1] == "    return Session(backend, user)"
    assert "按名字装出一个会话" in (build.docstring or "")


def test_ids_follow_exact_design_formula() -> None:
    result = symbols("def f(): pass\n")
    assert result[1].id == hashlib.sha1(b"repopkg.mod.ffunction", usedforsecurity=False).hexdigest()
    assert result[1].parent_id == result[0].id
    assert result[0].parent_id is None
    assert result[1].file_id == result[0].file_id
    assert result[1].path == "src/pkg/mod.py"


def test_ids_stable_across_line_changes_and_distinct_between_repositories() -> None:
    first = symbols("def f(): pass\n")
    shifted = symbols("# comment\n\ndef f(): pass\n")
    other = symbols("def f(): pass\n", repository_id="other")
    assert first[1].id == shifted[1].id
    assert first[1].id != other[1].id
    assert first[1].start_line == 1
    assert shifted[1].start_line == 3


def test_duplicate_parent_ids_and_nested_names_are_retained() -> None:
    result = symbols("def outer():\n    def inner(): pass\ndef outer():\n    def inner(): pass\n")
    assert len({s.id for s in result}) == 5
    assert result[2].parent_id == result[1].id
    assert result[4].parent_id == result[3].id
    assert result[4].qualified_name == "pkg.mod.outer#2.<locals>.inner"


def test_class_method_and_local_function_parents() -> None:
    result = symbols("@deco\nclass C:\n    def method(self):\n        def inner(): pass\n")
    assert result[1].signature == "class C"
    assert result[1].decorators == ("deco",)
    assert result[1].start_line == 1
    assert result[2].parent_id == result[1].id
    assert result[3].parent_id == result[2].id


def test_signature_handles_colons_unicode_and_single_line_body() -> None:
    result = symbols('def 计算(值: dict[str, int] = {"键": 1}) -> str: return "正文"\n')
    assert result[1].signature == 'def 计算(值: dict[str, int] = {"键": 1}) -> str'
    assert result[1].start_line == result[1].end_line == 1


def test_signature_preserves_multiline_async_header_and_crlf() -> None:
    source = "async def f(\r\n    value: int,\r\n) -> int:\r\n    return value\r\n"
    result = symbols(source)
    assert result[1].signature == "async def f(\r\n    value: int,\r\n) -> int"
    assert (result[0].start_line, result[0].end_line) == (1, 4)
    assert result[1].end_line == 4


def test_signature_excludes_trailing_comment_before_multiline_body() -> None:
    result = symbols('def f(value: str = ":"):  # header comment: colon\n    return value\n')
    assert result[1].signature == 'def f(value: str = ":")'


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ('"hello\\nworld"', "hello\nworld"),
        ('r"hello\\nworld"', "hello\\nworld"),
        ('"hello" " world"', "hello world"),
        ('b"bytes"', None),
        ('f"{value}"', None),
    ],
)
def test_docstrings_are_static_strings(literal: str, expected: str | None) -> None:
    result = symbols(f"# comment\n{literal}\ndef f():\n    {literal}\n")
    assert result[0].docstring == expected
    assert result[1].docstring == expected


def test_noninitial_string_is_not_docstring() -> None:
    assert symbols('def f():\n    x = 1\n    "not docs"\n')[1].docstring is None


def test_r06_compatibility_mode_is_preserved() -> None:
    from codepilot.repo.models import Symbol

    parsed = TreeSitterParser().parse(b"def f(): pass\n")
    assert extract_symbols(parsed, path="mod.py") == (
        Symbol("mod", "mod", "module"),
        Symbol("f", "mod.f", "function"),
    )
