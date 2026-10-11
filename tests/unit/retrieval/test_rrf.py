"""V-17: rank-based fusion, deduplication and deterministic ties."""

import math

import pytest

from codepilot.retrieval.fusion import rrf


def test_hand_calculated_default_k():
    result = rrf({"lexical": ["a", "b"], "semantic": ["b", "c"]})
    assert result == pytest.approx({"a": 1 / 61, "b": 1 / 62 + 1 / 61, "c": 1 / 62})
    assert list(result) == ["b", "a", "c"]


def test_rank_and_not_raw_score_controls_fusion():
    assert rrf({"x": ["first", "second"]}, k=0) == {"first": 1, "second": 0.5}


def test_duplicate_hits_only_use_first_occurrence_and_compact_rank():
    assert rrf({"x": ["a", "a", "b", "a", "b"]}) == rrf({"x": ["a", "b"]})
    assert rrf({"x": ["a"], "y": ["a"]}) == {"a": 2 / 61}


@pytest.mark.parametrize("rankings", [{}, {"x": []}, {"x": [], "y": []}])
def test_empty_rankings(rankings):
    assert rrf(rankings) == {}


@pytest.mark.parametrize("bad", [-1, 1.5, True, math.nan, math.inf, 10**400])
def test_invalid_k_is_explicit(bad):
    with pytest.raises(ValueError, match="k"):
        rrf({}, k=bad)


def test_channel_order_perturbation_and_business_id_tiebreak():
    rankings = {"z": ["b", "a"], "a": ["a", "b"], "empty": []}
    expected = list(rrf(rankings).items())
    assert expected[0][0] == "a" and expected[1][0] == "b"
    assert expected[0][1] == expected[1][1]
    assert expected == list(rrf(dict(reversed(list(rankings.items())))).items())
    assert rankings["z"] == ["b", "a"]
