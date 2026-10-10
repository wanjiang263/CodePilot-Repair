import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from codepilot.repo.models import Symbol
from codepilot.retrieval.documents import RetrievalError, SymbolDocument
from codepilot.retrieval.embeddings import CachedEmbeddingProvider, EmbeddingIdentity
from codepilot.retrieval.semantic import SemanticChannel
from codepilot.retrieval.service import retrieve_symbols
from codepilot.schemas.retrieval import RetrievalQuery, RetrievalResult

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/retrieval/semantic-vectors.json"


class MappingEmbedding:
    def __init__(self, mapping):
        self.mapping = mapping
        self.identity = EmbeddingIdentity("fake", "code", 2, "v1")
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return np.array([self.mapping[text] for text in texts])

    def close(self):
        pass


def document(sid, text, path="a.py", start=1, qualified_name=None):
    return SymbolDocument(
        Symbol(
            sid,
            qualified_name or sid,
            "function",
            id=sid,
            path=path,
            start_line=start,
            end_line=start,
        ),
        text,
        text,
        False,
    )


def fixture_channel(provider_wrapper=lambda p: p):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    docs = [document(row["id"], row["text"], row["path"]) for row in fixture["documents"]]
    mapping = {row["text"]: row["vector"] for row in fixture["documents"]}
    mapping[fixture["issue"]] = fixture["query_vector"]
    provider = MappingEmbedding(mapping)
    return (
        SemanticChannel(docs, provider_wrapper(provider)),
        RetrievalQuery(raw_issue=fixture["issue"]),
        provider,
    )


def test_hand_cosine_negative_zero_vectors_top_k_and_normalization():
    channel, query, provider = fixture_channel()
    assert channel.backend == "numpy"
    assert provider.calls == []
    candidates = channel.search(query)
    assert [c.symbol_id for c in candidates] == ["same", "y", "x", "negative"]
    assert [c.score for c in candidates] == pytest.approx([1, 0.8, 0.6, -1])
    assert [c.channel_scores[0].normalized for c in candidates] == pytest.approx([1, 0.9, 0.8, 0])
    assert all(c.channel_scores[0].channel == "semantic" for c in candidates)
    assert channel.search(query, limit=2) == candidates[:2]
    assert channel.search(query, limit=100) == candidates
    assert len(provider.calls) == 4  # Index once, query on each search.


def test_deterministic_ties_independent_of_input_order():
    docs = [
        document("z", "z", "b.py"),
        document("b", "b", start=2),
        document("d", "d", qualified_name="same"),
        document("c", "c", qualified_name="same"),
        document("a", "a", qualified_name="aaa"),
    ]
    mapping = {doc.text: [1, 0] for doc in docs} | {"issue": [1, 0]}
    query = RetrievalQuery(raw_issue="issue")
    forward = SemanticChannel(docs, MappingEmbedding(mapping)).search(query, limit=3)
    backward = SemanticChannel(list(reversed(docs)), MappingEmbedding(mapping)).search(
        query, limit=3
    )
    assert forward == backward
    assert [c.symbol_id for c in forward] == ["a", "c", "d"]
    assert all(c.channel_scores[0].normalized == 1 for c in forward)


def test_empty_index_and_blank_query_no_provider_calls():
    provider = MappingEmbedding({})
    assert SemanticChannel([], provider).search(RetrievalQuery(raw_issue="issue")) == []
    assert (
        SemanticChannel([document("x", "x")], provider).search(RetrievalQuery(raw_issue=" \n"))
        == []
    )
    assert provider.calls == []


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_limit(limit):
    channel, query, _ = fixture_channel()
    with pytest.raises(ValueError):
        channel.search(query, limit=limit)


def test_zero_query_returns_empty():
    provider = MappingEmbedding({"x": [1, 2], "issue": [0, 0]})
    assert (
        SemanticChannel([document("x", "x")], provider).search(RetrievalQuery(raw_issue="issue"))
        == []
    )


def test_all_zero_index_returns_empty():
    provider = MappingEmbedding({"x": [0, 0], "issue": [1, 2]})
    assert (
        SemanticChannel([document("x", "x")], provider).search(RetrievalQuery(raw_issue="issue"))
        == []
    )


@pytest.mark.parametrize("scale", [1e300, 1e-300])
def test_extreme_vector_magnitudes(scale):
    provider = MappingEmbedding({"x": [3 * scale, 4 * scale], "issue": [3 * scale, 4 * scale]})
    result = SemanticChannel([document("x", "x")], provider).search(
        RetrievalQuery(raw_issue="issue")
    )
    assert result[0].score == pytest.approx(1)


def test_index_and_query_cache_hits_after_rebuild(tmp_path):
    channel, query, provider = fixture_channel(lambda p: CachedEmbeddingProvider(p, tmp_path))
    expected = channel.search(query)
    provider.calls.clear()
    assert (
        SemanticChannel(
            list(reversed(channel.documents)), CachedEmbeddingProvider(provider, tmp_path)
        ).search(query)
        == expected
    )
    assert provider.calls == []


@pytest.mark.parametrize("vectors", [[1, 2], [float("nan"), 1]])
def test_invalid_index_provider_vectors(vectors):
    provider = MappingEmbedding({"x": vectors, "issue": [1, 2]})
    if vectors == [1, 2]:
        provider.embed = lambda texts: np.array([1, 2])
    with pytest.raises(RetrievalError):
        SemanticChannel([document("x", "x")], provider).search(RetrievalQuery(raw_issue="issue"))


def test_invalid_query_vectors():
    provider = MappingEmbedding({"x": [1, 2], "issue": [1]})
    with pytest.raises(RetrievalError):
        SemanticChannel([document("x", "x")], provider).search(RetrievalQuery(raw_issue="issue"))


def test_identity_change_and_duplicate_ids_rejected():
    provider = MappingEmbedding({"x": [1, 2], "issue": [1, 2]})
    channel = SemanticChannel([document("x", "x")], provider)
    provider.identity = replace(provider.identity, revision="v2")
    with pytest.raises(RetrievalError, match="identity changed"):
        channel.search(RetrievalQuery(raw_issue="issue"))
    for docs in ([document("x", "x"), document("x", "x")], [document("", "x")]):
        with pytest.raises(RetrievalError, match="unique nonempty"):
            SemanticChannel(docs, provider)


def test_service_semantic_preserves_result_contract(tmp_path):
    (tmp_path / "a.py").write_text("def a(): pass\ndef b(): pass\n", encoding="utf-8")
    symbols = [document("a", "", start=1).symbol, document("b", "", start=2).symbol]

    class Provider(MappingEmbedding):
        def embed(self, texts):
            self.calls.append(list(texts))
            return np.array([[1, 0] for _ in texts])

    provider = Provider({})
    result = retrieve_symbols(
        tmp_path,
        symbols,
        "whole issue text",
        channel="semantic",
        embedding_provider=provider,
        limit=1,
    )
    assert RetrievalResult.model_validate_json(result.model_dump_json()) == result
    assert result.fusion_method.value == "none"
    assert result.enabled_channels == ["semantic"]
    assert len(result.symbols) == 1 and len(result.per_channel["semantic"]) == 2
    assert result.files == [] and result.channel_weights == {}
    assert provider.calls[-1] == ["whole issue text"]
    assert "path: a.py" in provider.calls[0][0]
    with pytest.raises(ValueError, match="embedding_provider"):
        retrieve_symbols(tmp_path, symbols, "issue", channel="semantic")
