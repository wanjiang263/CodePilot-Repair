"""Shared deterministic embedding fixtures, independent of pytest entry point."""

import json
from pathlib import Path

import numpy as np

from codepilot.repo.graph import STRUCTURAL_KINDS, GraphNeighbor
from codepilot.repo.models import Symbol, confidence_to_score
from codepilot.retrieval.documents import SymbolDocument
from codepilot.retrieval.embeddings import EmbeddingIdentity
from codepilot.retrieval.semantic import SemanticChannel
from codepilot.schemas.retrieval import RankedSymbol, RetrievalQuery

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


def document(sid, text, path="a.py", start=1, qualified_name=None, kind="function"):
    return SymbolDocument(
        Symbol(
            sid,
            qualified_name or sid,
            kind,
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


def seed(sid, score, qualified_name=None, path="a.py", start=1):
    """An upstream channel's ranked candidate, used as a structural seed."""
    return RankedSymbol(
        symbol_id=sid,
        qualified_name=qualified_name or sid,
        file=path,
        score=score,
        start_line=start,
        end_line=start,
    )


class FakeGraph:
    """``RepoGraph.adjacent`` 的内存替身：边表 + 相同的方向、种类与最强边收敛规则。

    边为 ``(source_symbol, target_symbol, kind, confidence, line)``。通道单测用它
    而不是真图，才能把「深度衰减」「种类过滤」「TEST 反推」这些行为与索引层解耦。
    """

    def __init__(self, edges=()):
        self.edges = list(edges)
        self.queries = []

    def adjacent(self, qname, kinds=STRUCTURAL_KINDS, *, direction="both"):
        self.queries.append(qname)
        wanted = set(kinds)
        found = {}
        for source, target, kind, confidence, line in self.edges:
            if kind not in wanted:
                continue
            if direction in ("out", "both") and source.qualified_name == qname:
                other = target
            elif direction in ("in", "both") and target.qualified_name == qname:
                other = source
            else:
                continue
            candidate = GraphNeighbor(other, kind, confidence, line)
            previous = found.get(other.id)
            if previous is None or (confidence_to_score(confidence), -line) > (
                confidence_to_score(previous.confidence),
                -previous.line,
            ):
                found[other.id] = candidate
        return sorted(
            found.values(),
            key=lambda n: (
                n.symbol.qualified_name,
                -confidence_to_score(n.confidence),
                n.line,
                n.kind,
                n.symbol.id,
            ),
        )
