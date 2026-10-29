"""Explainable execution-duration evidence; estimates never imply test coverage."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any


def timing_identity(
    repository: str,
    base: str,
    environment: dict[str, Any],
    spec: dict[str, Any],
    selectors: list[str],
) -> str:
    config = {k: v for k, v in spec.items() if k != "timeout_seconds"}
    payload = [
        repository,
        base,
        environment.get("image_id"),
        environment.get("cache_key"),
        environment.get("runner_identity"),
        config,
        selectors,
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def estimate_timeout(identity: str, facts: list[dict[str, Any]], default: int) -> dict[str, Any]:
    usable = []
    rejected = 0
    for fact in facts:
        if fact.get("identity") != identity:
            continue
        value = fact.get("seconds")
        try:
            seconds = (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else float("nan")
            )
        except (OverflowError, ValueError):
            seconds = float("nan")
        if not math.isfinite(seconds) or seconds <= 0:
            rejected += 1
            continue
        usable.append({**fact, "seconds": seconds})
    complete = [
        float(f["seconds"])
        for f in usable
        if f.get("complete") is True and f.get("status") in {"pass", "fail"}
    ]
    lower = [float(f["seconds"]) for f in usable if f.get("status") == "timeout"]
    aggregate = [
        float(f["seconds"])
        for f in usable
        if f.get("process_completed") is True and not f.get("complete")
    ]
    if complete:
        # Median resists outliers, but never erase a known necessary duration.
        suggested = max(max(complete), statistics.median(complete) * 1.25 + 5)
        source, confidence = "exact_ordered_scope", "medium" if len(complete) >= 3 else "low"
    else:
        suggested, source, confidence = float(default), "conservative_default", "none"
    if not complete and aggregate:
        # Aggregate evidence can raise a recommendation, never justify shortening
        # a range whose collection completeness was not established.
        suggested = max(suggested, max(aggregate), statistics.median(aggregate) * 1.25 + 5)
        source, confidence = "exact_scope_aggregate", "low"
    if lower:
        suggested = max(suggested, max(lower) * 1.25 + 5)
        source += "+timeout_lower_bound"
    if not math.isfinite(suggested):
        suggested = max(float(default + 1), 1e9)
        source += "+overflow_saturated_above_cap"
    return {
        "identity": identity,
        "rejected_samples": rejected,
        "source": source,
        "confidence": confidence,
        "suggested_seconds": math.ceil(suggested),
        "runner_limit_seconds": default,
        "complete_samples": len(complete),
        "timeout_samples": len(lower),
        "aggregate_samples": len(aggregate),
        "observed_complete_seconds": complete,
        "timeout_lower_bounds_seconds": lower,
        # This is an observation, not a lower bound for another candidate.
        "historical_fastest_complete_seconds": min(complete) if complete else None,
        "startup_seconds": None,
        "startup_note": "included in observed total; not separately measured",
        "coverage_reduced": False,
    }
