"""Symbol-to-file aggregation with explicit, score-independent pin priority (V-19)."""

import math
from collections.abc import Sequence

from codepilot.retrieval.fusion import finite_sum, symbol_key
from codepilot.schemas.retrieval import RankedFile, RankedSymbol


def unique_symbols(candidates: Sequence[RankedSymbol]) -> list[RankedSymbol]:
    """Collapse duplicate IDs; reject conflicting identities and invalid scores.

    Pins take precedence, otherwise keep the strongest score. A serialization
    tiebreak makes equal-score duplicate explanations independent of input order.
    """
    identities: dict[str, tuple[str, int, str, str, int | None, str | None]] = {}
    for candidate in candidates:
        if not math.isfinite(candidate.score):
            raise ValueError("symbol score must be finite")
        if candidate.pinned and candidate.score != 0:
            raise ValueError("pinned symbol score must be zero")
        identity = (*symbol_key(candidate), candidate.end_line, candidate.kind)
        previous = identities.setdefault(candidate.symbol_id, identity)
        if previous != identity:
            raise ValueError(f"conflicting symbol identity: {candidate.symbol_id}")
    ordered = sorted(
        candidates,
        key=lambda c: (not c.pinned, -c.score, *symbol_key(c), c.model_dump_json()),
    )
    by_id: dict[str, RankedSymbol] = {}
    for candidate in ordered:
        by_id.setdefault(candidate.symbol_id, candidate)
    return list(by_id.values())


def aggregate_files(candidates: Sequence[RankedSymbol]) -> list[RankedFile]:
    """max + 0.1 * up to three other highest unique scores, grouped by file.

    ``top_symbols`` contains symbol IDs: every pin plus the four symbols used in
    the formula, with pins first. All evidence remains in RetrievalResult.symbols
    and per_channel; a zero-score pin must not disappear from a file explanation.
    """
    grouped: dict[str, list[RankedSymbol]] = {}
    for candidate in unique_symbols(candidates):
        grouped.setdefault(candidate.file, []).append(candidate)
    result = []
    for path in sorted(grouped):
        symbols = grouped[path]
        top = sorted(symbols, key=lambda c: (-c.score, *symbol_key(c)))[:4]
        pins = sorted((c for c in symbols if c.pinned), key=symbol_key)
        score = finite_sum([top[0].score, *(0.1 * c.score for c in top[1:])])
        explanation = (
            f"max={top[0].score:.12g} + 0.1 * ("
            + " + ".join(f"{c.score:.12g}" for c in top[1:])
            + "); unique symbols="
            + str(len(symbols))
        )
        if pins:
            explanation += "; pinned: " + "; ".join(
                f"{c.symbol_id}: {c.reason or 'traceback pin'}" for c in pins
            )
        result.append(
            RankedFile(
                path=path,
                score=score,
                pinned=bool(pins),
                top_symbols=list(dict.fromkeys(c.symbol_id for c in [*pins, *top])),
                reason=explanation,
            )
        )
    return sorted(result, key=lambda f: (not f.pinned, -f.score, f.path))
