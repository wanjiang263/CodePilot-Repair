"""四则运算。"""

from __future__ import annotations


def add(a: float, b: float) -> float:
    """加法。"""
    return a + b


def multiply(a: float, b: float) -> float:
    """乘法：内部调用 add，R-09 要求建出边。"""
    total = 0.0
    for _ in range(int(b)):
        total = add(total, a)
    return total


def divide(a: float, b: float) -> float:
    """除法。零除时抛 ValueError 而不是 ZeroDivisionError。"""
    if b == 0:
        raise ValueError("除数不能为零")
    return a / b
