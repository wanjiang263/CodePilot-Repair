"""Independent lexical channels; no cross-channel fusion in V-01 through V-05."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from rank_bm25 import BM25Okapi

from codepilot.retrieval.documents import RetrievalError, SymbolDocument, source_path
from codepilot.retrieval.query import tokenize, unique
from codepilot.schemas.retrieval import ChannelScore, RankedSymbol, RetrievalQuery


def rank_candidates(
    documents: Sequence[SymbolDocument], scores: dict[str, float], channel: str
) -> list[RankedSymbol]:
    """Canonical channel order, including deterministic ties and explanation scores."""
    if not scores:
        return []
    low, high = min(scores.values()), max(scores.values())
    result = []
    for document in documents:
        symbol = document.symbol
        if symbol.id not in scores:
            continue
        raw = scores[symbol.id]
        normalized = 1.0 if high == low else (raw - low) / (high - low)
        result.append(
            RankedSymbol(
                symbol_id=symbol.id,
                qualified_name=symbol.qualified_name,
                file=symbol.path,
                kind=symbol.kind,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                score=raw,
                channel_scores=[ChannelScore(channel=channel, raw=raw, normalized=normalized)],
            )
        )
    return sorted(
        result, key=lambda s: (-s.score, s.file, s.start_line or 0, s.qualified_name, s.symbol_id)
    )


class BM25Channel:
    """BM25Okapi over symbol documents, using identical query/document tokenization."""

    name = "bm25"

    def __init__(self, documents: Sequence[SymbolDocument]) -> None:
        self.documents = list(documents)
        self.tokens = [tokenize(document.text) for document in self.documents]
        self.model = (
            BM25Okapi(self.tokens, k1=1.5, b=0.75, epsilon=0.25) if any(self.tokens) else None
        )

    def search(self, query: RetrievalQuery) -> list[RankedSymbol]:
        terms = unique(
            tokenize(" ".join([*query.identifiers, *query.keywords, *query.error_messages]))
        )
        if self.model is None or not terms:
            return []
        raw = self.model.get_scores(terms)
        wanted = set(terms)
        scores = {
            doc.symbol.id: float(raw[i])
            for i, doc in enumerate(self.documents)
            if wanted.intersection(self.tokens[i])
        }
        return rank_candidates(self.documents, scores, self.name)


def _json_text(value: dict[str, str]) -> str:
    if "text" in value:
        return value["text"]
    return base64.b64decode(value["bytes"]).decode("utf-8", errors="replace")


class RipgrepChannel:
    """Fixed-string source search mapped to the narrowest enclosing symbol."""

    name = "ripgrep"

    def __init__(
        self, root: Path, documents: Sequence[SymbolDocument], *, timeout: float = 30.0
    ) -> None:
        self.root = root.resolve()
        self.documents = list(documents)
        self.timeout = timeout

    def search(self, query: RetrievalQuery) -> list[RankedSymbol]:
        terms = unique([*query.identifiers, *query.error_messages, *query.keywords])
        terms = [
            term
            for term in terms
            if term.strip() and "\n" not in term and "\r" not in term and "\0" not in term
        ]
        if not terms or not self.documents:
            return []
        executable = shutil.which("rg")
        if executable is None:
            raise RetrievalError("ripgrep (rg) is required for the ripgrep channel")
        by_path: dict[str, list[SymbolDocument]] = defaultdict(list)
        for doc in self.documents:
            by_path[doc.symbol.path].append(doc)
        matches: dict[str, set[str]] = defaultdict(set)
        name_hits: set[str] = set()
        error_hits: set[str] = set()
        for doc in self.documents:
            if {doc.symbol.name, doc.symbol.qualified_name}.intersection(query.identifiers):
                name_hits.add(doc.symbol.id)
        # Explicit indexed paths preserve scanner exclusions; chunk for Windows argv limits.
        batches: list[list[str]] = [[]]
        size = 0
        for path in sorted(by_path):
            source_path(self.root, path)
            if size + len(path) > 12000 and batches[-1]:
                batches.append([])
                size = 0
            batches[-1].append(path)
            size += len(path) + 3
        for batch in batches:
            command = [
                executable,
                "--no-config",
                "--fixed-strings",
                "--json",
                "--line-number",
                "--no-ignore",
                "--hidden",
                "--color=never",
                "-f",
                "-",
                "--",
                *batch,
            ]
            try:
                completed = subprocess.run(
                    command,
                    input="\n".join(terms) + "\n",
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise RetrievalError(f"ripgrep failed: {error}") from error
            if completed.returncode not in (0, 1):
                raise RetrievalError(
                    f"ripgrep exited {completed.returncode}: {completed.stderr.strip()}"
                )
            for line in completed.stdout.splitlines():
                event = json.loads(line)
                if event["type"] != "match":
                    continue
                data = event["data"]
                path = _json_text(data["path"]).replace("\\", "/")
                number = data["line_number"]
                enclosing = [
                    doc
                    for doc in by_path[path]
                    if doc.symbol.start_line <= number <= doc.symbol.end_line
                ]
                if not enclosing:
                    continue
                symbol = min(
                    enclosing,
                    key=lambda d: (
                        d.symbol.end_line - d.symbol.start_line,
                        -d.symbol.start_line,
                        {"module": 2, "class": 1}.get(d.symbol.kind, 0),
                        d.symbol.qualified_name,
                        d.symbol.id,
                    ),
                ).symbol
                # Inspect the matching line, since rg reports non-overlapping submatches only.
                source = _json_text(data["lines"])
                matches[symbol.id].update(term for term in terms if term in source)
                if any(error and error in source for error in query.error_messages):
                    error_hits.add(symbol.id)
        scores = {
            sid: float(len(matches[sid]) + 4 * (sid in name_hits) + 3 * (sid in error_hits))
            for sid in set(matches) | name_hits
        }
        return rank_candidates(self.documents, scores, self.name)
