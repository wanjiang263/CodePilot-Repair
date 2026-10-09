"""会话测试：演示测试类（test_method）。"""

from __future__ import annotations

from miniauth.models import User
from miniauth.session import AdminSession, Session


class TestSession:
    def test_login_with_correct_password(self, backend, user) -> None:
        session = Session(backend, user)
        assert session.login("hunter2") is not None

    def test_login_with_wrong_password(self, backend, user) -> None:
        session = Session(backend, user)
        assert session.login("wrong") is None


class TestAdminSession:
    def test_admin_can_login(self, backend, user) -> None:
        assert AdminSession(backend, user).login("hunter2") is not None

    def test_plain_user_cannot_use_admin_session(self, backend) -> None:
        plain = User(name="demo", roles=[])
        assert AdminSession(backend, plain).login("hunter2") is None
