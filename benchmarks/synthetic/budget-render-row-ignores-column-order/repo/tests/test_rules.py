"""``budget.rules`` 的现有测试。"""

from __future__ import annotations

from budget.rules import apply_discount, categorise, is_recurring


def test_categorise_detects_insurance() -> None:
    assert categorise(120.0, "Insurance renewal") == "housing"


def test_categorise_uses_coffee_keyword() -> None:
    assert categorise(12.0, "Coffee") == "food"


def test_categorise_large_fallback() -> None:
    assert categorise(900.0, "desk") == "large"
    assert categorise(20.0, "desk") == "general"


def test_is_recurring_rejects_unrelated_note() -> None:
    assert is_recurring("one off gift") is False


def test_discount_is_only_about_membership() -> None:
    """不检查两个分支的具体归属，只确认它是纯函数且原价可回显。

    ``apply_discount`` 的会员分支正是本 suite 的一个种子缺陷（条件写反），把两个
    分支都写死在可见测试里会让 base 一上来就是红的，而 base 必须是"缺陷尚未被
    任何现有测试抓到"的状态。
    """
    assert apply_discount(0.0, member=True) == 0.0
