"""三层深，用来验证模块名的点号拼接。"""

from __future__ import annotations


def leaf() -> str:
    """qualified name 应为 pkg.deep.nested.leaf。"""
    return "leaf"
