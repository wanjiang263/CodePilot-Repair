"""V-20: complete explanations, canonical snapshots and consistency checks."""

import json
import math
from dataclasses import replace

import pytest

from codepilot.retrieval.result import build_result, validate_result
from codepilot.schemas.common import TraceFrame
from codepilot.schemas.retrieval import ChannelScore, FusionMethod, RetrievalQuery, RetrievalResult
from support.retrieval import document, seed


def query(*frames):
    return RetrievalQuery(raw_issue="broken", parsed_frames=list(frames))


def channels():
    return {"bm25": [seed("a", 100), seed("b", 200)], "semantic": [seed("a", 0.9), seed("b", 0.1)]}


def test_weighted_result_keeps_all_raw_normalized_and_final_scores():
    inputs = channels()
    result = build_result(query(), inputs)
    assert result.channel_weights == {"bm25": 0.2, "semantic": 0.3}
    assert [(s.symbol_id, s.score) for s in result.symbols] == [("a", 0.3), ("b", 0.2)]
    assert result.per_channel["bm25"][0].score == 200
    assert result.symbols[0].channel_scores == [
        ChannelScore(channel="bm25", raw=100, normalized=0),
        ChannelScore(channel="semantic", raw=0.9, normalized=1),
    ]
    assert "weight=0.3" in result.symbols[0].reason
    assert inputs["bm25"][0].score == 100 and inputs["bm25"][0].channel_scores == []
    validate_result(result)


def test_lexical_default_budget_splits_across_enabled_channels():
    result = build_result(
        query(), {"bm25": [seed("a", 1)], "ripgrep": [seed("a", 5)], "semantic": []}
    )
    assert result.channel_weights == {"bm25": 0.1, "ripgrep": 0.1, "semantic": 0.3}
    assert result.symbols[0].score == pytest.approx(0.2)
    assert [s.channel for s in result.symbols[0].channel_scores] == ["bm25", "ripgrep", "semantic"]
    assert (
        result.symbols[0].channel_scores[-1].raw
        == result.symbols[0].channel_scores[-1].normalized
        == 0
    )
    assert "semantic missing" in result.symbols[0].reason


def test_explicit_weights_and_absent_weights_are_respected():
    result = build_result(query(), channels(), weights={"bm25": 0.8})
    assert result.channel_weights == {"bm25": 0.8, "semantic": 0}
    assert [(s.symbol_id, s.score) for s in result.symbols] == [("b", 0.8), ("a", 0)]
    group = build_result(
        query(), {"bm25": [seed("a", 1)], "ripgrep": [seed("a", 2)]}, weights={"lexical": 0.6}
    )
    assert group.symbols[0].score == pytest.approx(0.6)
    with pytest.raises(ValueError, match="lexical"):
        build_result(query(), channels(), weights={"lexical": 0.2, "bm25": 0.1})


def test_rrf_result_has_ranks_normalized_scores_and_no_weights():
    result = build_result(query(), channels(), fusion_method=FusionMethod.RRF)
    assert result.channel_weights == {}
    assert [s.symbol_id for s in result.symbols] == ["a", "b"]
    assert [s.score for s in result.symbols] == pytest.approx([1 / 61 + 1 / 62] * 2)
    assert "bm25 raw=100 normalized=0 rank=2" in result.symbols[0].reason
    validate_result(result)


def test_pin_without_any_channel_hit_remains_visible_and_verified():
    sym = replace(document("f", "", start=1).symbol, end_line=3)
    frame = TraceFrame(file="a.py", line=2, function="f", in_repo=True)
    result = build_result(query(frame), {"bm25": []}, repository_symbols=[sym])
    assert result.symbols[0].pinned and result.symbols[0].score == 0
    assert result.symbols[0].channel_scores[0].raw == 0
    assert result.files[0].pinned and result.files[0].score == 0
    validate_result(result)
    assert build_result(query(frame), {"bm25": []}).symbols == []


@pytest.mark.parametrize("method", [FusionMethod.WEIGHTED, FusionMethod.RRF])
def test_pins_do_not_enter_normalization_or_rrf_rank(method):
    sym = document("pin", "", start=1).symbol
    frame = TraceFrame(file="a.py", line=1, function="pin", in_repo=True)
    result = build_result(
        query(frame),
        {"bm25": [seed("pin", 100), seed("low", 1), seed("high", 3)]},
        repository_symbols=[sym],
        fusion_method=method,
    )
    assert result.symbols[0].pinned and result.symbols[0].score == 0
    assert result.symbols[0].channel_scores[0].raw == 100
    assert [s.symbol_id for s in result.symbols] == ["pin", "high", "low"]
    if method == FusionMethod.WEIGHTED:
        assert [s.score for s in result.symbols] == [0, 0.2, 0]
        assert result.symbols[1].channel_scores[0].normalized == 1
        assert result.per_channel["bm25"][1].channel_scores[0].normalized == pytest.approx(2 / 99)
    else:
        assert result.symbols[1].score == pytest.approx(1 / 61)
    validate_result(result)


def test_top_k_does_not_lose_full_scores_or_change_file_aggregation():
    raw = {"bm25": [seed(str(i), score) for i, score in enumerate([1, 2, 3, 4, 5])]}
    full = build_result(query(), raw, limit=20)
    small = build_result(query(), raw, limit=1)
    assert small.symbols == full.symbols[:1]
    assert small.fused_symbols == full.fused_symbols
    assert small.files == full.files
    assert len(small.per_channel["bm25"]) == 5 and len(small.fused_symbols) == 5
    assert small.files[0].score == pytest.approx(0.2 + 0.1 * (0.15 + 0.1 + 0.05))
    validate_result(small)


def test_duplicate_raw_hits_preserved_but_fused_once():
    result = build_result(query(), {"bm25": [seed("a", 2), seed("a", 1), seed("b", 3)]})
    assert len(result.per_channel["bm25"]) == 3
    assert len(result.fused_symbols) == 2
    assert result.files[0].top_symbols == ["b", "a"]
    validate_result(result)


def test_business_key_overrides_id_order_for_equal_fusion_scores():
    result = build_result(query(), {"bm25": [seed("a", 1, path="z.py"), seed("z", 1, path="a.py")]})
    assert [s.symbol_id for s in result.symbols] == ["z", "a"]


def test_roundtrip_and_input_order_perturbation_are_byte_identical():
    inputs = channels()
    result = build_result(query(), inputs, snapshot_id="caller-owned-reference")
    shuffled = {name: list(reversed(hits)) for name, hits in reversed(list(inputs.items()))}
    second = build_result(query(), shuffled, snapshot_id="caller-owned-reference")
    assert result.model_dump_json().encode() == second.model_dump_json().encode()
    restored = RetrievalResult.model_validate_json(result.model_dump_json())
    assert restored == result
    validate_result(restored)
    assert json.loads(restored.model_dump_json())["snapshot_id"] == "caller-owned-reference"


def test_legacy_schema_roundtrip_still_accepts_missing_additive_fields():
    old = RetrievalResult.model_validate(
        {
            "query": {"raw_issue": "issue"},
            "symbols": [seed("a", 1).model_dump()],
            "fusion_method": "none",
        }
    )
    assert old.fused_symbols == []
    assert RetrievalResult.model_validate_json(old.model_dump_json()) == old
    validate_result(old)


@pytest.mark.parametrize(
    "change", ["score", "rank", "files", "per_channel", "normalized", "enabled", "pin"]
)
def test_consistency_validation_detects_tampered_snapshot(change):
    result = build_result(query(), channels())
    if change == "score":
        result.fused_symbols[0].score += 1
    elif change == "rank":
        result.fused_symbols.reverse()
    elif change == "files":
        result.files[0].score += 1
    elif change == "per_channel":
        result.per_channel["bm25"][0].channel_scores[0].raw = 999
    elif change == "normalized":
        result.symbols[0].channel_scores[0].normalized = 0.5
    elif change == "enabled":
        result.enabled_channels.reverse()
    else:
        result.fused_symbols[0].pinned = True
        result.fused_symbols[0].score = 0
    with pytest.raises(ValueError, match="inconsistent"):
        validate_result(result)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_raw_candidate_is_rejected(bad):
    with pytest.raises(ValueError):
        build_result(query(), {"bm25": [seed("a", 1).model_copy(update={"score": bad})]})


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_limit_is_rejected(limit):
    with pytest.raises(ValueError, match="limit"):
        build_result(query(), channels(), limit=limit)


def test_empty_candidates_and_invalid_options():
    result = build_result(query(), {"bm25": [], "semantic": []})
    assert result.symbols == result.fused_symbols == result.files == []
    assert result.snapshot_id == ""
    validate_result(result)
    with pytest.raises(ValueError, match="channel"):
        build_result(query(), {"unknown": []})
    with pytest.raises(ValueError, match="fusion"):
        build_result(query(), {}, fusion_method="none")
    with pytest.raises(ValueError, match="weights"):
        build_result(query(), {}, fusion_method=FusionMethod.RRF, weights={})


def test_conflicting_identity_and_unconfirmed_pin_flag():
    with pytest.raises(ValueError, match="identity"):
        build_result(query(), {"bm25": [seed("a", 1)], "semantic": [seed("a", 2, path="b.py")]})
    result = build_result(query(), {"bm25": [seed("a", 0).model_copy(update={"pinned": True})]})
    assert not result.symbols[0].pinned


def test_nondefault_rrf_k_survives_standalone_roundtrip_and_validation():
    result = build_result(query(), channels(), fusion_method=FusionMethod.RRF, k=3)
    assert result.rrf_k == 3
    assert result.symbols[0].score == pytest.approx(1 / 4 + 1 / 5)
    restored = RetrievalResult.model_validate_json(result.model_dump_json())
    validate_result(restored)
