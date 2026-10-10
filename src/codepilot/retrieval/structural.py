"""Graph expansion from an upstream channel's seeds (V-11).

02 §8.2 / docs/05 V-11: take a channel's Top-20 symbols as seeds, walk
``CALL`` / ``INHERIT`` / ``IMPORT`` edges, and score a symbol found at depth *d*
as ``seed score * 0.5 ** d`` up to depth 2. Four decisions follow from that and
are worth stating because they change the result:

- **Seeds are not re-emitted.** The channel reports only symbols reached through
  an edge; a seed already carries its upstream score, and repeating it under a
  second channel would double-count it once fusion lands (V-16).
- **Non-positive seeds spread nothing.** A cosine of ``-0.8`` or a BM25 of ``0``
  means the upstream channel found nothing relevant; ``-0.8 * 0.5`` would rank
  *above* the seed itself, turning "not relevant" into "somewhat relevant".
- **Shortest path wins.** Breadth-first with a per-seed visited set gives each
  symbol the shallowest (hence highest) depth for that seed; across seeds the
  maximum is taken. The result is independent of iteration order.
- **Symbols outside the candidate set are still traversed.** The graph is the
  repository's, the documents are what a channel may report, so an unlabeled
  intermediate node can connect two reported candidates but never appears itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from codepilot.repo.graph import STRUCTURAL_KINDS, GraphNeighbor
from codepilot.repo.models import EdgeKind
from codepilot.retrieval.documents import RetrievalError, SymbolDocument
from codepilot.retrieval.lexical import rank_candidates
from codepilot.schemas.retrieval import RankedSymbol

DEFAULT_SEED_LIMIT = 20
DEFAULT_MAX_DEPTH = 2
DEFAULT_DECAY = 0.5


class StructureSource(Protocol):
    """The only graph capability this channel needs (``RepoGraph`` satisfies it)."""

    def adjacent(self, qname: str, kinds: Sequence[EdgeKind]) -> Sequence[GraphNeighbor]: ...


class StructuralChannel:
    name = "structural"

    def __init__(
        self,
        source: StructureSource,
        documents: Sequence[SymbolDocument],
        *,
        kinds: Sequence[EdgeKind] = STRUCTURAL_KINDS,
        seed_limit: int = DEFAULT_SEED_LIMIT,
        max_depth: int = DEFAULT_MAX_DEPTH,
        decay: float = DEFAULT_DECAY,
    ) -> None:
        self.documents = list(documents)
        ids = [doc.symbol.id for doc in self.documents]
        if any(not sid for sid in ids) or len(set(ids)) != len(ids):
            raise RetrievalError("Structural documents require unique nonempty symbol IDs")
        if not kinds:
            raise ValueError("kinds must not be empty")
        if seed_limit < 1:
            raise ValueError("seed_limit must be positive")
        if max_depth < 1:
            raise ValueError("max_depth must be positive")
        if not 0 < decay <= 1:
            raise ValueError("decay must be in (0, 1]")
        self.source = source
        self.kinds = tuple(kinds)
        self.seed_limit = seed_limit
        self.max_depth = max_depth
        self.decay = decay
        self._by_id = {doc.symbol.id: doc for doc in self.documents}

    def search(
        self, seeds: Sequence[RankedSymbol], *, limit: int | None = None
    ) -> list[RankedSymbol]:
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be positive")
        scores: dict[str, float] = {}
        for seed in seeds[: self.seed_limit]:
            if seed.score <= 0 or seed.symbol_id not in self._by_id:
                continue
            self._expand(seed.symbol_id, seed.score, scores)
        return rank_candidates(self.documents, scores, self.name)[:limit]

    def _expand(self, seed_id: str, seed_score: float, scores: dict[str, float]) -> None:
        visited = {seed_id}
        frontier = [self._by_id[seed_id].symbol.qualified_name]
        factor = 1.0
        for _ in range(self.max_depth):
            factor *= self.decay
            following: list[str] = []
            for qname in frontier:
                for neighbor in self.source.adjacent(qname, self.kinds):
                    neighbor_id = neighbor.symbol.id
                    if neighbor_id in visited:
                        continue
                    visited.add(neighbor_id)
                    if neighbor_id in self._by_id:
                        value = seed_score * factor
                        previous = scores.get(neighbor_id)
                        if previous is None or value > previous:
                            scores[neighbor_id] = value
                    following.append(neighbor.symbol.qualified_name)
            if not following:
                return
            frontier = following
