"""mini_auth：一个刻意做小的认证库。

存在的唯一目的是给 R 簇的解析器提供一份**覆盖每条规则**的输入：
相对导入（含多级）、继承、带参数与不带参数的装饰器、嵌套函数、
测试类、src/ layout。
"""

from miniauth.models import User
from miniauth.session import Session
from miniauth.tokens import issue_token, verify_token

__all__ = ["Session", "User", "issue_token", "verify_token"]
