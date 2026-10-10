from dataclasses import replace

import numpy as np
import pytest

from codepilot.retrieval.documents import RetrievalError
from codepilot.retrieval.embeddings import CachedEmbeddingProvider
from support.retrieval import FakeEmbedding


def test_persistent_hits_dedup_order_and_no_source_saved(tmp_path):
    provider = FakeEmbedding()
    cache = CachedEmbeddingProvider(provider, tmp_path)
    first = cache.embed(["private source", "x", "private source"])
    assert provider.calls == [["private source", "x"]]
    np.testing.assert_array_equal(first, [[14, 1], [1, 1], [14, 1]])
    provider.calls.clear()
    first[0, 0] = 999  # Returned arrays cannot mutate persisted cache.
    restarted = CachedEmbeddingProvider(provider, tmp_path)
    np.testing.assert_array_equal(restarted.embed(["x", "private source"]), [[1, 1], [14, 1]])
    assert provider.calls == []
    assert len(list(tmp_path.glob("*.npy"))) == 2
    assert all(b"private source" not in path.read_bytes() for path in tmp_path.iterdir())
    restarted.embed(["x", "new"])
    assert provider.calls == [["new"]]
    restarted.close()
    assert provider.closed


@pytest.mark.parametrize(
    "change",
    [{"model": "other"}, {"provider": "other-endpoint"}, {"revision": "v2"}, {"dimensions": 3}],
)
def test_cache_identity_isolation(tmp_path, change):
    first = FakeEmbedding()
    cache1 = CachedEmbeddingProvider(first, tmp_path)
    cache1.embed(["same"])
    second = FakeEmbedding(replace(first.identity, **change))
    cache2 = CachedEmbeddingProvider(second, tmp_path)
    assert cache1.cache_key("same") != cache2.cache_key("same")
    if second.identity.dimensions == 3:
        second.embed = lambda texts: np.ones((len(texts), 3))
    cache2.embed(["same"])
    assert len(list(tmp_path.glob("*.npy"))) == 2
    if second.identity.dimensions == 2:
        assert second.calls == [["same"]]


def test_cache_hash_uses_exact_unicode_content(tmp_path):
    cache = CachedEmbeddingProvider(FakeEmbedding(), tmp_path)
    assert cache.cache_key("é") != cache.cache_key("e\u0301")
    assert cache.cache_key("x") != cache.cache_key("x\n")
    assert len(cache.cache_key("中文")) == 64


def test_empty_cache_batch_no_calls(tmp_path):
    provider = FakeEmbedding()
    assert CachedEmbeddingProvider(provider, tmp_path).embed([]).shape == (0, 2)
    assert provider.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("corruption", [b"broken", None, "shape", "nan", "object"])
def test_corrupt_cache_recomputed(tmp_path, corruption, caplog):
    provider = FakeEmbedding()
    cache = CachedEmbeddingProvider(provider, tmp_path)
    cache.embed(["x"])
    path = next(tmp_path.glob("*.npy"))
    if corruption in (b"broken", None):
        path.write_bytes(corruption or b"")
    else:
        arrays = {"shape": [[1]], "nan": [[float("nan"), 1]], "object": [[{}, {}]]}
        with path.open("wb") as stream:
            np.save(stream, arrays[corruption])
    provider.calls.clear()
    np.testing.assert_array_equal(cache.embed(["x"]), [[1, 1]])
    assert provider.calls == [["x"]]
    assert "recomputing" in caplog.text


def test_failed_batch_not_cached(tmp_path):
    provider = FakeEmbedding()
    provider.embed = lambda texts: [[1]]
    cache = CachedEmbeddingProvider(provider, tmp_path)
    with pytest.raises(RetrievalError):
        cache.embed(["x", "y"])
    assert list(tmp_path.iterdir()) == []


def test_changed_provider_identity_rejected(tmp_path):
    provider = FakeEmbedding()
    cache = CachedEmbeddingProvider(provider, tmp_path)
    provider.identity = replace(provider.identity, model="changed")
    with pytest.raises(RetrievalError, match="identity changed"):
        cache.embed(["x"])


def test_atomic_write_failure_cleans_temp(tmp_path, monkeypatch):
    from pathlib import Path

    cache = CachedEmbeddingProvider(FakeEmbedding(), tmp_path)

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        cache.embed(["x"])
    assert list(tmp_path.iterdir()) == []
