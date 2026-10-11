"""V-18: only confirmed repository frames identify directly pinned symbols."""

from dataclasses import replace

import pytest

from codepilot.retrieval.fusion import (
    pin_traceback,
    rank_with_pinned,
    weighted_fusion,
    without_pinned,
)
from codepilot.schemas.common import TraceFrame
from codepilot.schemas.retrieval import RetrievalQuery
from support.retrieval import document, seed


def frame(file="a.py", line=10, function="f", in_repo=True):
    return TraceFrame(file=file, line=line, function=function, in_repo=in_repo)


def symbol(sid="f", file="a.py", start=5, end=15, kind="function"):
    return replace(document(sid, "code", path=file, start=start, kind=kind).symbol, end_line=end)


def query(*frames):
    return RetrievalQuery(raw_issue="issue", parsed_frames=list(frames))


def test_pin_is_first_zero_and_excluded_from_normalization_and_fusion():
    pinned = pin_traceback(query(frame()), [symbol()])
    channels = {"lexical": {"f": 100, "low": 1, "high": 3}}
    eligible = without_pinned(channels, pinned)
    assert eligible == {"lexical": {"low": 1, "high": 3}}
    scores = weighted_fusion(eligible)
    ranked = rank_with_pinned([seed(sid, value) for sid, value in scores.items()], pinned)
    assert [item.symbol_id for item in ranked] == ["f", "high", "low"]
    assert ranked[0].pinned and ranked[0].score == 0
    assert "a.py:10 in f" in ranked[0].reason
    assert scores == {"high": 0.2, "low": 0}
    assert channels["lexical"]["f"] == 100


def test_duplicate_frames_are_deduplicated_and_multiple_reasons_sorted():
    pinned = pin_traceback(query(frame(line=11), frame(), frame()), [symbol()])
    assert len(pinned) == 1
    assert pinned[0].reason.count("a.py:10 in f") == 1
    assert pinned[0].reason.index(":10") < pinned[0].reason.index(":11")


@pytest.mark.parametrize(
    "bad",
    [
        frame(in_repo=False),
        frame(file="site-packages/a.py"),
        frame(file="../a.py"),
        frame(function="other"),
        frame(line=4),
        frame(line=16),
    ],
)
def test_unconfirmed_external_or_unmatched_frames_never_pin(bad):
    assert pin_traceback(query(bad), [symbol()]) == []


def test_file_name_and_line_all_disambiguate_nested_names():
    outer = symbol("pkg.f", start=1, end=20)
    inner = symbol("pkg.f.f", start=8, end=12)
    other_file = symbol("other.f", file="b.py")
    pinned = pin_traceback(query(frame()), [outer, other_file, inner])
    assert [item.symbol_id for item in pinned] == ["pkg.f.f"]


def test_module_frame_maps_to_indexed_module():
    pinned = pin_traceback(query(frame(function="<module>")), [symbol("a", kind="module")])
    assert len(pinned) == 1 and pinned[0].kind == "module"


def test_multiple_pins_and_input_permutations_use_business_key():
    symbols = [symbol("z", file="z.py"), symbol("f"), symbol("early", start=1, end=3)]
    frames = [frame(file="z.py", function="z"), frame(), frame(line=2, function="early")]
    first = pin_traceback(query(*frames), symbols)
    second = pin_traceback(query(*reversed(frames)), list(reversed(symbols)))
    assert first == second
    assert [item.symbol_id for item in first] == ["early", "f", "z"]
    ordinary = [seed("later", 0.5, path="b.py"), seed("earlier", 0.5), seed("f", 99)]
    ranked = rank_with_pinned(ordinary, first)
    assert [item.symbol_id for item in ranked] == ["early", "f", "z", "earlier", "later"]
    assert rank_with_pinned(list(reversed(ordinary)), list(reversed(first))) == ranked
    assert ordinary[2].score == 99 and not ordinary[2].pinned


def test_empty_and_no_frame_cases():
    assert pin_traceback(query(), [symbol()]) == []
    assert pin_traceback(query(frame()), []) == []
    assert rank_with_pinned([], []) == []


def test_duplicate_candidates_only_count_once_and_pin_overrides_raw_score():
    pinned = pin_traceback(query(frame()), [symbol()])
    ranked = rank_with_pinned([seed("f", 99), seed("a", 1), seed("a", 2)], pinned * 2)
    assert [(item.symbol_id, item.score) for item in ranked] == [("f", 0), ("a", 2)]
