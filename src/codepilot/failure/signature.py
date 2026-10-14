"""Frozen §14.4 SHA1 fingerprint, deliberately independent of test ID and line."""

from __future__ import annotations

import hashlib
import re

from codepilot.failure.schemas import ParsedFailure


def normalize(message: str) -> str:
    value = message.replace("\\", "/")
    value = re.sub(r"0x[0-9a-fA-F]+\b", "<address>", value)
    value = re.sub(
        r"(?i)(?:[a-z]:)?/(?:[^\s'\"<>:]+/)*(?:tmp|temp|pytest-of-[^/\s]+)/(?:[^\s'\"<>:]+)",
        "<temp-path>",
        value,
    )
    value = re.sub(r"(['\"])([^'\"\n]{80,})\1", "'<long-string>'", value)
    value = re.sub(r"(?<![\w])\d+(?:\.\d+)?(?![\w])", "<number>", value)
    return " ".join(value.split())


def failure_signature(failure: ParsedFailure) -> str:
    frame = failure.deepest_repo_frame
    location = f"{frame.file.replace(chr(92), '/')}:{frame.function}" if frame else "no_repo_frame"
    payload = f"{failure.exception_type}|{location}|{normalize(failure.message)}"
    return hashlib.sha1(payload.encode("utf-8", "surrogatepass")).hexdigest()


def execution_signatures(failures: list[ParsedFailure]) -> list[str]:
    return sorted({failure_signature(failure) for failure in failures})
