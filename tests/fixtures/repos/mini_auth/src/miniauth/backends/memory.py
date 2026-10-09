"""内存后端：子包里的实现。"""

from __future__ import annotations

from miniauth.backends.base import AuthBackend
from miniauth.models import User


class MemoryBackend(AuthBackend):
    """把用户存在字典里。"""

    def __init__(self, users: dict[str, User] | None = None) -> None:
        self._users: dict[str, User] = dict(users or {})
        self._passwords: dict[str, str] = {}
        self._revoked: set[str] = set()

    def add(self, user: User, password: str) -> None:
        self._users[user.name] = user
        self._passwords[user.name] = password

    def check_password(self, name: str, password: str) -> bool:
        return self._passwords.get(name) == password

    def load(self, name: str) -> User | None:
        return self._users.get(name)

    def revoke(self, token: str) -> None:
        self._revoked.add(token)
