"""Single-channel compatibility and configurable channel collection (V-14)."""

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from codepilot.repo.git_history import FileChurn
from codepilot.repo.models import Symbol
from codepilot.retrieval.config import CHANNELS, RetrievalConfig, SeedChannel, ServiceChannel
from codepilot.retrieval.documents import SymbolDocument, build_documents
from codepilot.retrieval.embeddings import EmbeddingProvider
from codepilot.retrieval.graph_source import GraphSource
from codepilot.retrieval.history import HistoryChannel
from codepilot.retrieval.lexical import BM25Channel, RipgrepChannel
from codepilot.retrieval.query import build_query
from codepilot.retrieval.semantic import SemanticChannel
from codepilot.retrieval.structural import StructuralChannel
from codepilot.retrieval.test_proximity import TestProximityChannel
from codepilot.schemas.retrieval import (
    FusionMethod,
    RankedSymbol,
    RetrievalQuery,
    RetrievalResult,
)

_GRAPH_CHANNELS = ("structural", "test")


def _query_candidates(
    channel: SeedChannel,
    root: Path,
    documents: Sequence[SymbolDocument],
    query: RetrievalQuery,
    embedding_provider: EmbeddingProvider | None,
    semantic_backend: Literal["numpy", "faiss"],
) -> list[RankedSymbol]:
    """Issue-text channels; also the default seed source for the structural channel."""
    if channel == "semantic":
        if embedding_provider is None:
            raise ValueError("semantic channel requires an embedding_provider")
        return SemanticChannel(documents, embedding_provider, backend=semantic_backend).search(
            query
        )
    if channel == "ripgrep":
        return RipgrepChannel(root, documents).search(query)
    return BM25Channel(documents).search(query)


def retrieve_symbols(
    root: Path,
    symbols: Sequence[Symbol],
    issue: str,
    *,
    channel: ServiceChannel = "bm25",
    limit: int = 20,
    embedding_provider: EmbeddingProvider | None = None,
    semantic_backend: Literal["numpy", "faiss"] = "numpy",
    graph: GraphSource | None = None,
    seeds: Sequence[RankedSymbol] | None = None,
    seed_channel: SeedChannel = "bm25",
    churn: Mapping[str, FileChurn] | None = None,
    config: RetrievalConfig | None = None,
) -> RetrievalResult:
    if limit < 1:
        raise ValueError("limit must be positive")
    if config is not None:
        return _configured_result(
            root,
            symbols,
            issue,
            config,
            limit,
            embedding_provider,
            semantic_backend,
            graph,
            seeds,
            seed_channel,
            churn,
        )
    if channel not in ("bm25", "ripgrep", "semantic", "structural", "test", "history"):
        raise ValueError(f"Unknown retrieval channel: {channel}")
    if channel in _GRAPH_CHANNELS and graph is None:
        raise ValueError(f"{channel} channel requires a graph")
    if channel == "history" and churn is None:
        raise ValueError("history channel requires churn from GitHistory.file_churn()")
    documents = build_documents(root, symbols)
    query = build_query(issue, repo_files=(s.path for s in symbols), repo_root=root)
    if channel == "structural":
        assert graph is not None
        # Seeds default to the lexical channel's full ranking; the structural
        # channel truncates to its own Top-20.
        source = (
            seeds
            if seeds is not None
            else _query_candidates(
                seed_channel, root, documents, query, embedding_provider, semantic_backend
            )
        )
        candidates = StructuralChannel(graph, documents).search(source)
    elif channel == "test":
        assert graph is not None
        candidates = TestProximityChannel(graph, documents).search(query)
    elif channel == "history":
        assert churn is not None
        candidates = HistoryChannel(documents, churn).search()
    else:
        candidates = _query_candidates(
            channel, root, documents, query, embedding_provider, semantic_backend
        )
    return RetrievalResult(
        query=query,
        symbols=candidates[:limit],
        per_channel={channel: candidates},
        enabled_channels=[channel],
        fusion_method=FusionMethod.NONE,
    )


def _configured_result(
    root: Path,
    symbols: Sequence[Symbol],
    issue: str,
    config: RetrievalConfig,
    limit: int,
    embedding_provider: EmbeddingProvider | None,
    semantic_backend: Literal["numpy", "faiss"],
    graph: GraphSource | None,
    seeds: Sequence[RankedSymbol] | None,
    seed_channel: SeedChannel,
    churn: Mapping[str, FileChurn] | None,
) -> RetrievalResult:
    """Collect full raw rankings. Cross-channel ranking is a separate fusion step.

    Structural seeds come from an enabled upstream channel or explicit ``seeds``;
    collection never runs a disabled channel behind the caller's back.
    """
    enabled = config.enabled_channels
    if "semantic" in enabled and embedding_provider is None:
        raise ValueError("semantic channel requires an embedding_provider")
    for name in _GRAPH_CHANNELS:
        if name in enabled and graph is None:
            raise ValueError(f"{name} channel requires a graph")
    if "history" in enabled and churn is None:
        raise ValueError("history channel requires churn from GitHistory.file_churn()")
    if "ripgrep" in enabled and shutil.which("rg") is None:
        raise ValueError("ripgrep channel requires the rg executable")
    if "structural" in enabled and seeds is None and seed_channel not in enabled:
        raise ValueError("structural seed_channel must be enabled or explicit seeds provided")
    documents = build_documents(root, symbols)
    query = build_query(issue, repo_files=(s.path for s in symbols), repo_root=root)
    collected: dict[str, list[RankedSymbol]] = {}
    for name in CHANNELS:
        if name not in enabled:
            continue
        if name == "structural":
            assert graph is not None
            source = seeds if seeds is not None else collected[seed_channel]
            candidates = StructuralChannel(graph, documents).search(source)
        elif name == "test":
            assert graph is not None
            candidates = TestProximityChannel(graph, documents).search(query)
        elif name == "history":
            assert churn is not None
            candidates = HistoryChannel(documents, churn).search()
        else:
            candidates = _query_candidates(
                name, root, documents, query, embedding_provider, semantic_backend
            )
        collected[name] = candidates
    return RetrievalResult(
        query=query,
        symbols=collected[enabled[0]][:limit] if len(enabled) == 1 else [],
        per_channel=collected,
        enabled_channels=list(enabled),
        fusion_method=FusionMethod.NONE,
    )
