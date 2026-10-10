"""One bounded, source-grounded document per indexed symbol."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from codepilot.fsutil import is_link
from codepilot.repo.models import Symbol


class RetrievalError(RuntimeError):
    """Retrieval could not inspect its declared inputs."""


def source_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in relative:
        raise RetrievalError(f"Unsafe indexed path: {relative}")
    target = root
    for part in path.parts:
        target = target / part
        if is_link(target):
            raise RetrievalError(f"Linked indexed path: {relative}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise RetrievalError(f"Indexed path escapes repository: {relative}")
    return target


@dataclass(frozen=True, slots=True)
class SymbolDocument:
    symbol: Symbol
    text: str
    code: str
    truncated: bool


def build_documents(root: Path, symbols: Iterable[Symbol]) -> list[SymbolDocument]:
    lines_by_path: dict[str, list[str]] = {}
    result: list[SymbolDocument] = []
    seen: set[str] = set()
    for symbol in sorted(symbols, key=lambda s: (s.path, s.start_line, s.qualified_name, s.id)):
        if not symbol.id or symbol.id in seen:
            raise RetrievalError(f"Missing or duplicate symbol ID: {symbol.id!r}")
        seen.add(symbol.id)
        if symbol.path not in lines_by_path:
            try:
                lines_by_path[symbol.path] = (
                    source_path(root, symbol.path)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
            except OSError as error:
                raise RetrievalError(f"Cannot read indexed file: {symbol.path}") from error
        lines = lines_by_path[symbol.path]
        if not 1 <= symbol.start_line <= symbol.end_line <= max(1, len(lines)):
            raise RetrievalError(f"Invalid symbol range: {symbol.qualified_name}")
        body = lines[symbol.start_line - 1 : symbol.end_line]
        truncated = len(body) > 60
        if truncated:
            body = [*body[:40], "# ... truncated ...", *body[-10:]]
        code = "\n".join(body)
        text = (
            f"path: {symbol.path}\nsymbol: {symbol.qualified_name}\n"
            f"signature: {symbol.signature or ''}\ndocstring: {symbol.docstring or ''}\n"
            f"code: {code}"
        )
        result.append(SymbolDocument(symbol=symbol, text=text, code=code, truncated=truncated))
    return result
