"""V-16: hand-calculated normalized sums and validated finite weights."""

import math
import sys

import pytest

from codepilot.retrieval.fusion import DEFAULT_WEIGHTS, weighted_fusion


def test_documented_weights_and_hand_calculated_default_sum():
    assert dict(DEFAULT_WEIGHTS) == {
        "semantic": 0.30,
        "lexical": 0.20,
        "structural": 0.20,
        "test": 0.15,
        "history": 0.15,
    }
    channels = {
        "semantic": {"a": -1, "b": 0, "c": 1},
        "lexical": {"a": 2, "b": 6},
        "structural": {"a": 10},
        "test": {"c": 0},
        "history": {"b": 4},
    }
    assert weighted_fusion(channels) == pytest.approx({"a": 0.20, "b": 0.50, "c": 0.45})


def test_uses_normalized_scores_instead_of_raw_quantities():
    channels = {"bm25": {"a": 100, "b": 200}, "semantic": {"a": 0.9, "b": 0.1}}
    assert weighted_fusion(channels, {"bm25": 0.2, "semantic": 0.8}) == pytest.approx(
        {"a": 0.8, "b": 0.2}
    )


def test_missing_weights_and_missing_candidates_are_zero():
    assert weighted_fusion({"unknown": {"a": 100}, "semantic": {"b": 2}}, {}) == {"a": 0, "b": 0}
    assert weighted_fusion({"unknown": {"a": 100}, "semantic": {"b": 2}}, {"semantic": 0.3}) == {
        "a": 0,
        "b": 0.3,
    }


@pytest.mark.parametrize("channels", [{}, {"semantic": {}}, {"a": {}, "b": {}}])
def test_empty_channels(channels):
    assert weighted_fusion(channels) == {}


def test_custom_weights_are_not_renormalized_and_zero_is_preserved():
    assert weighted_fusion({"x": {"a": -2, "b": 2}}, {"x": 2.5}) == {"a": 0, "b": 2.5}


@pytest.mark.parametrize("weight", [-0.1, math.nan, math.inf, -math.inf])
def test_invalid_weight_is_rejected_even_if_channel_is_empty(weight):
    with pytest.raises(ValueError, match="weight"):
        weighted_fusion({}, {"unused": weight})


def test_overflow_in_sum_is_explicit_error():
    with pytest.raises(ValueError, match="finite"):
        weighted_fusion(
            {"x": {"a": 1}, "y": {"a": 1}}, {"x": sys.float_info.max, "y": sys.float_info.max}
        )


def test_order_perturbation_is_byte_stable_and_inputs_are_unchanged():
    channels = {"z": {"b": 3, "a": 2}, "a": {"a": 1}, "m": {"a": 1}}
    weights = {"a": 0.1, "z": 0.7, "m": 0.2}
    reversed_channels = {
        key: dict(reversed(list(value.items()))) for key, value in reversed(list(channels.items()))
    }
    assert list(weighted_fusion(channels, weights).items()) == list(
        weighted_fusion(reversed_channels, dict(reversed(list(weights.items())))).items()
    )
    assert channels["z"] == {"b": 3, "a": 2}


def test_default_weights_cannot_be_mutated():
    with pytest.raises(TypeError):
        DEFAULT_WEIGHTS["semantic"] = 9
