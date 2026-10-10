import sys
from types import SimpleNamespace

import numpy as np
import pytest

from codepilot.retrieval.semantic import SemanticChannel
from codepilot.schemas.retrieval import RetrievalQuery
from support.retrieval import MappingEmbedding, document, fixture_channel


class FakeFlatIP:
    def __init__(self, dimensions):
        self.dimensions = dimensions
        self.vectors = None
        self.calls = []

    def add(self, vectors):
        assert vectors.dtype == np.float32 and vectors.flags.c_contiguous
        assert vectors.shape[1] == self.dimensions
        self.vectors = vectors

    def search(self, queries, k):
        assert queries.dtype == np.float32 and queries.flags.c_contiguous
        self.calls.append(k)
        scores = queries @ self.vectors.T
        # Deliberately reverse equal-score candidates, like an unstable native index.
        ids = np.argsort(scores, axis=1)[:, ::-1][:, :k]
        return np.take_along_axis(scores, ids, axis=1), ids


def test_default_backend_does_not_import_faiss(monkeypatch):
    def fail(name):
        raise AssertionError("optional import on default path")

    monkeypatch.setattr("codepilot.retrieval.semantic.importlib.import_module", fail)
    channel, query, _ = fixture_channel()
    assert channel.backend == "numpy" and channel.search(query)


def test_missing_faiss_falls_back_with_clear_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", None)
    channel, query, provider = fixture_channel()
    expected = channel.search(query)
    with pytest.warns(RuntimeWarning, match=r"\[faiss\].*falling back to numpy"):
        fallback = SemanticChannel(channel.documents, provider, backend="faiss")
    assert fallback.requested_backend == "faiss" and fallback.backend == "numpy"
    assert fallback.fallback_reason is not None
    assert fallback.search(query) == expected


def test_faiss_binary_load_error_falls_back(monkeypatch):
    def fail(name):
        raise OSError("missing native library")

    monkeypatch.setattr("codepilot.retrieval.semantic.importlib.import_module", fail)
    with pytest.warns(RuntimeWarning, match="FAISS unavailable"):
        assert SemanticChannel([], MappingEmbedding({}), backend="faiss").backend == "numpy"


def test_explicit_faiss_matches_cosine_and_reuses_index(monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", SimpleNamespace(IndexFlatIP=FakeFlatIP))
    channel, query, provider = fixture_channel()
    expected = channel.search(query)
    faiss = SemanticChannel(channel.documents, provider, backend="faiss")
    result = faiss.search(query, limit=2)
    assert faiss.backend == "faiss" and faiss.fallback_reason is None
    assert [c.symbol_id for c in result] == [c.symbol_id for c in expected[:2]]
    assert [c.score for c in result] == pytest.approx([c.score for c in expected[:2]], abs=1e-6)
    assert [c.channel_scores[0].normalized for c in result] == pytest.approx(
        [c.channel_scores[0].normalized for c in expected[:2]], abs=1e-6
    )
    index = faiss._index
    faiss.search(query, limit=1)
    assert faiss._index is index and index.calls == [5, 5]


def test_faiss_ties_at_top_k_boundary_are_canonical(monkeypatch):
    monkeypatch.setitem(sys.modules, "faiss", SimpleNamespace(IndexFlatIP=FakeFlatIP))
    docs = [document("a", "a"), document("c", "c"), document("b", "b")]
    provider = MappingEmbedding({"a": [1, 0], "b": [1, 0], "c": [1, 0], "issue": [1, 0]})
    result = SemanticChannel(docs, provider, backend="faiss").search(
        RetrievalQuery(raw_issue="issue"), limit=2
    )
    assert [c.symbol_id for c in result] == ["a", "b"]


@pytest.mark.parametrize(
    "failure", ["create", "add", "search", "scores", "ids", "wrong-shape", "float-ids"]
)
def test_faiss_runtime_failure_falls_back_once(monkeypatch, failure):
    class BrokenIndex(FakeFlatIP):
        def __init__(self, dimensions):
            if failure == "create":
                raise RuntimeError("broken")
            super().__init__(dimensions)

        def add(self, vectors):
            if failure == "add":
                raise RuntimeError("broken")
            super().add(vectors)

        def search(self, queries, k):
            if failure == "search":
                raise RuntimeError("broken")
            scores, ids = super().search(queries, k)
            if failure == "scores":
                scores[0, 0] = float("nan")
            elif failure == "ids":
                ids[0, 0] = -1
            elif failure == "wrong-shape":
                ids = ids[0]
            elif failure == "float-ids":
                ids = ids.astype(float)
            return scores, ids

    monkeypatch.setitem(sys.modules, "faiss", SimpleNamespace(IndexFlatIP=BrokenIndex))
    channel, query, provider = fixture_channel()
    expected = channel.search(query)
    faiss = SemanticChannel(channel.documents, provider, backend="faiss")
    with pytest.warns(RuntimeWarning, match="FAISS failed.*falling back to numpy"):
        assert faiss.search(query) == expected
    assert faiss.backend == "numpy" and faiss._index is None
    assert faiss.search(query) == expected


def test_unknown_backend_rejected():
    with pytest.raises(ValueError, match="Unknown semantic backend"):
        SemanticChannel([], MappingEmbedding({}), backend="unknown")


def test_real_faiss_if_installed():
    pytest.importorskip("faiss", reason="NOT RUN: optional [faiss] package unavailable")
    channel, query, provider = fixture_channel()
    expected = channel.search(query)
    faiss = SemanticChannel(channel.documents, provider, backend="faiss")
    result = faiss.search(query)
    assert faiss.backend == "faiss"
    assert [c.symbol_id for c in result] == [c.symbol_id for c in expected]
    assert [c.score for c in result] == pytest.approx([c.score for c in expected], abs=1e-6)
