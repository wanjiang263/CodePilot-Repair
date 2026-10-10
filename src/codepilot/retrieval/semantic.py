"""Exact cosine retrieval over symbol documents (V-09)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from codepilot.retrieval.documents import RetrievalError, SymbolDocument
from codepilot.retrieval.embeddings import EmbeddingProvider, Vectors, validate_vectors
from codepilot.retrieval.lexical import rank_candidates
from codepilot.schemas.retrieval import RankedSymbol, RetrievalQuery


def normalize_vectors(vectors: Vectors) -> Vectors:
    """Scale first to avoid overflow/underflow; zero rows stay zero."""
    scale = np.max(np.abs(vectors), axis=1, keepdims=True)
    scaled = np.divide(vectors, scale, out=np.zeros_like(vectors), where=scale != 0)
    norms = np.linalg.norm(scaled, axis=1, keepdims=True)
    return np.divide(scaled, norms, out=np.zeros_like(scaled), where=norms != 0)


class SemanticChannel:
    name = "semantic"

    def __init__(self, documents: Sequence[SymbolDocument], provider: EmbeddingProvider) -> None:
        self.documents = list(documents)
        ids = [doc.symbol.id for doc in documents]
        if any(not sid for sid in ids) or len(set(ids)) != len(ids):
            raise RetrievalError("Semantic documents require unique nonempty symbol IDs")
        self.provider = provider
        self.identity = provider.identity
        self.backend = "numpy"
        self._vectors: Vectors | None = None

    def search(self, query: RetrievalQuery, *, limit: int | None = None) -> list[RankedSymbol]:
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be positive")
        if not self.documents or not query.raw_issue.strip():
            return []
        if self.provider.identity != self.identity:
            raise RetrievalError("Embedding provider identity changed after index construction")
        if self._vectors is None:
            raw = validate_vectors(
                self.provider.embed([doc.text for doc in self.documents]),
                len(self.documents),
                self.identity.dimensions,
            )
            self._vectors = normalize_vectors(raw)
        query_vector = normalize_vectors(
            validate_vectors(self.provider.embed([query.raw_issue]), 1, self.identity.dimensions)
        )
        if not query_vector.any():
            return []  # Cosine is undefined for a zero vector.
        raw_scores = np.clip(self._vectors @ query_vector[0], -1.0, 1.0)
        scores = {
            doc.symbol.id: float(raw_scores[index])
            for index, doc in enumerate(self.documents)
            if self._vectors[index].any()
        }
        # Normalize all candidates before slicing so Top-K never changes explanations.
        return rank_candidates(self.documents, scores, self.name)[:limit]
