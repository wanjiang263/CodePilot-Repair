"""测试日志截断与脱敏后持久化。"""

from __future__ import annotations

from dataclasses import dataclass

from codepilot.artifacts import STDERR_LOG, STDOUT_LOG, ArtifactStore
from codepilot.observability.redaction import redact_text

__all__ = ["LogArtifacts", "persist_logs", "tail_utf8"]


@dataclass(frozen=True)
class LogArtifacts:
    stdout_tail: str
    stderr_tail: str
    artifact_dir: str


def tail_utf8(text: str, limit_bytes: int) -> str:
    if limit_bytes < 0:
        raise ValueError("日志 tail 上限不能为负数")
    if limit_bytes == 0:
        return ""
    payload = text.encode("utf-8", errors="replace")
    if len(payload) <= limit_bytes:
        return text
    return payload[-limit_bytes:].decode("utf-8", errors="replace")


def persist_logs(
    artifacts: ArtifactStore,
    *,
    iteration: int,
    stdout: str,
    stderr: str,
    tail_bytes: int,
) -> LogArtifacts:
    """先脱敏再写完整日志；任一写入失败会抛出，调用方不得伪造路径。"""
    safe_stdout = _clean_utf8(redact_text(stdout))
    safe_stderr = _clean_utf8(redact_text(stderr))
    artifacts.write_text(artifacts.attempt_relpath(iteration, STDOUT_LOG), safe_stdout)
    artifacts.write_text(artifacts.attempt_relpath(iteration, STDERR_LOG), safe_stderr)
    directory = artifacts.attempt_dir(iteration)
    return LogArtifacts(
        stdout_tail=tail_utf8(safe_stdout, tail_bytes),
        stderr_tail=tail_utf8(safe_stderr, tail_bytes),
        artifact_dir=str(directory),
    )


def _clean_utf8(text: str) -> str:
    """把孤立 surrogate 等不可编码字符替换掉，保证 artifact 总能以 UTF-8 写出。"""
    return text.encode("utf-8", errors="replace").decode("utf-8")
