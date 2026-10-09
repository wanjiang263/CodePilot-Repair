"""这个文件故意有语法错误。

R-05 验收要求：一个坏文件不能让整次索引中断，坏文件自己的 parse_error 非空，
其余文件的 symbol 照常产出。所以这里的错误要局部——tree-sitter 是容错解析器，
它会在出错处插一个 ERROR 节点，前后的函数仍然能被解析出来。
"""

from __future__ import annotations


def before_the_error() -> int:
    """错误之前的函数：必须仍被解析出来。"""
    return 1


def missing_paren(a: int -> int:
    """少了一个右括号。"""
    return a


def after_the_error() -> int:
    """错误之后的函数：容错解析要能让它也被解析出来。"""
    return 2
