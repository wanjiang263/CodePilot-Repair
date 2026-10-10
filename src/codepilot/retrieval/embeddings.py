"""Synchronous, batch embeddings with an explicit model identity (V-06)."""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
import numpy as np
from numpy.typing import NDArray

from codepilot.llm.config import ProviderSpec
from codepilot.llm.errors import ProviderError, ProviderNotAllowedError, RateLimitError
from codepilot.llm.retry import ConcurrencyLimiter, RetryPolicy, call_with_retry
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


class OpenAICompatibleEmbeddingProvider:
    """Embedding endpoint using the L-layer configuration, retry and concurrency policy."""

    def __init__(
        self,
        *,
        provider: ProviderSpec,
        model: str,
        dimensions: int,
        revision: str,
        api_key: str,
        client: httpx.Client | None = None,
        policy: RetryPolicy | None = None,
        limiter: ConcurrencyLimiter | None = None,
        allowed_providers: Sequence[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if allowed_providers is not None and provider.name not in allowed_providers:
            raise ProviderNotAllowedError(
                "Embedding provider is not allowed", provider=provider.name
            )
        self._url = provider.base_url.rstrip("/") + "/embeddings"
        self.identity = EmbeddingIdentity(
            f"openai-compatible:{provider.name}:{self._url}", model, dimensions, revision
        )
        self.provider = provider
        self.policy = policy or RetryPolicy()
        self.limiter = limiter or ConcurrencyLimiter(provider.max_concurrency)
        self.sleep = sleep
        self._headers = {"Authorization": f"Bearer {api_key}", **provider.extra_headers}
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=provider.timeout_seconds)

    def embed(self, texts: Sequence[str]) -> Vectors:
        if not texts:
            return np.empty((0, self.identity.dimensions), dtype=np.float64)
        body: dict[str, object] = {
            "model": self.identity.model,
            "input": list(texts),
            "encoding_format": "float",
        }
        with self.limiter:
            response = call_with_retry(
                lambda: self._post(body), policy=self.policy, sleep=self.sleep
            )
        try:
            data = response.json()["data"]
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError("incorrect batch size")
            by_index = {}
            for item in data:
                index = item["index"]
                if type(index) is not int or index in by_index or not 0 <= index < len(texts):
                    raise ValueError("invalid embedding index")
                by_index[index] = item["embedding"]
            raw = [by_index[index] for index in range(len(texts))]
        except (ValueError, KeyError, TypeError) as exc:
            raise RetrievalError("Malformed embedding API response") from exc
        return validate_vectors(raw, len(texts), self.identity.dimensions)

    def _post(self, body: dict[str, object]) -> httpx.Response:
        try:
            response = self._client.post(
                self._url,
                json=body,
                headers=self._headers,
                timeout=self.provider.timeout_seconds,
            )
        except httpx.TransportError as exc:
            raise ProviderError(
                "Embedding transport failure",
                provider=self.provider.name,
                model_alias=self.identity.model,
            ) from exc
        if response.is_success:
            return response
        if response.status_code == 429:
            header = response.headers.get("retry-after", "")
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
            raise RateLimitError(
                "Embedding rate limit",
                status=429,
                retry_after=retry_after,
                provider=self.provider.name,
                model_alias=self.identity.model,
            )
        raise ProviderError(
            "Embedding request rejected",
            status=response.status_code,
            provider=self.provider.name,
            model_alias=self.identity.model,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatibleEmbeddingProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
