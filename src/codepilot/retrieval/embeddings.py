"""Synchronous, batch embeddings with an explicit model identity (V-06)."""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from codepilot.retrieval.documents import RetrievalError

type Vectors = NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    """Revision is a model/deployment version, never an API key or alias alone."""

    provider: str
    model: str
    dimensions: int
    revision: str

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip() or not self.revision.strip():
            raise ValueError("provider, model and revision must be nonempty")
        if isinstance(self.dimensions, bool) or self.dimensions < 1:
            raise ValueError("dimensions must be positive")


def validate_vectors(raw: object, count: int, dimensions: int) -> Vectors:
    """Reject malformed batches before they reach a cache or ranking channel."""
    try:
        array = np.asarray(raw)
        if array.dtype.kind not in "fiu":
            raise ValueError("vectors must contain real numbers")
        vectors = np.asarray(array, dtype=np.float64)
    except (ValueError, TypeError, OverflowError) as exc:
        raise RetrievalError("Invalid embedding vectors") from exc
    if vectors.shape != (count, dimensions) or not np.isfinite(vectors).all():
        raise RetrievalError(f"Expected finite embedding batch of shape {(count, dimensions)}")
    return vectors.copy()


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def identity(self) -> EmbeddingIdentity: ...

    def embed(self, texts: Sequence[str]) -> Vectors:
        """Return finite (len(texts), dimensions) vectors in input order."""
        ...

    def close(self) -> None: ...


class SentenceTransformerProvider:
    """Lazy optional dependency; model ID/path and revision are caller configuration."""

    def __init__(
        self,
        model: str,
        *,
        dimensions: int,
        revision: str,
        batch_size: int = 32,
        device: str = "cpu",
        local_files_only: bool = True,
    ) -> None:
        self.identity = EmbeddingIdentity("sentence-transformers", model, dimensions, revision)
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size
        self.device = device
        self.local_files_only = local_files_only
        self._model: Any = None

    def embed(self, texts: Sequence[str]) -> Vectors:
        if not texts:
            return np.empty((0, self.identity.dimensions), dtype=np.float64)
        if self._model is None:
            try:
                module = importlib.import_module("sentence_transformers")
            except ImportError as exc:
                raise RetrievalError(
                    "Install the [embeddings] extra for sentence-transformers"
                ) from exc
            self._model = module.SentenceTransformer(
                self.identity.model,
                revision=self.identity.revision,
                device=self.device,
                local_files_only=self.local_files_only,
                trust_remote_code=False,
            )
        raw = self._model.encode(
            list(texts),
            batch_size=self.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
            prompt="",
        )
        return validate_vectors(raw, len(texts), self.identity.dimensions)

    def close(self) -> None:
        self._model = None
