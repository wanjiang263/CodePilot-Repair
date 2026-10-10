"""Minimal single-channel acceptance path; full Phase 2 fusion is still pending."""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from codepilot.repo.models import Symbol
from codepilot.retrieval.documents import build_documents
from codepilot.retrieval.lexical import BM25Channel, RipgrepChannel
from codepilot.retrieval.query import build_query
from codepilot.schemas.retrieval import FusionMethod, RetrievalResult


def retrieve_symbols(
    root: Path,
    symbols: Sequence[Symbol],
    issue: str,
    *,
    channel: Literal["bm25", "ripgrep"] = "bm25",
    limit: int = 20,
) -> RetrievalResult:
    if limit < 1:
        raise ValueError("limit must be positive")
    if channel not in ("bm25", "ripgrep"):
        raise ValueError(f"Unknown retrieval channel: {channel}")
    documents = build_documents(root, symbols)
    query = build_query(issue, repo_files=(s.path for s in symbols), repo_root=root)
    candidates = (
        BM25Channel(documents).search(query)
        if channel == "bm25"
        else RipgrepChannel(root, documents).search(query)
    )
    return RetrievalResult(
        query=query,
        symbols=candidates[:limit],
        per_channel={channel: candidates},
        enabled_channels=[channel],
        fusion_method=FusionMethod.NONE,
    )
