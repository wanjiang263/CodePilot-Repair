"""V-13: the history channel scores files by log-normalized recent churn.

Monotonicity and "no history is an empty result, not an error" are the two proofs
docs/05 names; untouched files staying out of the candidate set, post-base commits
staying out of the ranking, and a stable tie order are part of the channel's contract.
"""

import math

import pytest

from codepilot.repo.git_history import FileChurn, GitHistory
from codepilot.retrieval.documents import RetrievalError
from codepilot.retrieval.history import HistoryChannel, churn_score
from codepilot.retrieval.service import retrieve_symbols
from codepilot.schemas.retrieval import RetrievalResult
from support.gitfixture import init_repo
from support.retrieval import document


def _docs():
    return [
        document("a1", "", "a.py", 1),
        document("a2", "", "a.py", 5),
        document("z1", "", "z.py", 1),
    ]


def _churn(**counts):
    return {path: FileChurn(path, modifications, 0) for path, modifications in counts.items()}


def test_log_normalization_is_monotone_and_compresses_the_tail():
    counts = (1, 2, 4, 8, 400)
    scores = [churn_score(count) for count in counts]
    assert scores == sorted(scores)
    assert scores == [math.log1p(count) for count in counts]
    # Sublinear: doubling the count less than doubles the score, so a hot file
    # cannot outshout every other channel.
    assert all(churn_score(2 * count) < 2 * churn_score(count) for count in counts)
    assert churn_score(400) < 6
    assert churn_score(1) == pytest.approx(0.6931471805599453)


@pytest.mark.parametrize("count", [0, -1])
def test_churn_score_domain_is_at_least_one_modification(count):
    with pytest.raises(ValueError):
        churn_score(count)


def test_scores_follow_file_churn_and_repeat_symbols_share_them():
    channel = HistoryChannel(_docs(), _churn(**{"a.py": 3, "z.py": 1}))
    found = {c.symbol_id: c.score for c in channel.search()}
    assert found == {
        "a1": math.log1p(3),
        "a2": math.log1p(3),
        "z1": math.log1p(1),
    }
    assert [c.symbol_id for c in channel.search()] == ["a1", "a2", "z1"]
    assert [c.channel_scores[0].channel for c in channel.search()] == ["history"] * 3


def test_untouched_files_are_absent_not_zero_candidates():
    channel = HistoryChannel(_docs(), _churn(**{"a.py": 2}))
    found = channel.search()
    assert [c.symbol_id for c in found] == ["a1", "a2"]
    assert [c.channel_scores[0].normalized for c in found] == [1.0, 1.0]


def test_no_history_returns_empty_not_an_error():
    docs = _docs()
    assert HistoryChannel(docs, {}).search() == []
    # A churn window whose paths do not intersect the candidates is also an empty result.
    assert HistoryChannel(docs, _churn(**{"deleted.py": 9, "renamed.py": 4})).search() == []
    assert HistoryChannel([], _churn(**{"a.py": 3})).search() == []


def test_zero_modification_entry_is_no_evidence():
    assert HistoryChannel(_docs(), _churn(**{"a.py": 0, "z.py": 1})).search()[0].symbol_id == "z1"


def test_churn_key_order_and_document_order_do_not_change_the_result():
    docs = _docs()
    churn = _churn(**{"a.py": 5, "z.py": 1})
    forward = HistoryChannel(docs, churn).search()
    backward = HistoryChannel(list(reversed(docs)), dict(reversed(list(churn.items())))).search()
    assert forward == backward
    assert [c.symbol_id for c in forward] == ["a1", "a2", "z1"]


def test_top_k_slices_after_normalizing_the_full_candidate_set():
    channel = HistoryChannel(_docs(), _churn(**{"a.py": 7, "z.py": 1}))
    full = channel.search()
    assert [c.channel_scores[0].normalized for c in full] == pytest.approx([1.0, 1.0, 0.0])
    head = channel.search(limit=2)
    assert head == full[:2]


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_limit(limit):
    with pytest.raises(ValueError):
        HistoryChannel(_docs(), _churn(**{"a.py": 1})).search(limit=limit)


def test_duplicate_or_missing_ids_rejected():
    docs = _docs()
    for bad in ([*docs, document("a1", "dup", "other.py")], [*docs, document("", "x", "e.py")]):
        with pytest.raises(RetrievalError, match="unique nonempty"):
            HistoryChannel(bad, _churn(**{"a.py": 1}))


def test_post_base_commits_cannot_reach_the_ranking(tmp_path):
    """Real git: files created or edited after the base never reach the channel (03 §11.2)."""
    repo = init_repo(tmp_path / "r", {"a.py": "def one():\n    return 1\n", "z.py": "Z = 1\n"})
    repo.write("a.py", "def one():\n    return 2\n")
    base = repo.commit("touch a")
    repo.write("hot.py", "def boom():\n    return 1\n")
    repo.commit("post-base new file")
    repo.write("a.py", "def one():\n    return 3\n")
    repo.commit("post-base edit")

    churn = GitHistory(repo.path, base).file_churn()
    assert "hot.py" not in churn
    docs = [
        document("one", "", "a.py", 1),
        document("z", "", "z.py", 1),
        document("boom", "", "hot.py", 1),
    ]
    found = HistoryChannel(docs, churn).search()
    assert [c.symbol_id for c in found] == ["one", "z"]
    assert found[0].score == math.log1p(2)  # creation commit + the base commit itself
    assert found[1].score == math.log1p(1)


def test_service_history_channel_contract(tmp_path):
    (tmp_path / "a.py").write_text("def alpha(): pass\ndef beta(): pass\n", encoding="utf-8")
    symbols = [document("alpha", "", "a.py", 1).symbol, document("beta", "", "a.py", 2).symbol]
    churn = _churn(**{"a.py": 3})

    result = retrieve_symbols(
        tmp_path,
        symbols,
        "anything: the issue text is not an input here",
        channel="history",
        churn=churn,
    )
    assert [c.symbol_id for c in result.symbols] == ["alpha", "beta"]
    assert result.symbols[0].score == math.log1p(3)
    assert result.enabled_channels == ["history"]
    assert result.fusion_method.value == "none"
    assert list(result.per_channel) == ["history"]
    assert result.files == [] and result.channel_weights == {}
    assert RetrievalResult.model_validate_json(result.model_dump_json()) == result

    empty = retrieve_symbols(tmp_path, symbols, "issue", channel="history", churn={})
    assert empty.symbols == [] and empty.per_channel["history"] == []
    with pytest.raises(ValueError, match="churn"):
        retrieve_symbols(tmp_path, symbols, "issue", channel="history")
