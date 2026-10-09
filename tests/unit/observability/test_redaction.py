"""F-09 的证明：嵌套结构中的 key 被替换；正常文本不被误伤；幂等。

"正常文本不被误伤"是这里最要紧的一组用例。脱敏太狠会把 artifacts 变成
一堆 ``***``，而且不会报错——只有对照用例能挡住它。
"""

from __future__ import annotations

import pytest

from codepilot.observability.redaction import (
    REDACTED,
    is_sensitive_key,
    redact,
    redact_text,
)


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "apikey",
        "API-KEY",
        "Authorization",
        "secret",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "private_key",
        "cookie",
        "deepseek_api_key",
        "MOONSHOT_API_KEY",
    ],
)
def test_sensitive_keys_are_detected(key: str) -> None:
    assert is_sensitive_key(key) is True


@pytest.mark.parametrize(
    "key",
    [
        # 这几个如果被误判，用量统计会整段消失，且不会有任何报错。
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "max_total_tokens",
        "cached_input_tokens",
        # 环境变量的**名字**是允许出现在配置里的（03 §24）。
        "api_key_env",
        # 普通的十六进制字段：无差别匹配会把它们也抹掉，artifacts 就失去
        # 追溯能力了。
        "sha256",
        "patch_hash",
        "symbol_id",
        "code_version",
        "config_hash",
        # 无关字段
        "issue",
        "summary",
        "model",
        "base_url",
        "cost",
    ],
)
def test_non_sensitive_keys_are_kept(key: str) -> None:
    assert is_sensitive_key(key) is False


def test_redact_replaces_value_under_sensitive_key() -> None:
    out = redact({"api_key": "abcdef123456"})
    assert out == {"api_key": REDACTED}


def test_redact_does_not_descend_into_sensitive_value() -> None:
    """敏感键下的整个值被替换，不留下片段。"""
    out = redact({"credentials": {"user": "u", "password": "p"}})
    assert out == {"credentials": REDACTED}


def test_redact_walks_nested_structures() -> None:
    payload = {
        "run_id": "run1",
        "usage": {"input_tokens": 9200, "output_tokens": 610},
        "providers": [
            {"name": "deepseek", "api_key": "sk-abcdefghijklmn"},
            {"name": "glm", "api_key_env": "ZHIPU_API_KEY"},
        ],
    }
    out = redact(payload)
    assert out["usage"] == {"input_tokens": 9200, "output_tokens": 610}
    assert out["providers"][0]["api_key"] == REDACTED
    assert out["providers"][1]["api_key_env"] == "ZHIPU_API_KEY"


def test_redact_preserves_tuple_type() -> None:
    assert redact(("ok", "fine")) == ("ok", "fine")


def test_redact_preserves_non_string_scalars() -> None:
    payload = {"cost": 0.42, "llm_calls": 5, "ok": True, "nothing": None}
    assert redact(payload) == payload


def test_redact_text_matches_openai_style_keys() -> None:
    text = "using key sk-proj-AbCdEf1234567890 for request"
    out = redact_text(text)
    assert "sk-proj-AbCdEf1234567890" not in out
    assert out.startswith("using key sk-")


def test_redact_text_matches_bearer_header() -> None:
    out = redact_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
    assert "eyJhbGciOiJIUzI1NiJ9" not in out


def test_redact_text_matches_inline_assignment() -> None:
    out = redact_text("api_key=Zx9Qw8Er7Ty6Ui5Op4")
    assert "Zx9Qw8Er7Ty6Ui5Op4" not in out


def test_redact_text_leaves_ordinary_text_alone() -> None:
    """一段普通的 traceback 必须原样保留。"""
    text = (
        "Traceback (most recent call last):\n"
        '  File "src/pkg/core.py", line 42, in first\n'
        "    return items[0]\n"
        "IndexError: list index out of range"
    )
    assert redact_text(text) == text


def test_redact_does_not_touch_hex_hashes() -> None:
    """``sha256`` / ``patch_hash`` 是十六进制串，不能用无差别规则匹配。"""
    payload = {
        "sha256": "a" * 64,
        "patch_hash": "b" * 40,
        "symbol_id": "c" * 32,
    }
    assert redact(payload) == payload


def test_redact_is_idempotent() -> None:
    payload = {
        "api_key": "sk-abcdefghijklmn",
        "nested": [{"token": "xyz"}, "api_key=Zx9Qw8Er7Ty6Ui5Op4"],
    }
    once = redact(payload)
    assert redact(once) == once


def test_redact_is_idempotent_on_text() -> None:
    text = "key=Zx9Qw8Er7Ty6Ui5Op4 and sk-abcdefghijklm"
    once = redact_text(text)
    assert redact_text(once) == once
