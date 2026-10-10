"""Exact cosine retrieval over symbol documents (V-09)."""

from __future__ import annotations

import importlib
import warnings
from collections.abc import Sequence
from typing import Any, Literal

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

    def __init__(
        self,
        documents: Sequence[SymbolDocument],
        provider: EmbeddingProvider,
        *,
        backend: Literal["numpy", "faiss"] = "numpy",
    ) -> None:
        if backend not in ("numpy", "faiss"):
            raise ValueError(f"Unknown semantic backend: {backend}")
        self.documents = list(documents)
        ids = [doc.symbol.id for doc in documents]
        if any(not sid for sid in ids) or len(set(ids)) != len(ids):
            raise RetrievalError("Semantic documents require unique nonempty symbol IDs")
        self.provider = provider
        self.identity = provider.identity
        self.requested_backend = backend
        self.backend = backend
        self.fallback_reason: str | None = None
        self._faiss: Any = None
        self._index: Any = None
        self._vectors: Vectors | None = None
        if backend == "faiss":
            try:
                self._faiss = importlib.import_module("faiss")
            except (ImportError, OSError) as exc:
                self._fallback(
                    f"FAISS unavailable ({type(exc).__name__}); install the [faiss] extra"
                )

    def _fallback(self, reason: str) -> None:
        self.backend = "numpy"
        self.fallback_reason = reason
        self._index = None
        warnings.warn(
            f"{reason}; falling back to numpy cosine retrieval", RuntimeWarning, stacklevel=3
        )

    def _scores(self, query_vector: Vectors) -> Vectors:
        assert self._vectors is not None
        if self.backend == "faiss":
            try:
                if self._index is None:
                    self._index = self._faiss.IndexFlatIP(self.identity.dimensions)
                    self._index.add(np.ascontiguousarray(self._vectors, dtype=np.float32))
                # Fetch every candidate: min-max explanations and ties at the K
                # boundary must use the same full corpus as the NumPy backend.
                distances, indices = self._index.search(
                    np.ascontiguousarray(query_vector, dtype=np.float32), len(self.documents)
                )
                scores = validate_vectors(distances, 1, len(self.documents))[0]
                ids = np.asarray(indices)
                if (
                    ids.shape != (1, len(self.documents))
                    or ids.dtype.kind not in "iu"
                    or set(ids[0].tolist()) != set(range(len(self.documents)))
                ):
                    raise ValueError("Invalid FAISS candidate IDs")
                ordered = np.empty(len(self.documents), dtype=np.float64)
                ordered[ids[0]] = scores
                return np.clip(ordered, -1.0, 1.0)
            except (RuntimeError, ValueError, TypeError, AttributeError, OSError) as exc:
                # Malformed FAISS results also raise RetrievalError (RuntimeError).
                self._fallback(f"FAISS failed ({type(exc).__name__})")
        result: Vectors = np.clip(self._vectors @ query_vector[0], -1.0, 1.0)
        return result

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
        raw_scores = self._scores(query_vector)
        scores = {
            doc.symbol.id: float(raw_scores[index])
            for index, doc in enumerate(self.documents)
            if self._vectors[index].any()
        }
        # Normalize all candidates before slicing so Top-K never changes explanations.
        return rank_candidates(self.documents, scores, self.name)[:limit]
