"""Shared deterministic embedding fixtures, independent of pytest entry point."""

import json
from pathlib import Path

import numpy as np

from codepilot.repo.models import Symbol
from codepilot.retrieval.documents import SymbolDocument
from codepilot.retrieval.embeddings import EmbeddingIdentity
from codepilot.retrieval.semantic import SemanticChannel
from codepilot.schemas.retrieval import RetrievalQuery

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/retrieval/semantic-vectors.json"


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
