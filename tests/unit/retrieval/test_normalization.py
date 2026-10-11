"""V-15: specified min-max behavior, including IEEE-754 boundaries."""

import math
import sys

import pytest

from codepilot.retrieval.fusion import min_max


@pytest.mark.parametrize(
    "scores, expected",
    [
        ({}, {}),
        ({"a": 2, "b": 4, "c": 6}, {"a": 0, "b": 0.5, "c": 1}),
        ({"a": -6, "b": -4, "c": -2}, {"a": 0, "b": 0.5, "c": 1}),
        ({"a": -2, "b": 0, "c": 2}, {"a": 0, "b": 0.5, "c": 1}),
        ({"a": 0, "b": 0}, {"a": 1, "b": 1}),
        ({"a": -7, "b": -7}, {"a": 1, "b": 1}),
        ({"only": 9}, {"only": 1}),
        ({"a": -sys.float_info.max, "b": 0, "c": sys.float_info.max}, {"a": 0, "b": 0.5, "c": 1}),
        ({"a": 0, "b": math.ulp(0.0), "c": 2 * math.ulp(0.0)}, {"a": 0, "b": 0.5, "c": 1}),
        ({"a": 1, "b": math.nextafter(1, 2)}, {"a": 0, "b": 1}),
        ({"a": -0.0, "b": 0.0}, {"a": 1, "b": 1}),
    ],
)
def test_min_max_exact_values_and_no_mutation(scores, expected):
    original = dict(scores)
    assert min_max(scores) == expected
    assert scores == original
    assert all(math.isfinite(value) for value in min_max(scores).values())


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_input_is_rejected(bad):
    with pytest.raises(ValueError, match="finite"):
        min_max({"bad": bad})


def test_order_is_canonical_for_reversed_input():
    scores = {"z": 3.0, "a": -5.0, "m": 0.0}
    assert list(min_max(scores)) == ["a", "m", "z"]
    assert list(min_max(scores).items()) == list(
        min_max(dict(reversed(list(scores.items())))).items()
    )
