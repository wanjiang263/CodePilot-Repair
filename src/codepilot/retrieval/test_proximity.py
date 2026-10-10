"""Symbols under test, found by walking backwards from tests named in the issue (V-12).

02 §8.2 gives this channel two inputs and one rule: test names mentioned in the
issue and test frames inside a traceback, followed through the ``TEST`` edges
that R-12 built. The rule it leaves open is *how much* each link is worth; this
module fixes it explicitly:

- Only symbols the parser labeled ``test_function`` / ``test_method`` count as
  evidence. A production symbol whose name happens to appear in the issue is the
  lexical channel's job, and re-counting it here would just duplicate that score.
- Evidence strength is the TEST edge's own confidence (``high`` 1.0 for a direct
  call in the test body, ``medium`` 0.6 for import plus name, ``low`` 0.3 for the
  filename convention). Those are R-12's tiers, not new constants.
- A symbol gains **every** matching test's weight, so code covered by two failing
  tests outranks code covered by one. Contributions are summed in symbol-ID order
  because floating-point addition is not associative, and "same issue, same
  numbers" is a Phase 2 acceptance criterion.
- Targets that are themselves tests are dropped: the channel answers "what is
  under test", and a test is never the patch candidate.
"""

from __future__ import annotations

from collections.abc import Sequence

from codepilot.repo.models import EdgeKind, Symbol, confidence_to_score
from codepilot.retrieval.documents import RetrievalError, SymbolDocument
from codepilot.retrieval.graph_source import GraphSource
from codepilot.retrieval.lexical import rank_candidates
from codepilot.schemas.retrieval import RankedSymbol, RetrievalQuery

TEST_KINDS = frozenset({"test_function", "test_method"})
TEST_EDGE_KINDS: tuple[EdgeKind, ...] = ("TEST",)


def _names_match(token: str, symbol: Symbol) -> bool:
    return (
        symbol.name == token
        or symbol.qualified_name == token
        or symbol.qualified_name.endswith(f".{token}")
    )


class TestProximityChannel:
    """The ``test`` channel (02 §8.2). pytest would otherwise try to collect this name."""

    __test__ = False

    name = "test"

    def __init__(
        self,
        source: GraphSource,
        documents: Sequence[SymbolDocument],
        *,
        kinds: Sequence[EdgeKind] = TEST_EDGE_KINDS,
    ) -> None:
        self.documents = list(documents)
        ids = [doc.symbol.id for doc in self.documents]
        if any(not sid for sid in ids) or len(set(ids)) != len(ids):
            raise RetrievalError("Test proximity documents require unique nonempty symbol IDs")
        if not kinds:
            raise ValueError("kinds must not be empty")
        self.source = source
        self.kinds = tuple(kinds)
        self._by_id = {doc.symbol.id: doc for doc in self.documents}
        self._tests = [doc.symbol for doc in self.documents if doc.symbol.kind in TEST_KINDS]

    def search(self, query: RetrievalQuery, *, limit: int | None = None) -> list[RankedSymbol]:
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be positive")
        matched = self._matched_tests(query)
        evidence: dict[str, dict[str, float]] = {}
        for test in matched:
            for neighbor in self.source.adjacent(test.qualified_name, self.kinds, direction="out"):
                target = neighbor.symbol
                if (
                    target.kind in TEST_KINDS
                    or target.id not in self._by_id
                    or target.id == test.id
                ):
                    continue
                weights = evidence.setdefault(target.id, {})
                previous = weights.get(test.id)
                weight = confidence_to_score(neighbor.confidence)
                if previous is None or weight > previous:
                    # 同一测试对同一目标有多条边时只算最强的一条。
                    weights[test.id] = weight
        scores = {
            target_id: sum(weights[key] for key in sorted(weights))
            for target_id, weights in evidence.items()
        }
        return rank_candidates(self.documents, scores, self.name)[:limit]

    def _matched_tests(self, query: RetrievalQuery) -> list[Symbol]:
        named = set(query.test_names)
        frames = [frame for frame in query.parsed_frames if frame.in_repo]
        return [
            test
            for test in self._tests
            if (named and any(_names_match(token, test) for token in named))
            or any(
                frame.file == test.path and _names_match(frame.function, test) for frame in frames
            )
        ]
