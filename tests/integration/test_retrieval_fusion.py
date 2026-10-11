"""V-20 wiring: all six real channels, an in-memory graph and deterministic vectors."""

from dataclasses import replace

import pytest

from codepilot.repo.git_history import FileChurn
from codepilot.retrieval.config import RetrievalConfig
from codepilot.retrieval.result import validate_result
from codepilot.retrieval.service import retrieve_symbols
from codepilot.schemas.retrieval import FusionMethod, RetrievalResult
from support.retrieval import FakeEmbedding, FakeGraph, document


@pytest.fixture
def repository(tmp_path):
    (tmp_path / "a.py").write_text(
        "def one():\n    return two()\ndef two():\n    return 2\n", encoding="utf-8"
    )
    (tmp_path / "test_a.py").write_text(
        "def test_one():\n    assert one() == 2\n", encoding="utf-8"
    )
    # Keep issue terms below half the corpus so BM25 has positive IDF seeds.
    # V-11 intentionally refuses to expand nonpositive seeds.
    (tmp_path / "unused.py").write_text(
        "def alpha(): pass\ndef beta(): pass\ndef gamma(): pass\ndef delta(): pass\n",
        encoding="utf-8",
    )
    distractors = [
        document(f"unused.{name}", "", path="unused.py", start=i).symbol
        for i, name in enumerate(("alpha", "beta", "gamma", "delta"), start=1)
    ]
    a = replace(document("a.one", "", start=1).symbol, name="one", end_line=2)
    b = replace(document("a.two", "", start=3).symbol, name="two", end_line=4)
    t = replace(
        document("test_a.test_one", "", path="test_a.py", start=1, kind="test_function").symbol,
        name="test_one",
        end_line=2,
    )
    graph = FakeGraph([(a, b, "CALL", "high", 2), (t, a, "TEST", "high", 2)])
    churn = {"a.py": FileChurn("a.py", 2, 0), "test_a.py": FileChurn("test_a.py", 1, 1)}
    return tmp_path, [a, b, t, *distractors], graph, churn


@pytest.mark.parametrize("method", [FusionMethod.WEIGHTED, FusionMethod.RRF])
def test_all_six_channels_integrate_repeat_and_roundtrip(repository, method):
    root, symbols, graph, churn = repository
    config = RetrievalConfig(
        bm25=True, ripgrep=True, semantic=True, structural=True, test=True, history=True
    )
    issue = (
        'one calls two incorrectly\nFile "a.py", line 2, in one\nValueError: one fails\ntest_one'
    )
    kwargs = {
        "config": config,
        "fusion_method": method,
        "embedding_provider": FakeEmbedding(),
        "graph": graph,
        "churn": churn,
    }
    first = retrieve_symbols(root, symbols, issue, **kwargs)
    second = retrieve_symbols(root, symbols, issue, **kwargs)
    reordered = retrieve_symbols(root, list(reversed(symbols)), issue, **kwargs)
    assert (
        first.model_dump_json().encode()
        == second.model_dump_json().encode()
        == reordered.model_dump_json().encode()
    )
    assert list(first.per_channel) == [
        "bm25",
        "ripgrep",
        "semantic",
        "structural",
        "test",
        "history",
    ]
    assert all(first.per_channel[name] for name in first.enabled_channels)
    assert (
        first.symbols[0].symbol_id == "a.one"
        and first.symbols[0].pinned
        and first.symbols[0].score == 0
    )
    assert first.files[0].path == "a.py" and first.files[0].pinned
    restored = RetrievalResult.model_validate_json(first.model_dump_json())
    assert restored == first
    validate_result(restored)


def test_default_multichannel_fuses_and_single_channel_remains_compatible(repository):
    root, symbols, graph, _ = repository
    combined = retrieve_symbols(
        root, symbols, "one", config=RetrievalConfig(structural=True), graph=graph
    )
    assert combined.fusion_method == "weighted" and combined.files and combined.fused_symbols
    legacy = retrieve_symbols(root, symbols, "one")
    configured = retrieve_symbols(root, symbols, "one", config=RetrievalConfig())
    assert legacy == configured
    assert legacy.fusion_method == "none" and legacy.files == [] and legacy.fused_symbols == []


def test_opt_in_single_channel_fusion_and_unconfirmed_external_frame(repository):
    root, symbols, _, _ = repository
    result = retrieve_symbols(
        root,
        symbols,
        'one\nFile "/third-party/a.py", line 2, in one',
        fusion_method=FusionMethod.WEIGHTED,
    )
    assert result.files and result.fusion_method == "weighted"
    assert not any(s.pinned for s in result.symbols)
    validate_result(result)


def test_top_k_keeps_full_channel_results_and_full_fused_ranking(repository):
    root, symbols, _, churn = repository
    kwargs = {"config": RetrievalConfig(history=True), "churn": churn}
    full = retrieve_symbols(root, symbols, "one", **kwargs)
    small = retrieve_symbols(root, symbols, "one", limit=1, **kwargs)
    assert len(small.symbols) == 1
    assert small.fused_symbols == full.fused_symbols
    assert small.per_channel == full.per_channel
    assert small.files == full.files[:1]
    validate_result(small)


def test_all_disabled_can_still_pin_from_verified_repository_frame(repository):
    root, symbols, _, _ = repository
    result = retrieve_symbols(
        root,
        symbols,
        'File "a.py", line 2, in one',
        config=RetrievalConfig(bm25=False),
        fusion_method=FusionMethod.WEIGHTED,
    )
    assert result.per_channel == {} and result.enabled_channels == []
    assert len(result.symbols) == 1 and result.symbols[0].pinned
    validate_result(result)


def test_single_channel_cli_remains_frozen():
    from typer.testing import CliRunner

    from codepilot.cli import app

    result = CliRunner().invoke(app, ["retrieve", "--help"])
    assert result.exit_code == 0
    assert "--fusion" not in result.stdout and "--channels" not in result.stdout


@pytest.mark.parametrize("config", [None, RetrievalConfig()])
def test_explicit_single_channel_weights_are_applied(repository, config):
    root, symbols, _, _ = repository
    result = retrieve_symbols(root, symbols, "one", config=config, channel_weights={"bm25": 0.7})
    assert result.fusion_method == "weighted"
    assert result.channel_weights == {"bm25": 0.7}
    assert max(s.score for s in result.symbols) == pytest.approx(0.7)


@pytest.mark.parametrize("config", [None, RetrievalConfig()])
def test_caller_snapshot_id_is_preserved_without_persistence(repository, config):
    root, symbols, _, _ = repository
    result = retrieve_symbols(root, symbols, "one", config=config, snapshot_id="caller-reference")
    assert result.snapshot_id == "caller-reference"
    assert result.fusion_method == "none"


def test_rrf_parameter_cannot_be_silently_ignored(repository):
    root, symbols, _, _ = repository
    with pytest.raises(ValueError, match="rrf_k"):
        retrieve_symbols(root, symbols, "one", rrf_k=3)
