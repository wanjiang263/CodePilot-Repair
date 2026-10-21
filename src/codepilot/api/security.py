"""Input hardening for the HTTP surface.

The browser may only name a repository through a server-side policy:

* ``https`` URLs whose host is in the configured allow-list, with no credentials,
  port, query, fragment, percent-encoding or path traversal;
* a known demo repository name that the server maps to ``api_demo_root/<name>``.

It can never submit ``file://``, a host path, a relative path, an SSH/SCP source
or an arbitrary artifact path. Run/artifact lookup is always by task/run identity
in the queue database or the read-only benchmark catalog.

All paths returned by the API are scrubbed against known roots so internal
filesystem layout and credentials never leak through events or reports.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from codepilot.config import PROJECT_ROOT, Settings
from codepilot.observability.redaction import redact_text

__all__ = [
    "PathScrubber",
    "RepositorySecurityError",
    "ValidatedRepository",
    "validate_base_commit",
    "validate_repository",
]

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DEMO_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_BASE_COMMIT = re.compile(r"^(?:HEAD|[0-9a-fA-F]{7,64})$")
_MAX_URL_LENGTH = 2048
_ABSOLUTE_PATH = re.compile(
    r"""(?<![\w:/])(?:[A-Za-z]:\\[^\s"'<>|]+|/(?:root|home|mnt|tmp|var|opt|srv|usr)/[^\s"'<>|]+)"""
)


class RepositorySecurityError(ValueError):
    """The requested repository source is not admissible."""


@dataclass(frozen=True, slots=True)
class ValidatedRepository:
    kind: str
    """``https`` or ``demo``."""
    value: str
    """Normalized clone URL, or the demo repository name."""
    label: str
    """Safe display label; never an absolute host path."""
    local_path: Path | None = None


def _demo_path(settings: Settings, name: str) -> Path:
    if not _DEMO_NAME.fullmatch(name):
        raise RepositorySecurityError("演示仓库名称不合法")
    root = settings.api_demo_root.expanduser().resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        raise RepositorySecurityError("演示仓库名称越界")
    if not candidate.is_dir():
        raise RepositorySecurityError("演示仓库不存在")
    return candidate


def _validate_https(url: str, allowed_hosts: frozenset[str]) -> str:
    if len(url) > _MAX_URL_LENGTH:
        raise RepositorySecurityError("仓库 URL 过长")
    if _CONTROL.search(url) or "\\" in url or " " in url:
        raise RepositorySecurityError("仓库 URL 含非法字符")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise RepositorySecurityError("只接受 https:// 仓库")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise RepositorySecurityError("仓库 URL 不允许携带用户凭据")
    if parsed.query or parsed.fragment:
        raise RepositorySecurityError("仓库 URL 不允许 query 或 fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RepositorySecurityError("仓库 URL 端口不合法") from exc
    if port not in (None, 443):
        raise RepositorySecurityError("仓库 URL 只允许默认 HTTPS 端口")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host not in allowed_hosts:
        raise RepositorySecurityError("仓库域名不在白名单内")
    if not allowed_hosts:
        raise RepositorySecurityError("服务端未配置仓库白名单")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(
        (".local", ".internal", ".localhost", ".home.arpa")
    ):
        raise RepositorySecurityError("仓库域名不是公共域名")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise RepositorySecurityError("仓库 URL 不允许使用 IP 地址")
    if "." not in host:
        raise RepositorySecurityError("仓库域名必须是完整域名")
    path = parsed.path
    if not path.startswith("/") or path in {"/", ""}:
        raise RepositorySecurityError("仓库 URL 缺少 owner/repo 路径")
    if "%" in path:
        raise RepositorySecurityError("仓库 URL 不允许百分号编码")
    if path.endswith("/") or "//" in path:
        raise RepositorySecurityError("仓库 URL 路径不合法")
    decoded = unquote(path)
    if decoded != path:
        raise RepositorySecurityError("仓库 URL 不允许编码路径")
    parts = PurePosixPath(path).parts
    if len(parts) < 3:
        raise RepositorySecurityError("仓库 URL 必须包含 owner/repo")
    if len(parts) > 8:
        raise RepositorySecurityError("仓库 URL 路径层级过深")
    for part in parts[1:]:
        if part in {".", ".."} or part.startswith(".") or part.startswith("-"):
            raise RepositorySecurityError("仓库 URL 路径含非法片段")
    normalized_path = "/".join(parts[1:])
    return f"https://{host}/{normalized_path}"


def validate_repository(
    *,
    kind: str,
    value: str,
    settings: Settings,
) -> ValidatedRepository:
    """Validate a repository reference and return a server-side identity."""
    cleaned = value.strip()
    if not cleaned:
        raise RepositorySecurityError("仓库来源不能为空")
    if kind == "demo":
        path = _demo_path(settings, cleaned)
        return ValidatedRepository(
            kind="demo",
            value=cleaned,
            label=f"demo/{cleaned}",
            local_path=path,
        )
    if kind != "https":
        raise RepositorySecurityError("仓库来源只支持 https 或服务端演示仓库")
    normalized = _validate_https(cleaned, settings.allowed_repository_hosts)
    label = normalized.removeprefix("https://")
    return ValidatedRepository(kind="https", value=normalized, label=label)


def validate_base_commit(value: str) -> str:
    """API accepts only ``HEAD`` or an explicit hexadecimal commit."""
    commit = value.strip()
    if not _BASE_COMMIT.fullmatch(commit):
        raise RepositorySecurityError("base_commit 必须是 HEAD 或 7-64 位十六进制提交号")
    return commit.upper() if commit == "HEAD" else commit


class PathScrubber:
    """Replace known absolute roots and redact secrets in API output."""

    def __init__(
        self,
        settings: Settings,
        *,
        extra_roots: dict[str, str] | None = None,
        scrub_absolute_paths: bool = True,
    ) -> None:
        self._scrub_absolute_paths = scrub_absolute_paths
        roots: dict[str, str] = {
            str(settings.artifacts_dir.resolve()): "<artifacts>",
            str(settings.cache_dir.resolve()): "<cache>",
            str(settings.api_demo_root.resolve()): "<demo>",
            str(PROJECT_ROOT.resolve()): "<project>",
        }
        if settings.api_queue_path is not None:
            roots[str(settings.api_queue_path.resolve().parent)] = "<api-data>"
        if extra_roots:
            roots.update(extra_roots)
        self._replacements: list[tuple[str, str]] = []
        for raw, label in sorted(roots.items(), key=lambda item: len(item[0]), reverse=True):
            if not raw:
                continue
            self._replacements.append((raw, label))
            self._replacements.append((raw.replace("\\", "/"), label))

    def text(self, value: str) -> str:
        result = value
        for raw, label in self._replacements:
            if os.name == "nt":
                result = re.sub(re.escape(raw), label, result, flags=re.IGNORECASE)
            else:
                result = result.replace(raw, label)
        if self._scrub_absolute_paths:
            result = _ABSOLUTE_PATH.sub("<path>", result)
        return redact_text(result)

    def payload(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.payload(item) for item in value]
        if isinstance(value, tuple):
            return [self.payload(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value
