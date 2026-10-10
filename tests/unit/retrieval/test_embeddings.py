import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from codepilot.retrieval.documents import RetrievalError
from codepilot.retrieval.embeddings import (
    EmbeddingIdentity,
    EmbeddingProvider,
    SentenceTransformerProvider,
    validate_vectors,
)


class FakeEmbedding:
    def __init__(self, identity=None):
        self.identity = identity or EmbeddingIdentity("fake", "code", 2, "v1")
        self.calls = []
        self.closed = False

    def embed(self, texts):
        self.calls.append(list(texts))
        return np.array([[len(text), 1] for text in texts], dtype=float).reshape(-1, 2)

    def close(self):
        self.closed = True


def test_provider_protocol_and_empty_batch():
    assert isinstance(FakeEmbedding(), EmbeddingProvider)
    provider = SentenceTransformerProvider("configured-code-model", dimensions=2, revision="v1")
    assert provider.embed([]).shape == (0, 2)
    assert provider._model is None
    provider.close()


def test_local_model_lazy_loading_options_and_batch_order(monkeypatch):
    calls = []

    class Model:
        def __init__(self, model, **options):
            calls.append((model, options))

        def encode(self, texts, **options):
            calls.append((texts, options))
            return [[len(text), 1] for text in texts]

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Model)
    )
    provider = SentenceTransformerProvider("local/code", dimensions=2, revision="sha", batch_size=4)
    assert calls == []
    np.testing.assert_array_equal(provider.embed(["long", "x"]), [[4, 1], [1, 1]])
    provider.embed(["y"])
    assert len(calls) == 3
    assert calls[0] == (
        "local/code",
        {"revision": "sha", "device": "cpu", "local_files_only": True, "trust_remote_code": False},
    )
    assert calls[1][1]["prompt"] == ""
    assert calls[1][1]["batch_size"] == 4
    provider.close()
    assert provider._model is None


def test_missing_local_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(RetrievalError, match=r"\[embeddings\]"):
        SentenceTransformerProvider("code", dimensions=2, revision="v1").embed(["x"])


@pytest.mark.parametrize(
    "raw",
    [
        [[1]],
        [[1, 2], [3, 4]],
        [[float("nan"), 1]],
        [[float("inf"), 1]],
        [[True, False]],
        [["1", "2"]],
        [[1j, 2]],
        [[1], [1, 2]],
        None,
    ],
)
def test_vector_validation(raw):
    with pytest.raises(RetrievalError):
        validate_vectors(raw, 1, 2)


@pytest.mark.parametrize(
    "options",
    [
        {"provider": "", "model": "m", "dimensions": 2, "revision": "v"},
        {"provider": "p", "model": "", "dimensions": 2, "revision": "v"},
        {"provider": "p", "model": "m", "dimensions": 0, "revision": "v"},
        {"provider": "p", "model": "m", "dimensions": True, "revision": "v"},
        {"provider": "p", "model": "m", "dimensions": 2, "revision": ""},
    ],
)
def test_identity_validation(options):
    with pytest.raises(ValueError):
        EmbeddingIdentity(**options)


def test_local_invalid_batch_size():
    with pytest.raises(ValueError):
        SentenceTransformerProvider("m", dimensions=2, revision="v", batch_size=0)


@pytest.mark.slow
def test_real_local_model_opt_in():
    model = os.environ.get("CODEPILOT_TEST_EMBEDDING_MODEL")
    if not model:
        pytest.skip("NOT RUN: set CODEPILOT_TEST_EMBEDDING_MODEL to a preinstalled code model")
    dimensions = int(os.environ["CODEPILOT_TEST_EMBEDDING_DIMENSIONS"])
    revision = os.environ["CODEPILOT_TEST_EMBEDDING_REVISION"]
    provider = SentenceTransformerProvider(model, dimensions=dimensions, revision=revision)
    try:
        assert provider.embed(["def authenticate(user): return user.is_active"]).shape == (
            1,
            dimensions,
        )
    finally:
        provider.close()
