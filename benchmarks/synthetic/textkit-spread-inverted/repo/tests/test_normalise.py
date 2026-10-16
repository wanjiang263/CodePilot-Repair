"""``textkit.normalise`` 的现有测试。"""

from __future__ import annotations

from textkit.normalise import collapse_whitespace, split_fields, title


def test_split_fields_blank_line() -> None:
    assert split_fields("   ") == []


def test_split_fields_honours_max_split() -> None:
    assert split_fields("a,b,c", ",", max_split=1) == ["a", "b,c"]


def test_collapse_whitespace() -> None:
    assert collapse_whitespace("  a\tb\n c ") == "a b c"


def test_title_of_word() -> None:
    assert title("hello") == "Hello"
