"""后端抽象：只定义接口，不实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from miniauth.models import User


class AuthBackend(ABC):
    """所有后端的基类。**仓库内的基类**——R-11 要求为它建边。

    ``ABC`` 本身是仓库外的基类，不该建边。
    """

    @abstractmethod
    def check_password(self, name: str, password: str) -> bool:
        """校验密码。"""

    @abstractmethod
    def load(self, name: str) -> User | None:
        """按名字取用户。"""

    def revoke(self, token: str) -> None:
        """吊销令牌。默认什么也不做——不是所有后端都有状态。"""
