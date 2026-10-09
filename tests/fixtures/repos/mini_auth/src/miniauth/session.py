"""会话对象：演示继承与多级导入。"""

from __future__ import annotations

from miniauth.backends import AuthBackend, MemoryBackend
from miniauth.models import User
from miniauth.tokens import issue_token, verify_token


class Session:
    """一次登录会话。

    ``AuthBackend`` 在仓库内——R-11 要求为它建出 INHERIT 边。
    """

    def __init__(self, backend: AuthBackend, user: User) -> None:
        self.backend = backend
        self.user = user

    def login(self, password: str) -> str | None:
        """登录成功返回令牌。"""
        if not self.backend.check_password(self.user.name, password):
            return None
        return issue_token(self.user.name)

    def logout(self, token: str) -> None:
        self.backend.revoke(token)

    def resume(self, token: str) -> User | None:
        """用令牌恢复会话。"""
        name = verify_token(token)
        if name is None:
            return None
        return self.backend.load(name)


class AdminSession(Session):
    """管理员会话：演示单继承 + 方法覆盖。"""

    def login(self, password: str) -> str | None:
        if not self.user.has_role("admin"):
            return None
        return super().login(password)


class AuditedSession(Session):
    """演示子类调用父类方法。"""

    def __init__(self, backend: AuthBackend, user: User, log: list[str]) -> None:
        super().__init__(backend, user)
        self.log = log

    def login(self, password: str) -> str | None:
        self.log.append(self.user.name)
        return super().login(password)
