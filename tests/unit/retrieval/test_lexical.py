import math
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from codepilot.repo.models import Symbol
from codepilot.repo.parser import TreeSitterParser
from codepilot.repo.symbols import extract_symbols
from codepilot.retrieval.documents import RetrievalError, SymbolDocument, build_documents
from codepilot.retrieval.lexical import BM25Channel, RipgrepChannel, rank_candidates
from codepilot.retrieval.query import build_query
from codepilot.retrieval.service import retrieve_symbols
from codepilot.schemas.retrieval import ChannelScore, RetrievalQuery, RetrievalResult

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/retrieval"


def document(sid, text, path="a.py"):
    return SymbolDocument(
        Symbol(sid, sid, "function", id=sid, path=path, start_line=1, end_line=1), text, text, False
    )


def test_bm25_matches_hand_calculated_scores_and_symbol_unit():
    # N=3, df(apple)=1, dl=3, avgdl=2, tf=2, k1=1.5, b=.75.
    docs = [
        document("apple", "apple apple pear"),
        document("plum", "plum pear"),
        document("fig", "fig"),
    ]
    candidates = BM25Channel(docs).search(RetrievalQuery(raw_issue="apple", keywords=["apple"]))
    expected = math.log(2.5 / 1.5) * (2 * 2.5 / (2 + 1.5 * (0.25 + 0.75 * 3 / 2)))
    assert len(candidates) == 1
    assert candidates[0].symbol_id == "apple"
    assert candidates[0].score == pytest.approx(expected)
    assert candidates[0].channel_scores[0].raw == pytest.approx(expected)
    assert candidates[0].channel_scores[0].normalized == 1
    # Same file, different symbols remain independent candidates.
    assert len(BM25Channel(docs).search(build_query("pear"))) == 2


def test_bm25_negative_scores_empty_and_no_hit():
    docs = [document("one", "common common"), document("two", "common")]
    result = BM25Channel(docs).search(build_query("common"))
    assert len(result) == 2 and all(s.score < 0 for s in result)
    assert BM25Channel(docs).search(build_query("absent")) == []
    assert BM25Channel(docs).search(build_query("")) == []
    assert BM25Channel([]).search(build_query("common")) == []
    assert BM25Channel([document("empty", "")]).search(build_query("x")) == []


def test_rank_ties_and_normalization():
    docs = [document("b", "x", "b.py"), document("z", "x"), document("a", "x")]
    result = rank_candidates(docs, {"b": 2, "z": 2, "a": 0}, "bm25")
    assert [c.symbol_id for c in result] == ["z", "b", "a"]
    assert [c.channel_scores[0].normalized for c in result] == [1, 1, 0]
    with pytest.raises(ValidationError):
        ChannelScore(channel="bm25", raw=float("nan"), normalized=0)


def fixture_documents():
    source = (FIXTURE / "auth.py").read_bytes()
    symbols = extract_symbols(
        TreeSitterParser().parse(source, path="auth.py"),
        path="auth.py",
        repository_id="retrieval-fixture",
    )
    return build_documents(FIXTURE, symbols)


def test_real_ripgrep_name_and_error_bonus_and_innermost_symbol():
    docs = fixture_documents()
    result = RipgrepChannel(FIXTURE, docs).search(
        build_query('`getUserName` "invalid token [expired]"')
    )
    assert result[0].qualified_name == "auth.getUserName"
    assert result[0].score >= 8
    assert not any(s.kind == "module" for s in result)
    assert RipgrepChannel(FIXTURE, docs).search(build_query("totally_absent_symbol")) == []


def test_exact_symbol_precedes_substring_match(tmp_path):
    (tmp_path / "a.py").write_text(
        "def user_id():\n    pass\ndef other():\n    return 'user_id_suffix'\n"
    )
    symbols = extract_symbols(
        TreeSitterParser().parse((tmp_path / "a.py").read_bytes(), path="a.py"),
        path="a.py",
        repository_id="r",
    )
    result = RipgrepChannel(tmp_path, build_documents(tmp_path, symbols)).search(
        RetrievalQuery(raw_issue="user_id", identifiers=["user_id"])
    )
    assert [s.qualified_name for s in result] == ["a.user_id", "a.other"]
    assert [s.score for s in result] == [5, 1]


def test_ripgrep_missing_failure_timeout_and_empty(monkeypatch):
    docs = fixture_documents()
    monkeypatch.setattr("codepilot.retrieval.lexical.shutil.which", lambda _: None)
    channel = RipgrepChannel(FIXTURE, docs)
    assert channel.search(build_query("")) == []
    with pytest.raises(RetrievalError, match="required"):
        channel.search(build_query("user"))
    monkeypatch.setattr("codepilot.retrieval.lexical.shutil.which", lambda _: "rg")
    monkeypatch.setattr(
        "codepilot.retrieval.lexical.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 2, "", "permission denied"),
    )
    with pytest.raises(RetrievalError, match="permission denied"):
        channel.search(build_query("user"))

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("rg", 1)

    monkeypatch.setattr("codepilot.retrieval.lexical.subprocess.run", timeout)
    with pytest.raises(RetrievalError, match="ripgrep failed"):
        channel.search(build_query("user"))


def test_single_channel_result_roundtrip_and_limit():
    docs = fixture_documents()
    symbols = [doc.symbol for doc in docs]
    result = retrieve_symbols(FIXTURE, symbols, "user", limit=1)
    assert len(result.symbols) == 1
    assert len(result.per_channel["bm25"]) > 1
    assert result.enabled_channels == ["bm25"]
    assert result.fusion_method == "none"
    assert result.files == [] and result.channel_weights == {}
    assert RetrievalResult.model_validate_json(result.model_dump_json()) == result
    assert retrieve_symbols(FIXTURE, symbols, "getUserName", channel="ripgrep").symbols
    with pytest.raises(ValueError):
        retrieve_symbols(FIXTURE, symbols, "user", limit=0)


def test_bm25_ranking_on_hand_computed_positive_idf():
    docs = [
        document("twice", "apple apple pear"),
        document("once", "apple plum"),
        document("fig", "fig"),
        document("lime", "lime"),
        document("kiwi", "kiwi"),
    ]
    result = BM25Channel(docs).search(build_query("apple apple"))
    idf = math.log(3.5 / 2.5)
    avg = 8 / 5
    twice = idf * 2 * 2.5 / (2 + 1.5 * (0.25 + 0.75 * 3 / avg))
    once = idf * 2.5 / (1 + 1.5 * (0.25 + 0.75 * 2 / avg))
    assert [s.symbol_id for s in result] == ["twice", "once"]
    assert [s.score for s in result] == pytest.approx([twice, once])


def test_ripgrep_only_indexed_files_and_literal_option_like_pattern(tmp_path):
    (tmp_path / "a file.py").write_text("value = '-n [bad].*'\n", encoding="utf-8")
    (tmp_path / "unindexed.py").write_text("value = '-n [bad].*'\n", encoding="utf-8")
    docs = [document("indexed", "value", "a file.py")]
    query = RetrievalQuery(raw_issue="error", error_messages=["-n [bad].*"])
    result = RipgrepChannel(tmp_path, docs).search(query)
    assert [(s.symbol_id, s.score) for s in result] == [("indexed", 4)]


def test_ripgrep_nested_method_owns_match(tmp_path):
    code = "class C:\n    def f(self):\n        def inner():\n            return 'needle'\n        return inner()\n"
    (tmp_path / "a.py").write_text(code, encoding="utf-8")
    symbols = extract_symbols(
        TreeSitterParser().parse(code.encode(), path="a.py"), path="a.py", repository_id="r"
    )
    result = RipgrepChannel(tmp_path, build_documents(tmp_path, symbols)).search(
        build_query("needle")
    )
    assert len(result) == 1
    assert result[0].qualified_name.endswith("inner")


def test_unquoted_pascalcase_class_is_retrieved(tmp_path):
    code = "class Session:\n    pass\n"
    (tmp_path / "a.py").write_text(code, encoding="utf-8")
    symbols = extract_symbols(
        TreeSitterParser().parse(code.encode(), path="a.py"), path="a.py", repository_id="r"
    )
    query = build_query("Session is broken")
    assert "Session" in query.identifiers
    result = RipgrepChannel(tmp_path, build_documents(tmp_path, symbols)).search(query)
    assert [(candidate.qualified_name, candidate.score) for candidate in result] == [
        ("a.Session", 5)
    ]
