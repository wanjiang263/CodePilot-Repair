"""Compact indexed Python definitions; no imports or execution of repository code."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from codepilot.fsutil import workspace_source_path
from codepilot.repo.index import RepositoryIndex
from codepilot.repo.models import Symbol


class SkeletonError(ValueError):
    """Unsafe, unreadable, unindexed or stale repository input."""


@dataclass(frozen=True, slots=True)
class FileSkeleton:
    file_id: str
    path: str
    text: str
    line_count: int
    sha256: str
    symbols: tuple[Symbol, ...]
    parse_error: str | None = None


class SkeletonBuilder:
    """Reuse R-07 signatures and identity; verify the indexed source before use.

    Invalid syntax degrades to an explicit status with no trusted definitions.
    Empty files produce an empty definition list. Read/encoding/staleness errors
    fail explicitly. A per-call source cache supports non-overlapping symbol context.
    """

    def __init__(self, root: Path, index: RepositoryIndex) -> None:
        self.root = root
        self.files = {file.path: file for file in index.files()}
        self.symbols = {symbol.id: symbol for symbol in index.symbol_objects()}
        self._lines: dict[str, tuple[str, ...]] = {}
        self._built: dict[str, FileSkeleton] = {}

    def build(self, path: str) -> FileSkeleton:
        try:
            target = workspace_source_path(self.root, path)
        except ValueError as error:
            raise SkeletonError(str(error)) from error
        path = PurePosixPath(path.replace("\\", "/")).as_posix()
        file = self.files.get(path)
        if file is None or not path.endswith(".py"):
            raise SkeletonError(f"Unindexed Python file: {path}")
        if path in self._built:
            return self._built[path]
        try:
            raw = target.read_bytes()
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as error:
            raise SkeletonError(f"Cannot read/decode indexed file: {path}") from error
        digest = hashlib.sha256(raw).hexdigest()
        if digest != file.sha256:
            raise SkeletonError(f"stale index: {path}")
        lines = tuple(text.splitlines())
        self._lines[path] = lines
        definitions = (
            tuple(
                sorted(
                    (s for s in self.symbols.values() if s.path == path and s.kind != "module"),
                    key=lambda s: (s.start_line, s.qualified_name, s.id),
                )
            )
            if not file.parse_error
            else ()
        )
        blocks = []
        for symbol in definitions:
            if not 1 <= symbol.start_line <= symbol.end_line <= len(lines) or not symbol.signature:
                raise SkeletonError(f"Invalid indexed definition: {symbol.id}")
            blocks.append(
                f"[{symbol.id}] {symbol.qualified_name} @ {path}:{symbol.start_line}-{symbol.end_line}\n"
                + "\n".join(f"@{decorator}" for decorator in symbol.decorators)
                + ("\n" if symbol.decorators else "")
                + symbol.signature
            )
        skeleton = FileSkeleton(
            file.id, path, "\n\n".join(blocks), len(lines), digest, definitions, file.parse_error
        )
        self._built[path] = skeleton
        return skeleton

    def lines(self, path: str) -> tuple[str, ...]:
        self.build(path)
        return self._lines[path]
