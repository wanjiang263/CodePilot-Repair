"""数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class User:
    """一个账户。"""

    name: str
    roles: list[str] = field(default_factory=list)

    def has_role(self, role: str) -> bool:
        """是否拥有某个角色。"""
        return role in self.roles

    def display(self) -> str:
        """展示名。

        故意带一个**嵌套函数**：R-06 要求它的 qualified name 是
        ``miniauth.models.User.display.<locals>.render``。
        """

        def render(prefix: str) -> str:
            return f"{prefix}{self.name}"

        return render("@")
