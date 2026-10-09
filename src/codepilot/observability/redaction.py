"""脱敏（03 §24 / §25）。

所有落盘内容——事件、``llm_calls.jsonl``、保存的配置、Run Report——写出前
都要经过 ``redact()``。这不是可选项：一次 ``--record`` 运行会把请求头、
错误响应体、配置原样写进 artifacts，那里面的 key 会跟着 artifacts 目录一起
被复制、打包、发给别人。

两条容易做错的地方：

1. **键名匹配必须精确，不能用子串**。``input_tokens`` / ``output_tokens``
   包含 ``token``，按子串匹配会把用量统计整段抹掉，而且不会有任何报错。
2. **不要对长十六进制串做无差别匹配**。``FileNode.sha256``、``patch_hash``、
   symbol id 都是十六进制，无差别匹配会把它们全部替换成 ``***``，
   让 artifacts 失去追溯能力。只有当十六进制串出现在敏感键名下时才脱敏。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["REDACTED", "is_sensitive_key", "redact", "redact_text"]

REDACTED = "***REDACTED***"

#: 键名精确匹配（先做 lower + 把 ``-`` ``.`` 统一成 ``_``）。
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "authorization",
        "auth",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "credential",
        "credentials",
        "private_key",
        "cookie",
        "set_cookie",
        "session_id",
    }
)

#: 键名后缀匹配。``max_total_tokens`` 以 ``_tokens`` 结尾，不会被命中；
#: ``access_token`` / ``api_key`` 会被命中。``api_key_env`` 以 ``_env`` 结尾，
#: 也不会被命中——环境变量**名字**是允许出现在配置里的（03 §24）。
_SENSITIVE_SUFFIXES: tuple[str, ...] = (
    "_api_key",
    "_apikey",
    "_secret",
    "_password",
    "_passwd",
    "_access_key",
    "_private_key",
    "_access_token",
    "_refresh_token",
    "_credential",
)

# 字符串内部模式。顺序有意义：先处理明确的 key 形态，再处理挂在前缀上的。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # sk-xxxx / sk-proj-xxxx（OpenAI、DeepSeek 等）
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-" + REDACTED),
    # Authorization: Bearer xxxx
    (
        re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9_\-\.=]{6,}"),
        r"\1 " + REDACTED,
    ),
    # api_key=xxx / api-key: xxx / secret="xxx"
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token"
            r"|secret[_-]?key|secret|password|passwd|authorization)"
            r"(\s*[=:]\s*)([\"']?)([A-Za-z0-9_\-\./\+]{6,})"
        ),
        r"\1\2\3" + REDACTED,
    ),
)


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace("-", "_").replace(".", "_")


def is_sensitive_key(key: str) -> bool:
    """键名是否表示一个秘密值。

    只做**精确匹配**与**后缀匹配**，不做子串匹配——``input_tokens`` 必须
    保持可见。
    """
    normalized = _normalize_key(key)
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(_SENSITIVE_SUFFIXES)


def redact_text(text: str) -> str:
    """对一段自由文本做模式替换。幂等。"""
    result = text
    for pattern, replacement in _PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact(value: Any) -> Any:
    """递归脱敏任意 JSON 可序列化结构。

    - ``dict``：键名敏感时整个值替换为 ``REDACTED``（不下钻，避免留下片段）；
      否则递归处理值。
    - ``list`` / ``tuple``：逐项递归；``tuple`` 保持为 ``tuple``。
    - ``str``：做模式替换。
    - 其他类型原样返回（数字、布尔、None 都可能是有用的统计量）。

    幂等：``redact(redact(x)) == redact(x)``。
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for raw_key, raw_val in value.items():
            if isinstance(raw_key, str) and is_sensitive_key(raw_key):
                out[raw_key] = REDACTED
            else:
                out[raw_key] = redact(raw_val)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
