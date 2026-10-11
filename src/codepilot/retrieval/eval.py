"""Task-level retrieval metrics (02 §17.2), never imported by inference code.

The denominator includes every supplied task, including empty gold labels and
missing predictions. A function target is (repository-relative file, qname);
module-level changes use the module symbol's qname.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

Function = tuple[str, str]


@dataclass(frozen=True)
class GoldLocations:
    files: tuple[str, ...] = ()
    functions: tuple[Function, ...] = ()


@dataclass(frozen=True)
class Prediction:
    files: tuple[str, ...] = ()
    functions: tuple[Function, ...] = ()


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_tasks: int
    missing_predictions: list[str]
    file_hits: dict[int, int]
    function_hits: dict[int, int]
    exact_hits: dict[int, int]
    file_recall: dict[int, float]
    function_recall: dict[int, float]
    localization_exact: dict[int, float]


def _top[T](items: tuple[T, ...], k: int) -> set[T]:
    return set(list(dict.fromkeys(items))[:k])


def _hits(
    gold: Mapping[str, GoldLocations],
    predictions: Mapping[str, Prediction],
    k: int,
    metric: Literal["file", "function", "exact"],
) -> int:
    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive integer")
    hits = 0
    for task_id in sorted(gold):
        target = gold[task_id]
        pred = predictions.get(task_id, Prediction())
        if metric == "function":
            matched = bool(set(target.functions) & _top(pred.functions, k))
        elif metric == "exact":
            matched = bool(target.files) and set(target.files) <= _top(pred.files, k)
        else:
            matched = bool(set(target.files) & _top(pred.files, k))
        hits += int(matched)
    return hits


def file_recall(
    gold: Mapping[str, GoldLocations], predictions: Mapping[str, Prediction], k: int
) -> float:
    """At least one changed file occurs in the first K distinct predicted files."""
    hits = _hits(gold, predictions, k, "file")
    return hits / len(gold) if gold else 0.0


def function_recall(
    gold: Mapping[str, GoldLocations], predictions: Mapping[str, Prediction], k: int
) -> float:
    hits = _hits(gold, predictions, k, "function")
    return hits / len(gold) if gold else 0.0


def localization_exact(
    gold: Mapping[str, GoldLocations], predictions: Mapping[str, Prediction], k: int
) -> float:
    """All changed files occur in Top-K; an empty label is not a vacuous hit."""
    hits = _hits(gold, predictions, k, "exact")
    return hits / len(gold) if gold else 0.0


def evaluate(
    gold: Mapping[str, GoldLocations], predictions: Mapping[str, Prediction]
) -> EvaluationReport:
    """Preserve raw integer statistics alongside the 1/5/10 ratios."""
    counts = {
        metric: {k: _hits(gold, predictions, k, metric) for k in (1, 5, 10)}
        for metric in ("file", "function", "exact")
    }
    total = len(gold)
    ratios = {
        metric: {k: n / total if total else 0.0 for k, n in values.items()}
        for metric, values in counts.items()
    }
    return EvaluationReport(
        total_tasks=total,
        missing_predictions=sorted(set(gold) - predictions.keys()),
        file_hits=counts["file"],
        function_hits=counts["function"],
        exact_hits=counts["exact"],
        file_recall=ratios["file"],
        function_recall=ratios["function"],
        localization_exact=ratios["exact"],
    )
