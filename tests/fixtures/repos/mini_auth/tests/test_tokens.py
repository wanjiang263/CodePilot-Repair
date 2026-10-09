"""令牌测试：演示测试函数与直接调用被测算符。"""

from __future__ import annotations

from miniauth.tokens import issue_token, verify_token


def test_issue_returns_three_parts() -> None:
    token = issue_token("demo")
    assert len(token.split(":")) == 3


def test_verify_accepts_a_fresh_token() -> None:
    assert verify_token(issue_token("demo", ttl=60)) == "demo"


def test_verify_rejects_garbage() -> None:
    assert verify_token("not-a-token") is None
