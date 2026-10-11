"""Deterministic pure scoring functions for query-local retrieval fusion."""

import math
from collections.abc import Mapping
from types import MappingProxyType

DEFAULT_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {"semantic": 0.30, "lexical": 0.20, "structural": 0.20, "test": 0.15, "history": 0.15}
)


def min_max(scores: Mapping[str, float]) -> dict[str, float]:
    """Normalize to [0, 1]; equal scores, including zero, all map to 1.

    Scale only when subtraction overflows so adjacent floats and subnormals keep
    their precision. Nonfinite evidence is an error, never a fabricated score.
    Keys are canonical regardless of the input mapping's insertion order.
    """
    if any(not math.isfinite(value) for value in scores.values()):
        raise ValueError("scores must be finite")
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi == lo:
        return dict.fromkeys(sorted(scores), 1.0)
    span = hi - lo
    if math.isfinite(span):
        return {sid: (scores[sid] - lo) / span for sid in sorted(scores)}
    scale = max(abs(lo), abs(hi))
    scaled_lo, scaled_hi = lo / scale, hi / scale
    return {
        sid: (scores[sid] / scale - scaled_lo) / (scaled_hi - scaled_lo) for sid in sorted(scores)
    }


def finite_sum(values: list[float]) -> float:
    """Accumulate finite contributions or report an unrepresentable result."""
    try:
        result = math.fsum(values)
    except OverflowError as exc:
        raise ValueError("score sum must be finite") from exc
    if not math.isfinite(result):
        raise ValueError("score sum must be finite")
    return result


def weighted_fusion(
    channels: Mapping[str, Mapping[str, float]],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> dict[str, float]:
    """Sum query-local normalized evidence, with missing weights/evidence as 0.

    Weights need not sum to one: explicit caller weights retain their meaning.
    The ``lexical`` group is assembled by the result layer from independently
    normalized BM25/Ripgrep evidence; no raw quantities are added here.
    """
    if any(not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ValueError("weights must be finite and nonnegative")
    normalized = {name: min_max(channels[name]) for name in sorted(channels)}
    ids = sorted({sid for scores in normalized.values() for sid in scores})
    return {
        sid: finite_sum(
            [scores.get(sid, 0.0) * weights.get(name, 0.0) for name, scores in normalized.items()]
        )
        for sid in ids
    }
