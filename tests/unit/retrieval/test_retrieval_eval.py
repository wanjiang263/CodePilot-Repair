"""V-21: task-level metrics, including missing predictions in the denominator."""

import pytest

from codepilot.retrieval.eval import (
    GoldLocations,
    Prediction,
    evaluate,
    file_recall,
    function_recall,
    localization_exact,
)


@pytest.fixture
def sample():
    gold = {
        "one": GoldLocations(files=("a.py", "b.py"), functions=(("a.py", "a.f"),)),
        "two": GoldLocations(files=("c.py",), functions=(("c.py", "c.f"),)),
        "failed": GoldLocations(files=("d.py",), functions=(("d.py", "d.f"),)),
        "empty": GoldLocations(),
    }
    predictions = {
        "one": Prediction(files=("a.py", "a.py", "b.py"), functions=(("a.py", "a.f"),)),
        "two": Prediction(files=("x.py", "c.py"), functions=(("x.py", "c.f"), ("c.py", "c.f"))),
        "unknown": Prediction(files=("d.py",)),
    }
    return gold, predictions


def test_hand_computed_task_metrics(sample):
    gold, predictions = sample
    # Four tasks, not three nonempty labels or two available predictions.
    assert file_recall(gold, predictions, 1) == 1 / 4
    assert file_recall(gold, predictions, 5) == 2 / 4
    assert function_recall(gold, predictions, 1) == 1 / 4
    assert function_recall(gold, predictions, 5) == 2 / 4
    assert localization_exact(gold, predictions, 1) == 0
    assert localization_exact(gold, predictions, 2) == 2 / 4


def test_report_preserves_integer_counts_and_stable_json(sample):
    gold, predictions = sample
    result = evaluate(gold, predictions)
    assert result.total_tasks == 4
    assert result.missing_predictions == ["empty", "failed"]
    assert result.file_hits == {1: 1, 5: 2, 10: 2}
    assert result.function_hits == {1: 1, 5: 2, 10: 2}
    assert result.exact_hits == {1: 0, 5: 2, 10: 2}
    assert result.file_recall == {1: 0.25, 5: 0.5, 10: 0.5}
    assert (
        result.model_dump_json()
        == evaluate(dict(reversed(list(gold.items()))), predictions).model_dump_json()
    )
    assert type(result).model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("metric", [file_recall, function_recall, localization_exact])
def test_empty_tasks_and_missing_predictions(metric):
    assert metric({}, {}, 1) == 0.0
    assert metric({"empty": GoldLocations()}, {}, 1) == 0.0


@pytest.mark.parametrize("k", [0, -1, True, 1.5])
def test_invalid_k_is_explicit(k):
    with pytest.raises(ValueError, match="positive integer"):
        file_recall({}, {}, k)


def test_duplicate_predictions_do_not_consume_top_k_and_module_is_a_function_target():
    gold = {"module": GoldLocations(files=("a.py",), functions=(("a.py", "a"),))}
    pred = {"module": Prediction(files=("x.py", "x.py", "a.py"), functions=(("a.py", "a"),))}
    assert file_recall(gold, pred, 2) == 1
    assert function_recall(gold, pred, 1) == 1


def test_metrics_do_not_mutate_inputs(sample):
    gold, predictions = sample
    before = (dict(gold), dict(predictions))
    evaluate(gold, predictions)
    assert (gold, predictions) == before
