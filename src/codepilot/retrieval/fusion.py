"""Deterministic pure scoring functions for query-local retrieval fusion."""

import math
from collections.abc import Mapping


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
