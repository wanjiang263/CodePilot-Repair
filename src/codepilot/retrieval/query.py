"""Rule-based issue extraction and shared code-aware tokenization."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from codepilot.schemas.common import TraceFrame
from codepilot.schemas.retrieval import RetrievalQuery

_WORD = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", re.UNICODE)
_FRAME = re.compile(
    r'File (?P<quote>["\'])(?P<file>[^\r\n]+?)(?P=quote), '
    r"line (?P<line>\d+), in (?P<function>[^\s]+)"
)
_PATH = re.compile(r"(?<![\w/\\])(?:[A-Za-z]:[/\\]|/)?(?:[\w.-]+[/\\])*[\w.-]+\.py\b")
_ERROR = re.compile(r"^[ \t]*([\w.]*(?:Error|Exception|Warning)):[ \t]*(.*)$", re.MULTILINE)
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "i",
        "if",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "when",
        "with",
        "not",
        "after",
        "before",
        "please",
        "should",
        "would",
        "file",
        "line",
        "traceback",
        "most",
        "recent",
        "call",
        "last",
    ]
)


def unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def tokenize(text: str) -> list[str]:
    """Keep whole identifiers and their components; retain document term frequency."""
    result: list[str] = []
    for match in _WORD.finditer(text):
        word = match.group()
        whole = word.lower()
        split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", word)
        split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", split)
        parts = re.split(r"[\s_.]+", split.lower())
        result.extend(token for token in unique([whole, *parts]) if token not in _STOP)
    return result


def _frame_path(raw: str, files: set[str], root: Path | None) -> tuple[str, bool]:
    path = raw.replace("\\", "/")
    if root is not None:
        prefix = root.resolve().as_posix().rstrip("/") + "/"
        if path.startswith(prefix):
            path = path[len(prefix) :]
    relative = PurePosixPath(path)
    if ".." in relative.parts or relative.is_absolute() or re.match(r"^[A-Za-z]:", path):
        return path, False
    path = relative.as_posix()
    return path, path in files


def build_query(
    issue: str, *, repo_files: Iterable[str] = (), repo_root: Path | None = None
) -> RetrievalQuery:
    files = {path.replace("\\", "/") for path in repo_files}
    raw_frames: list[str] = []
    frames: list[TraceFrame] = []
    for match in _FRAME.finditer(issue):
        path, internal = _frame_path(match["file"], files, repo_root)
        raw_frames.append(match[0])
        frames.append(
            TraceFrame(
                file=path, line=int(match["line"]), function=match["function"], in_repo=internal
            )
        )
    identifiers = [word for word in re.findall(r"`([^`\n]+)`", issue) if _WORD.fullmatch(word)]
    identifiers.extend(
        match.group()
        for match in _WORD.finditer(issue)
        if "_" in match.group()
        or "." in match.group()
        or re.search(r"[a-z][A-Z]|[A-Z]{2}[a-z]", match.group())
    )
    errors: list[str] = []
    for match in _ERROR.finditer(issue):
        identifiers.append(match[1])
        errors.extend([match[0].strip(), match[2].strip()])
    # Quoted diagnostic strings are useful even without an exception prefix.
    errors.extend(re.findall(r'(?<!\w)["\']([^"\'\r\n]+)["\']', _FRAME.sub("", issue)))
    identifiers.extend(frame.function for frame in frames if _WORD.fullmatch(frame.function))
    identifiers = unique(identifiers)
    paths = unique(
        [*(m.group().replace("\\", "/") for m in _PATH.finditer(issue)), *(f.file for f in frames)]
    )
    tests = unique(word for word in identifiers if word.rsplit(".", 1)[-1].startswith("test_"))
    return RetrievalQuery(
        raw_issue=issue,
        identifiers=identifiers,
        keywords=unique(tokenize(issue)),
        error_messages=unique(errors),
        file_paths=paths,
        traceback_frames=raw_frames,
        parsed_frames=frames,
        test_names=tests,
    )
