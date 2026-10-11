"""V-19: file formula, unique symbols and priority carried from traceback pins."""

import math
import sys

import pytest

from codepilot.retrieval.aggregate import aggregate_files
from codepilot.schemas.retrieval import RankedFile
from support.retrieval import seed


def test_hand_calculated_max_plus_three_other_scores():
    symbols = [seed(f"s{i}", value) for i, value in enumerate([10, 9, 8, 7, 6, 5])]
    files = aggregate_files(symbols)
    assert len(files) == 1
    assert files[0].score == pytest.approx(10 + 0.1 * (9 + 8 + 7))
    assert files[0].top_symbols == ["s0", "s1", "s2", "s3"]
    assert "max=10" in files[0].reason and "9" in files[0].reason


def test_single_symbol_is_not_counted_twice():
    assert aggregate_files([seed("one", 2)])[0].score == 2


def test_duplicate_symbols_keep_strongest_once():
    files = aggregate_files([seed("a", 10), seed("a", 10), seed("a", 5), seed("b", 4)])
    assert files[0].score == pytest.approx(10.4)
    assert files[0].top_symbols == ["a", "b"]


def test_empty_symbols():
    assert aggregate_files([]) == []


def test_file_ties_and_top_symbol_ties_use_business_key():
    symbols = [seed("z", 1, path="b.py"), seed("late", 1, start=9), seed("early", 1, start=1)]
    files = aggregate_files(symbols)
    assert [file.path for file in files] == ["a.py", "b.py"]
    assert files[0].top_symbols == ["early", "late"]
    equal = aggregate_files([seed("z", 1, path="z.py"), seed("a", 1)])
    assert [file.path for file in equal] == ["a.py", "z.py"]


def test_pin_priority_is_preserved_without_a_numeric_bonus():
    pinned = seed("pin", 0, path="p.py").model_copy(
        update={"pinned": True, "reason": "repository traceback: p.py:1 in pin"}
    )
    files = aggregate_files([seed("ordinary", 100), pinned])
    assert [(file.path, file.score, file.pinned) for file in files] == [
        ("p.py", 0, True),
        ("a.py", 100, False),
    ]
    assert files[0].top_symbols == ["pin"]
    assert "repository traceback" in files[0].reason


def test_pin_and_scored_symbols_in_same_file_keep_numeric_formula():
    pinned = seed("pin", 0).model_copy(update={"pinned": True, "reason": "repository traceback"})
    files = aggregate_files(
        [pinned, *[seed(str(i), value) for i, value in enumerate([5, 4, 3, 2, 1])]]
    )
    assert files[0].score == pytest.approx(5 + 0.1 * (4 + 3 + 2))
    # Pins remain visible even when they contribute no score to the top-four formula.
    assert files[0].top_symbols == ["pin", "0", "1", "2", "3"]


def test_input_order_is_deterministic_and_does_not_mutate():
    symbols = [seed("a", 1), seed("a", 2), seed("b", 2), seed("other", 3, path="b.py")]
    first = aggregate_files(symbols)
    assert first == aggregate_files(list(reversed(symbols)))
    assert symbols[0].score == 1
    assert RankedFile.model_validate_json(first[0].model_dump_json()) == first[0]


def test_legacy_file_payload_remains_valid():
    assert not RankedFile.model_validate({"path": "a.py", "score": 1}).pinned


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_score_is_rejected_even_when_model_copy_bypasses_validation(bad):
    with pytest.raises(ValueError, match="finite"):
        aggregate_files([seed("a", 1).model_copy(update={"score": bad})])


def test_formula_overflow_is_explicit_error():
    with pytest.raises(ValueError, match="finite"):
        aggregate_files([seed(str(i), sys.float_info.max) for i in range(4)])


def test_conflicting_duplicate_symbol_identity_is_rejected():
    with pytest.raises(ValueError, match="identity"):
        aggregate_files([seed("same", 1), seed("same", 2, path="b.py")])


def test_nonzero_pinned_score_is_not_allowed_to_inflate_file_score():
    with pytest.raises(ValueError, match=r"pinned.*zero"):
        aggregate_files([seed("a", 10).model_copy(update={"pinned": True})])
