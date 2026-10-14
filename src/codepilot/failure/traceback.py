"""Parse observed frames without opening or importing any referenced file."""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from pathlib import PurePosixPath

from codepilot.schemas.common import TraceFrame

_PYTHON = re.compile(r'^\s*File "(.+)", line (\d+)(?:, in (.+))?\s*$')
_PYTEST = re.compile(r"^\s*(.+?\.py):(\d+):(?: in (.*)|.*)?$")


def path_identity(
    path: str, repo_files: Collection[str], repo_roots: Sequence[str]
) -> tuple[str, bool]:
    value = path.replace("\\", "/")
    # Traversal is never normalized into an authorized repository identity.
    if ".." in PurePosixPath(value).parts or "\x00" in value:
        return value, False
    for root in sorted(repo_roots, key=len, reverse=True):
        prefix = root.replace("\\", "/").rstrip("/") + "/"
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    value = PurePosixPath(value).as_posix()
    return value, value in repo_files


def parse_traceback(
    text: str, *, repo_files: Collection[str] = (), repo_roots: Sequence[str] = ()
) -> list[TraceFrame]:
    lines = text.splitlines()
    frames: list[TraceFrame] = []
    observed_function = ""
    observed_code: str | None = None
    for index, line in enumerate(lines):
        line = re.sub(r"^E\s+", "", line)
        if header := re.match(r"\s*(?:async )?def ([\w]+)\s*\(", line):
            observed_function = header[1]
        if line.startswith(">"):
            observed_code = line[1:].strip() or None
        match = _PYTHON.match(line) or _PYTEST.match(line)
        if match is None or int(match[2]) < 1:
            continue
        file, in_repo = path_identity(match[1], repo_files, repo_roots)
        code = observed_code if _PYTEST.match(line) else None
        if _PYTHON.match(line) and index + 1 < len(lines):
            following = re.sub(r"^E\s+", "    ", lines[index + 1])
            if following.startswith(("    ", "\t")) and not _PYTHON.match(following):
                code = following.strip() or None
        frames.append(
            TraceFrame(
                file=file,
                line=int(match[2]),
                function=(match[3] or observed_function),
                in_repo=in_repo,
                code=code,
            )
        )
        observed_function, observed_code = "", None
    return frames
