"""同名重复定义。

02 §7.2：同名重复定义（如条件定义）保留全部，追加 ``#2``、``#3`` 后缀。
只保留最后一个会让"检索到一个其实没生效的定义"变成静默的错误答案。
"""

from __future__ import annotations

import sys

if sys.platform == "win32":

    def platform_path() -> str:
        """Windows 实现。qualified name 应为 pkg.duplicates.platform_path。"""
        return "C:/"

else:

    def platform_path() -> str:
        """POSIX 实现。qualified name 应为 pkg.duplicates.platform_path#2。"""
        return "/"


def platform_path() -> str:
    """无条件重定义，覆盖前面的。qualified name 应为 pkg.duplicates.platform_path#3。"""
    return ""


def unique() -> int:
    """对照：没有重名的函数不带后缀。"""
    return 0
