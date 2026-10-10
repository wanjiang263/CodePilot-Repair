"""File churn from the base commit's ancestors as a retrieval signal (V-13).

docs/05 V-13 fixes the shape of this channel: the raw score is a log
normalization of how often a file was recently modified. Three consequences are
worth writing down, because each one is a choice a later reader would otherwise
have to reverse-engineer:

- **``log1p``, not a tuned constant.** ``math.log1p(count)`` is monotone and
  compresses the long tail, so a file touched 400 times does not drown out every
  other channel once scores are combined. Churn of 1 gives ``0.693…``, 2 gives
  ``1.098…``, 0 is not a candidate at all.
- **Untouched files are absent, not zero.** Emitting ``0`` for every file outside
  the churn window would put the whole repository into the candidate set and pin
  the min-max floor to a value that means "no evidence".
- **No query, by design.** 02 §8.2 gives this channel no input at all: churn does
  not depend on the issue text. Fusion (V-16) consumes per-channel candidate
  lists, so the channels do not have to share a call signature.

History isolation is *not* re-implemented here. ``GitHistory`` (R-13) walks only
ancestors of a full-SHA base commit, renames are recorded on the new path only,
and this channel consumes whatever mapping it is handed — so the isolation
guarantee is inherited, and the test below checks that a post-base commit does
not reach the ranking through it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from codepilot.repo.git_history import FileChurn
from codepilot.retrieval.documents import RetrievalError, SymbolDocument
from codepilot.retrieval.lexical import rank_candidates
from codepilot.schemas.retrieval import RankedSymbol


def churn_score(modifications: int) -> float:
    """Monotone log normalization of a file's modification count (02 §8.2 history).

    Domain is "at least one modification"; the channel only calls it for files
    the churn window actually touched.
    """
    if modifications < 1:
        raise ValueError(f"modifications must be at least 1, got {modifications}")
    return math.log1p(modifications)


class HistoryChannel:
    name = "history"

    def __init__(self, documents: Sequence[SymbolDocument], churn: Mapping[str, FileChurn]) -> None:
        self.documents = list(documents)
        ids = [doc.symbol.id for doc in self.documents]
        if any(not sid for sid in ids) or len(set(ids)) != len(ids):
            raise RetrievalError("History documents require unique nonempty symbol IDs")
        self.churn = dict(churn)

    def search(self, *, limit: int | None = None) -> list[RankedSymbol]:
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be positive")
        scores: dict[str, float] = {}
        for document in self.documents:
            entry = self.churn.get(document.symbol.path)
            if entry is None or entry.modifications <= 0:
                continue
            scores[document.symbol.id] = churn_score(entry.modifications)
        return rank_candidates(self.documents, scores, self.name)[:limit]
