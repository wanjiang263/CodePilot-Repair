"""Exact/whitespace-tolerant, ambiguity-safe Search/Replace patches (P-09/P-10)."""

from __future__ import annotations

import difflib
import os
import tempfile
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

__all__ = ["MatchLocation", "MatchStatus", "SearchReplaceResult", "apply_search_replace"]


class MatchStatus(StrEnum):
    APPLIED_EXACT = "applied_exact"
    APPLIED_TRAILING_WHITESPACE = "applied_trailing_whitespace"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class MatchLocation(BaseModel):
    start_line: int
    end_line: int
    snippet: str = ""
    similarity: float | None = None


class SearchReplaceResult(BaseModel):
    applied: bool
    status: MatchStatus
    matches: list[MatchLocation] = Field(default_factory=list)
    suggestions: list[MatchLocation] = Field(default_factory=list)


def apply_search_replace(path: Path, search: str, replace: str) -> SearchReplaceResult:
    if not search:
        raise ValueError("search block must not be empty")
    original = path.read_text(encoding="utf-8")
    exact_offsets = _all_offsets(original, search)
    if len(exact_offsets) > 1:
        return SearchReplaceResult(
            applied=False,
            status=MatchStatus.AMBIGUOUS,
            matches=[_location(original, offset, len(search)) for offset in exact_offsets],
        )
    if len(exact_offsets) == 1:
        offset = exact_offsets[0]
        updated = original[:offset] + replace + original[offset + len(search) :]
        _atomic_write(path, updated)
        return SearchReplaceResult(
            applied=True,
            status=MatchStatus.APPLIED_EXACT,
            matches=[_location(original, offset, len(search))],
        )

    tolerant = _trailing_whitespace_matches(original, search)
    if len(tolerant) > 1:
        return SearchReplaceResult(
            applied=False,
            status=MatchStatus.AMBIGUOUS,
            matches=[_line_location(original, start, end) for start, end in tolerant],
        )
    if len(tolerant) == 1:
        start, end = tolerant[0]
        lines = original.splitlines(keepends=True)
        updated = "".join([*lines[:start], replace, *lines[end:]])
        _atomic_write(path, updated)
        return SearchReplaceResult(
            applied=True,
            status=MatchStatus.APPLIED_TRAILING_WHITESPACE,
            matches=[_line_location(original, start, end)],
        )
    return SearchReplaceResult(
        applied=False,
        status=MatchStatus.NOT_FOUND,
        suggestions=_suggestions(original, search),
    )


def _all_offsets(text: str, needle: str) -> list[int]:
    offsets: list[int] = []
    start = 0
    while True:
        position = text.find(needle, start)
        if position < 0:
            return offsets
        offsets.append(position)
        start = position + 1


def _location(text: str, offset: int, length: int) -> MatchLocation:
    start_line = text.count("\n", 0, offset) + 1
    end_line = start_line + text[offset : offset + length].count("\n")
    return MatchLocation(
        start_line=start_line, end_line=end_line, snippet=text[offset : offset + length]
    )


def _trailing_whitespace_matches(text: str, search: str) -> list[tuple[int, int]]:
    source_lines = text.splitlines(keepends=True)
    search_lines = search.splitlines()
    if not search_lines:
        return []
    wanted = [line.rstrip(" \t") for line in search_lines]
    normalized = [line.rstrip("\r\n").rstrip(" \t") for line in source_lines]
    width = len(wanted)
    return [
        (start, start + width)
        for start in range(0, len(normalized) - width + 1)
        if normalized[start : start + width] == wanted
    ]


def _line_location(text: str, start: int, end: int) -> MatchLocation:
    lines = text.splitlines(keepends=True)
    return MatchLocation(start_line=start + 1, end_line=end, snippet="".join(lines[start:end]))


def _suggestions(text: str, search: str) -> list[MatchLocation]:
    lines = text.splitlines(keepends=True)
    width = max(1, len(search.splitlines()))
    candidates: list[MatchLocation] = []
    for start in range(max(1, len(lines) - width + 1)):
        snippet = "".join(lines[start : start + width])
        score = difflib.SequenceMatcher(None, search, snippet).ratio()
        candidates.append(
            MatchLocation(
                start_line=start + 1,
                end_line=min(len(lines), start + width),
                snippet=snippet,
                similarity=score,
            )
        )
    candidates.sort(key=lambda item: (-(item.similarity or 0.0), item.start_line))
    return candidates[:3]


def _atomic_write(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
