"""Explicit non-learned token-hash vector baseline for offline CLI evaluation.

This exercises the real cosine channel; it measures lexical feature hashing,
not the effectiveness of a trained semantic embedding model. No network calls,
Python hash randomization, labels or repository-specific lookup tables.
"""

import hashlib
from collections import Counter
from collections.abc import Sequence

import numpy as np

from codepilot.retrieval.embeddings import EmbeddingIdentity, Vectors
from codepilot.retrieval.query import tokenize

OFFLINE_DESCRIPTION = "offline token-hash baseline; not a learned semantic model"


class OfflineHashEmbedding:
    identity = EmbeddingIdentity("offline-hash", "sha256-token-count", 256, "v1")

    def embed(self, texts: Sequence[str]) -> Vectors:
        vectors = np.zeros((len(texts), self.identity.dimensions), dtype=np.float64)
        for row, text in enumerate(texts):
            for token, count in sorted(Counter(tokenize(text)).items()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                column = int.from_bytes(digest[:8], "big") % self.identity.dimensions
                vectors[row, column] += count if digest[8] & 1 else -count
        return vectors

    def close(self) -> None:
        """No external resources are owned by this baseline."""
